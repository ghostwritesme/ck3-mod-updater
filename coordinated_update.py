"""Coordinate archive and installed-folder updates across recoverable transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping
from uuid import uuid4

from batch_update import BatchPlanItem
from install_ledger import _atomic_json_write
from installed_deployment import (
    DeploymentError,
    DeploymentRecoveryResult,
    InstalledDeploymentManager,
    InstalledDeploymentResult,
)
from playset_state import PlaysetMod
from update_workflow import RecoveryResult, UpdateError, UpdateManager, UpdateResult


COORDINATOR_SCHEMA_VERSION = 1
FINISHED_PHASES = {"committed", "rolled_back", "cancelled"}


class CoordinatedUpdateError(RuntimeError):
    """Raised when a combined archive/installed update cannot complete safely."""


@dataclass(frozen=True)
class CoordinatedUpdateResult:
    transaction_id: str
    archive_updates: tuple[UpdateResult, ...]
    installed_updates: tuple[InstalledDeploymentResult, ...]


@dataclass(frozen=True)
class CoordinatedRecoveryResult:
    transaction_id: str
    action: str
    detail: str


@dataclass(frozen=True)
class CoordinatedRollbackResult:
    transaction_id: str
    archive_count: int
    installed_count: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CoordinatedUpdateManager:
    """Keep library archives and enabled installed folders consistent after failure or restart."""

    def __init__(
        self,
        journal_directory: Path,
        archive_manager: UpdateManager,
        installed_manager: InstalledDeploymentManager,
    ):
        self.journal_directory = Path(journal_directory)
        self.archive_manager = archive_manager
        self.installed_manager = installed_manager
        self._lock = threading.RLock()

    def _journal_path(self, transaction_id: str) -> Path:
        if len(transaction_id) != 32 or any(value not in "0123456789abcdef" for value in transaction_id):
            raise CoordinatedUpdateError("Invalid coordinated-update transaction ID")
        return self.journal_directory / f"{transaction_id}.json"

    def _write(self, payload: dict[str, Any]) -> None:
        payload["updated_at_utc"] = _utc_now()
        try:
            _atomic_json_write(self._journal_path(str(payload["transaction_id"])), payload)
        except (KeyError, OSError) as error:
            raise CoordinatedUpdateError(f"Could not save coordinated-update journal: {error}") from error

    def _load(self, transaction_id: str) -> dict[str, Any]:
        try:
            payload = json.loads(self._journal_path(transaction_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise CoordinatedUpdateError(f"Could not read coordinated-update journal: {error}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != COORDINATOR_SCHEMA_VERSION:
            raise CoordinatedUpdateError("Coordinated-update journal is invalid or unsupported")
        if payload.get("transaction_id") != transaction_id or not isinstance(payload.get("items"), list):
            raise CoordinatedUpdateError("Coordinated-update journal is malformed")
        return payload

    def apply(
        self,
        items: Iterable[BatchPlanItem],
        enabled_mods: Mapping[str, PlaysetMod],
        data_directory: Path,
    ) -> CoordinatedUpdateResult:
        """Prepare every target, then apply explicit updates with all-or-nothing recovery."""
        with self._lock:
            item_list = tuple(items)
            if not item_list:
                raise CoordinatedUpdateError("The update contains no validated archives")
            transaction_id = uuid4().hex
            payload: dict[str, Any] = {
                "schema_version": COORDINATOR_SCHEMA_VERSION,
                "transaction_id": transaction_id,
                "phase": "preparing",
                "created_at_utc": _utc_now(),
                "items": [],
                "error": None,
            }
            self._write(payload)
            try:
                for item in item_list:
                    archive_prepared = self.archive_manager.prepare_update(
                        item.current_archive,
                        item.candidate_archive,
                        expected_mod_id=item.mod_id,
                        workshop_updated=item.workshop_updated,
                    )
                    record: dict[str, Any] = {
                        "mod_id": item.mod_id,
                        "archive_transaction_id": archive_prepared.transaction_id,
                        "installed_transaction_id": None,
                    }
                    payload["items"].append(record)
                    self._write(payload)
                    enabled = enabled_mods.get(item.mod_id)
                    if enabled is not None:
                        installed_prepared = self.installed_manager.prepare(
                            item.candidate_archive,
                            enabled,
                            data_directory,
                        )
                        record["installed_transaction_id"] = installed_prepared.transaction_id
                        self._write(payload)

                payload["phase"] = "prepared"
                self._write(payload)
                archive_results: list[UpdateResult] = []
                installed_results: list[InstalledDeploymentResult] = []
                for record in payload["items"]:
                    payload["phase"] = "applying"
                    payload["current_mod_id"] = record["mod_id"]
                    self._write(payload)
                    archive_results.append(
                        self.archive_manager.apply_update(record["archive_transaction_id"])
                    )
                    if record["installed_transaction_id"]:
                        installed_results.append(
                            self.installed_manager.apply(record["installed_transaction_id"])
                        )
                payload["phase"] = "committed"
                payload["current_mod_id"] = None
                payload["error"] = None
                self._write(payload)
                return CoordinatedUpdateResult(
                    transaction_id,
                    tuple(archive_results),
                    tuple(installed_results),
                )
            except (UpdateError, DeploymentError, CoordinatedUpdateError) as error:
                payload["error"] = str(error)
                recovery_errors = self._rollback_records(payload)
                payload["phase"] = "rollback_failed" if recovery_errors else "rolled_back"
                if recovery_errors:
                    payload["error"] += "; recovery needs attention: " + "; ".join(recovery_errors)
                self._write(payload)
                if recovery_errors:
                    raise CoordinatedUpdateError(payload["error"]) from error
                raise CoordinatedUpdateError(
                    f"Update failed; archive and installed changes were rolled back: {error}"
                ) from error

    def _rollback_records(self, payload: Mapping[str, Any]) -> list[str]:
        errors: list[str] = []
        records = payload.get("items", [])
        if not isinstance(records, list):
            return ["Coordinated transaction has an invalid item list"]
        for raw in reversed(records):
            if not isinstance(raw, dict):
                errors.append("Coordinated transaction contains an invalid item")
                continue
            installed_id = raw.get("installed_transaction_id")
            if installed_id:
                try:
                    phase = self.installed_manager.transaction_phase(str(installed_id))
                    if phase == "committed":
                        self.installed_manager.rollback(str(installed_id))
                    elif phase == "prepared":
                        self.installed_manager.cancel_prepared(str(installed_id))
                    elif phase not in {"rolled_back", "cancelled"}:
                        errors.append(f"Installed transaction {installed_id} remains in phase {phase}")
                except (OSError, DeploymentError) as error:
                    errors.append(str(error))
            archive_id = raw.get("archive_transaction_id")
            if archive_id:
                try:
                    phase = self.archive_manager.transaction_phase(str(archive_id))
                    if phase == "committed":
                        self.archive_manager.rollback(str(archive_id))
                    elif phase == "prepared":
                        self.archive_manager.cancel_prepared(str(archive_id))
                    elif phase not in {"rolled_back", "cancelled"}:
                        errors.append(f"Archive transaction {archive_id} remains in phase {phase}")
                except (OSError, UpdateError) as error:
                    errors.append(str(error))
        return errors

    def latest_recoverable(self, mod_id: str) -> str | None:
        """Return the newest committed combined update containing a mod ID."""
        with self._lock:
            if not self.journal_directory.is_dir():
                return None
            matches: list[tuple[str, str]] = []
            for path in self.journal_directory.glob("*.json"):
                try:
                    payload = self._load(path.stem)
                except CoordinatedUpdateError:
                    continue
                if payload.get("phase") != "committed":
                    continue
                if any(
                    isinstance(value, dict) and value.get("mod_id") == mod_id
                    for value in payload.get("items", [])
                ):
                    matches.append((str(payload.get("updated_at_utc", "")), path.stem))
            return max(matches)[1] if matches else None

    def rollback(self, transaction_id: str) -> CoordinatedRollbackResult:
        """Undo a committed combined update, including every item in its batch."""
        with self._lock:
            payload = self._load(transaction_id)
            if payload.get("phase") != "committed":
                raise CoordinatedUpdateError("Only a committed combined update can be undone")
            records = [value for value in payload.get("items", []) if isinstance(value, dict)]
            errors = self._rollback_records(payload)
            payload["phase"] = "rollback_failed" if errors else "rolled_back"
            payload["error"] = "; ".join(errors) if errors else None
            self._write(payload)
            if errors:
                raise CoordinatedUpdateError(payload["error"])
            return CoordinatedRollbackResult(
                transaction_id,
                len(records),
                sum(1 for value in records if value.get("installed_transaction_id")),
            )

    def recover_pending(self) -> list[CoordinatedRecoveryResult]:
        """Recover child transactions first, then reconcile interrupted combined operations."""
        with self._lock:
            results: list[CoordinatedRecoveryResult] = []
            archive_recovery: list[RecoveryResult] = self.archive_manager.recover_pending()
            installed_recovery: list[DeploymentRecoveryResult] = self.installed_manager.recover_pending()
            results.extend(
                CoordinatedRecoveryResult(value.transaction_id, value.action, value.detail)
                for value in archive_recovery
            )
            results.extend(
                CoordinatedRecoveryResult(value.transaction_id, value.action, value.detail)
                for value in installed_recovery
            )
            if not self.journal_directory.is_dir():
                return results
            for path in sorted(self.journal_directory.glob("*.json")):
                try:
                    payload = self._load(path.stem)
                    phase = str(payload.get("phase", ""))
                    if phase in FINISHED_PHASES:
                        continue
                    errors = self._rollback_records(payload)
                    payload["phase"] = "rollback_failed" if errors else "rolled_back"
                    payload["error"] = "; ".join(errors) if errors else None
                    self._write(payload)
                    if errors:
                        results.append(CoordinatedRecoveryResult(path.stem, "error", payload["error"]))
                    else:
                        results.append(
                            CoordinatedRecoveryResult(
                                path.stem,
                                "rolled_back",
                                "Interrupted combined update was restored",
                            )
                        )
                except (OSError, UpdateError, DeploymentError, CoordinatedUpdateError) as error:
                    results.append(CoordinatedRecoveryResult(path.stem, "error", str(error)))
            return results
