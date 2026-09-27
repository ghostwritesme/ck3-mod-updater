"""Plan and apply an explicitly selected batch of recoverable archive updates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from mod_scan_core import ArchiveScanRow, inspect_mod_archive
from update_impact import ImpactError, UpdateImpact, compare_archives
from update_workflow import PreparedUpdate, UpdateError, UpdateManager, UpdateResult


@dataclass(frozen=True)
class BatchPlanItem:
    mod_id: str
    name: str
    current_archive: Path
    candidate_archive: Path
    workshop_updated: int
    impact: UpdateImpact


@dataclass(frozen=True)
class BatchPlanIssue:
    candidate_archive: Path
    message: str


@dataclass(frozen=True)
class BatchPlan:
    items: tuple[BatchPlanItem, ...]
    issues: tuple[BatchPlanIssue, ...]


@dataclass(frozen=True)
class BatchResult:
    updates: tuple[UpdateResult, ...]


class BatchUpdateError(RuntimeError):
    """Raised when a batch cannot be completed and recovered safely."""


def build_batch_plan(
    rows: Iterable[ArchiveScanRow],
    candidate_archives: Iterable[Path],
) -> BatchPlan:
    """Match explicitly selected candidates to unique scanned archives."""
    current_by_id: dict[str, ArchiveScanRow] = {}
    blocked_ids: set[str] = set()
    for row in rows:
        mod_id = row.archive.mod_id
        if not mod_id or row.archive.status != "ck3_mod":
            continue
        if mod_id in current_by_id or row.duplicate_count > 1:
            blocked_ids.add(mod_id)
        else:
            current_by_id[mod_id] = row

    issues: list[BatchPlanIssue] = []
    candidate_by_id: dict[str, Path] = {}
    duplicate_candidates: set[str] = set()
    for value in candidate_archives:
        candidate = Path(value).resolve()
        record = inspect_mod_archive(candidate)
        if record.status != "ck3_mod" or not record.mod_id:
            issues.append(BatchPlanIssue(candidate, "No valid CK3 Workshop ID was found"))
            continue
        if record.mod_id in candidate_by_id:
            duplicate_candidates.add(record.mod_id)
            issues.append(BatchPlanIssue(candidate, f"More than one candidate uses ID {record.mod_id}"))
            continue
        candidate_by_id[record.mod_id] = candidate

    items: list[BatchPlanItem] = []
    for mod_id, candidate in candidate_by_id.items():
        if mod_id in duplicate_candidates:
            continue
        if mod_id in blocked_ids:
            issues.append(BatchPlanIssue(candidate, f"Current library has duplicate archives for ID {mod_id}"))
            continue
        current = current_by_id.get(mod_id)
        if current is None:
            issues.append(BatchPlanIssue(candidate, f"No current archive matches ID {mod_id}"))
            continue
        if current.workshop_status != "available" or not current.workshop_updated:
            issues.append(BatchPlanIssue(candidate, f"Workshop revision is unavailable for ID {mod_id}"))
            continue
        try:
            impact = compare_archives(current.archive.archive_path, candidate)
        except ImpactError as error:
            issues.append(BatchPlanIssue(candidate, str(error)))
            continue
        items.append(
            BatchPlanItem(
                mod_id=mod_id,
                name=current.archive.name or current.workshop_title or current.archive.archive_path.name,
                current_archive=current.archive.archive_path,
                candidate_archive=candidate,
                workshop_updated=current.workshop_updated,
                impact=impact,
            )
        )
    return BatchPlan(
        items=tuple(sorted(items, key=lambda item: item.name.casefold())),
        issues=tuple(issues),
    )


class BatchUpdateManager:
    def __init__(self, update_manager: UpdateManager):
        self.update_manager = update_manager

    def apply(self, items: Iterable[BatchPlanItem]) -> BatchResult:
        """Prepare every candidate before replacement and roll back earlier commits on failure."""
        item_list = tuple(items)
        if not item_list:
            raise BatchUpdateError("The batch contains no validated updates")
        prepared: list[PreparedUpdate] = []
        committed: list[UpdateResult] = []
        try:
            for item in item_list:
                prepared.append(
                    self.update_manager.prepare_update(
                        item.current_archive,
                        item.candidate_archive,
                        expected_mod_id=item.mod_id,
                        workshop_updated=item.workshop_updated,
                    )
                )
            for transaction in prepared:
                committed.append(self.update_manager.apply_update(transaction.transaction_id))
        except UpdateError as error:
            recovery_errors: list[str] = []
            committed_ids = {result.transaction_id for result in committed}
            for transaction in reversed(prepared):
                try:
                    if transaction.transaction_id in committed_ids:
                        self.update_manager.rollback(transaction.transaction_id)
                    else:
                        self.update_manager.cancel_prepared(transaction.transaction_id)
                except UpdateError as recovery_error:
                    recovery_errors.append(str(recovery_error))
            detail = f"Batch update failed and completed items were rolled back: {error}"
            if recovery_errors:
                detail += "; recovery needs attention: " + "; ".join(recovery_errors)
            raise BatchUpdateError(detail) from error
        return BatchResult(tuple(committed))
