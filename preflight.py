"""Conservative structural checks for an active CK3 setup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mod_scan_core import ArchiveScanRow
from playset_state import PlaysetState
from save_inspection import SaveMetadata, compare_save_to_playset
from update_impact import ImpactError, inspect_archive_structure


@dataclass(frozen=True)
class PreflightIssue:
    severity: str
    code: str
    message: str
    mod_id: str | None = None


@dataclass(frozen=True)
class PreflightReport:
    checked_mods: int
    issues: tuple[PreflightIssue, ...]

    @property
    def errors(self) -> tuple[PreflightIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[PreflightIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def structurally_ready(self) -> bool:
        return not self.errors


def run_preflight(
    rows: Iterable[ArchiveScanRow],
    playset: PlaysetState,
    save: SaveMetadata | None,
    game_version: str,
) -> PreflightReport:
    """Check identity, archive safety, declarations, and save/playset consistency."""
    by_id: dict[str, list[ArchiveScanRow]] = {}
    for row in rows:
        if row.archive.mod_id:
            by_id.setdefault(row.archive.mod_id, []).append(row)

    issues: list[PreflightIssue] = []
    for warning in playset.warnings:
        issues.append(PreflightIssue("warning", "playset_warning", warning))

    checked = 0
    for mod in playset.mods:
        if not mod.mod_id:
            issues.append(
                PreflightIssue(
                    "warning",
                    "unidentified_enabled_mod",
                    f"Enabled mod has no stable numeric ID: {mod.display_name}",
                )
            )
            continue
        matches = by_id.get(mod.mod_id, [])
        if not matches:
            issues.append(
                PreflightIssue(
                    "error",
                    "missing_archive",
                    f"No source archive was found for enabled mod: {mod.display_name}",
                    mod.mod_id,
                )
            )
            continue
        if len(matches) > 1:
            issues.append(
                PreflightIssue(
                    "error",
                    "duplicate_archive",
                    f"Multiple source archives were found for enabled mod: {mod.display_name}",
                    mod.mod_id,
                )
            )
            continue
        row = matches[0]
        checked += 1
        if row.archive.status != "ck3_mod":
            issues.append(
                PreflightIssue(
                    "error",
                    "invalid_archive",
                    f"Enabled mod archive failed CK3 descriptor validation: {mod.display_name}",
                    mod.mod_id,
                )
            )
            continue
        try:
            inspect_archive_structure(row.archive.archive_path)
        except ImpactError as error:
            issues.append(
                PreflightIssue(
                    "error",
                    "unsafe_archive",
                    f"{mod.display_name}: {error}",
                    mod.mod_id,
                )
            )
        if not mod.directory or not mod.directory.is_dir() or mod.directory.is_symlink():
            issues.append(
                PreflightIssue(
                    "error",
                    "missing_installed_content",
                    f"Enabled installed folder is missing or unsafe: {mod.display_name}",
                    mod.mod_id,
                )
            )
        elif not (mod.directory / "descriptor.mod").is_file():
            issues.append(
                PreflightIssue(
                    "error",
                    "missing_internal_descriptor",
                    f"Enabled installed folder has no descriptor.mod: {mod.display_name}",
                    mod.mod_id,
                )
            )
        if not mod.descriptor_path or not mod.descriptor_path.is_file() or mod.descriptor_path.is_symlink():
            issues.append(
                PreflightIssue(
                    "error",
                    "missing_external_descriptor",
                    f"Enabled launcher descriptor is missing or unsafe: {mod.display_name}",
                    mod.mod_id,
                )
            )
        if row.compatibility == "declared_incompatible":
            issues.append(
                PreflightIssue(
                    "warning",
                    "descriptor_version_warning",
                    f"{mod.display_name} declares a different CK3 version; this does not prove it is broken",
                    mod.mod_id,
                )
            )
        elif row.compatibility == "unknown":
            issues.append(
                PreflightIssue(
                    "warning",
                    "compatibility_not_declared",
                    f"{mod.display_name} does not declare a supported CK3 version",
                    mod.mod_id,
                )
            )

    if save:
        if save.game_version and game_version and save.game_version != game_version:
            issues.append(
                PreflightIssue(
                    "warning",
                    "save_game_version_changed",
                    f"Latest save uses CK3 {save.game_version}; installed CK3 is {game_version}",
                )
            )
        comparison = compare_save_to_playset(save, playset.ordered_mod_ids)
        if not comparison.matching_mods:
            detail: list[str] = []
            if comparison.missing_from_playset:
                detail.append("missing " + ", ".join(comparison.missing_from_playset))
            if comparison.added_since_save:
                detail.append("added " + ", ".join(comparison.added_since_save))
            issues.append(
                PreflightIssue(
                    "warning",
                    "save_playset_changed",
                    "Latest save and active playset differ: " + "; ".join(detail),
                )
            )
    return PreflightReport(checked_mods=checked, issues=tuple(issues))


def preflight_summary(report: PreflightReport) -> str:
    if report.errors:
        return f"Preflight: {len(report.errors)} structural blocker(s), {len(report.warnings)} warning(s)"
    if report.warnings:
        return f"Preflight: no structural blockers, {len(report.warnings)} warning(s)"
    return "Preflight: no structural blockers detected"
