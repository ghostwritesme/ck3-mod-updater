"""Verified, user-visible snapshots of a working CK3 mod setup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from typing import Iterable, Mapping
from uuid import uuid4

from mod_scan_core import sha256_file
from playset_state import PlaysetState
from save_inspection import SaveMetadata


SNAPSHOT_SCHEMA_VERSION = 1
CONFIGURATION_FILES = (
    "dlc_load.json",
    "game_data.json",
    "mods_registry.json",
    "pdx_settings.txt",
)


class SnapshotError(RuntimeError):
    """Raised when a complete setup snapshot cannot be created or verified."""


@dataclass(frozen=True)
class SnapshotEstimate:
    file_count: int
    total_bytes: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_id: str
    name: str
    directory: Path
    file_count: int
    total_bytes: int
    warnings: tuple[str, ...]
    game_version: str | None = None
    created_at_utc: str | None = None


@dataclass(frozen=True)
class _SnapshotSource:
    source: Path
    destination: Path
    category: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:60] or "CK3 setup"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _files_in_directory(source: Path, destination: Path, category: str) -> Iterable[_SnapshotSource]:
    if source.is_symlink():
        raise SnapshotError(f"Symbolic-link mod folder cannot be snapshotted: {source}")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise SnapshotError(f"Symbolic-link mod content cannot be snapshotted: {path}")
        if path.is_file():
            yield _SnapshotSource(path, destination / path.relative_to(source), category)


class SnapshotManager:
    """Create and verify snapshots without modifying their original sources."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _collect_sources(
        self,
        playset: PlaysetState,
        archives_by_mod_id: Mapping[str, Iterable[Path]],
        save: SaveMetadata | None,
    ) -> tuple[list[_SnapshotSource], list[str]]:
        sources: list[_SnapshotSource] = []
        warnings: list[str] = list(playset.warnings)
        for filename in CONFIGURATION_FILES:
            path = playset.data_directory / filename
            if path.is_file() and not path.is_symlink():
                sources.append(_SnapshotSource(path, Path("configuration") / filename, "configuration"))

        seen_archives: set[Path] = set()
        for mod in playset.mods:
            if mod.descriptor_path and mod.descriptor_path.is_file():
                sources.append(
                    _SnapshotSource(
                        mod.descriptor_path,
                        Path("installed") / "mod" / mod.descriptor_path.name,
                        "installed_descriptor",
                    )
                )
            else:
                warnings.append(f"Descriptor is missing for enabled mod: {mod.display_name}")
            if mod.directory and mod.directory.is_dir():
                sources.extend(
                    _files_in_directory(
                        mod.directory,
                        Path("installed") / "mod" / mod.directory.name,
                        "installed_content",
                    )
                )
            else:
                warnings.append(f"Installed folder is missing for enabled mod: {mod.display_name}")
            matches = tuple(archives_by_mod_id.get(mod.mod_id, ())) if mod.mod_id else ()
            if not matches:
                warnings.append(f"No source archive matched enabled mod: {mod.display_name}")
            for archive in matches:
                resolved = Path(archive).resolve()
                if resolved in seen_archives:
                    continue
                seen_archives.add(resolved)
                if not resolved.is_file() or resolved.is_symlink():
                    warnings.append(f"Source archive is unavailable: {resolved.name}")
                    continue
                sources.append(
                    _SnapshotSource(
                        resolved,
                        Path("archives") / resolved.name,
                        "source_archive",
                    )
                )

        if save:
            if save.path.is_file() and not save.path.is_symlink():
                sources.append(_SnapshotSource(save.path, Path("save") / save.path.name, "save"))
            else:
                warnings.append("The selected save is no longer available")

        destinations: set[Path] = set()
        for source in sources:
            if source.destination in destinations:
                raise SnapshotError(f"Snapshot destination collision: {source.destination}")
            destinations.add(source.destination)
        return sources, warnings

    def estimate(
        self,
        playset: PlaysetState,
        archives_by_mod_id: Mapping[str, Iterable[Path]],
        save: SaveMetadata | None = None,
    ) -> SnapshotEstimate:
        sources, warnings = self._collect_sources(playset, archives_by_mod_id, save)
        try:
            total_bytes = sum(source.source.stat().st_size for source in sources)
        except OSError as error:
            raise SnapshotError(f"Could not measure snapshot files: {error}") from error
        return SnapshotEstimate(len(sources), total_bytes, tuple(warnings))

    def create(
        self,
        name: str,
        game_version: str,
        playset: PlaysetState,
        archives_by_mod_id: Mapping[str, Iterable[Path]],
        save: SaveMetadata | None = None,
    ) -> SnapshotResult:
        sources, warnings = self._collect_sources(playset, archives_by_mod_id, save)
        if not sources:
            raise SnapshotError("There are no active setup files to preserve")
        total_bytes = sum(source.source.stat().st_size for source in sources)
        snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        safe_name = _safe_name(name)
        target = self.root / f"{snapshot_id} {safe_name}"
        temporary = self.root / f".{snapshot_id}.tmp"
        self.root.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(self.root).free
        if free_bytes < total_bytes + 64 * 1024 * 1024:
            raise SnapshotError("Not enough free space to create and verify this setup snapshot")

        entries: list[dict[str, object]] = []
        try:
            temporary.mkdir()
            for source in sources:
                destination = temporary / source.destination
                if not _is_within(destination, temporary):
                    raise SnapshotError("Unsafe snapshot destination was refused")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source.source, destination)
                source_hash = sha256_file(source.source)
                copied_hash = sha256_file(destination)
                if source_hash != copied_hash:
                    raise SnapshotError(f"Snapshot verification failed: {source.source.name}")
                entries.append(
                    {
                        "category": source.category,
                        "snapshot_path": source.destination.as_posix(),
                        "original_path": str(source.source),
                        "size": destination.stat().st_size,
                        "sha256": copied_hash,
                    }
                )

            created_at_utc = _utc_now()
            manifest = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "name": safe_name,
                "created_at_utc": created_at_utc,
                "game_version": game_version,
                "provider": playset.provider,
                "enabled_mods": [
                    {
                        "position": mod.position,
                        "mod_id": mod.mod_id,
                        "display_name": mod.display_name,
                        "game_registry_id": mod.game_registry_id,
                    }
                    for mod in playset.mods
                ],
                "disabled_dlcs": list(playset.disabled_dlcs),
                "save": (
                    {
                        "filename": save.path.name,
                        "game_version": save.game_version,
                        "game_date": save.game_date,
                        "mod_ids": list(save.mod_ids),
                    }
                    if save
                    else None
                ),
                "warnings": warnings,
                "files": entries,
            }
            manifest_path = temporary / "snapshot.json"
            with manifest_path.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(manifest, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except (OSError, SnapshotError) as error:
            if temporary.exists() and _is_within(temporary, self.root):
                shutil.rmtree(temporary, ignore_errors=True)
            if isinstance(error, SnapshotError):
                raise
            raise SnapshotError(f"Could not create setup snapshot: {error}") from error
        return SnapshotResult(
            snapshot_id=snapshot_id,
            name=safe_name,
            directory=target,
            file_count=len(entries),
            total_bytes=total_bytes,
            warnings=tuple(warnings),
            game_version=game_version,
            created_at_utc=created_at_utc,
        )

    def list_snapshots(self) -> tuple[SnapshotResult, ...]:
        if not self.root.is_dir():
            return ()
        results: list[SnapshotResult] = []
        for manifest_path in self.root.glob("*/snapshot.json"):
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                files = payload.get("files", [])
                results.append(
                    SnapshotResult(
                        snapshot_id=str(payload["snapshot_id"]),
                        name=str(payload["name"]),
                        directory=manifest_path.parent,
                        file_count=len(files) if isinstance(files, list) else 0,
                        total_bytes=sum(
                            int(entry.get("size", 0))
                            for entry in files
                            if isinstance(entry, dict)
                        ),
                        warnings=tuple(str(value) for value in payload.get("warnings", [])),
                        game_version=(
                            str(payload["game_version"])
                            if payload.get("game_version")
                            else None
                        ),
                        created_at_utc=(
                            str(payload["created_at_utc"])
                            if payload.get("created_at_utc")
                            else None
                        ),
                    )
                )
            except (KeyError, OSError, TypeError, ValueError):
                continue
        return tuple(sorted(results, key=lambda result: result.snapshot_id, reverse=True))

    def verify(self, snapshot_directory: Path) -> tuple[str, ...]:
        directory = Path(snapshot_directory).resolve()
        if not _is_within(directory, self.root):
            raise SnapshotError("Snapshot is outside the configured snapshot storage")
        try:
            payload = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise SnapshotError(f"Could not read snapshot manifest: {error}") from error
        if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotError("Snapshot format is invalid or unsupported")
        problems: list[str] = []
        entries = payload.get("files")
        if not isinstance(entries, list):
            raise SnapshotError("Snapshot file list is invalid")
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append("Invalid file record")
                continue
            relative = Path(str(entry.get("snapshot_path", "")))
            path = directory / relative
            if not _is_within(path, directory) or not path.is_file() or path.is_symlink():
                problems.append(f"Missing or unsafe file: {relative.as_posix()}")
                continue
            try:
                if sha256_file(path) != str(entry.get("sha256", "")):
                    problems.append(f"Hash mismatch: {relative.as_posix()}")
            except OSError:
                problems.append(f"Unreadable file: {relative.as_posix()}")
        return tuple(problems)
