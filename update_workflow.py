"""Transactional, recoverable replacement of CK3 mod archive files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import threading
from typing import Any, Mapping
from uuid import uuid4

from install_ledger import (
    InstallLedger,
    LedgerError,
    _atomic_json_write,
    baseline_from_payload,
    baseline_to_payload,
)
from mod_scan_core import ArchiveBaseline, inspect_mod_archive, sha256_file
from update_impact import ImpactError, inspect_archive_structure


JOURNAL_SCHEMA_VERSION = 1
RECOVERABLE_PHASES = {"backup_created", "replaced", "rollback_failed"}
FINISHED_PHASES = {"committed", "rolled_back", "cancelled"}


class UpdateError(RuntimeError):
    """Raised when an update cannot proceed or cannot be recovered safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.replace(temporary, destination)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class PreparedUpdate:
    transaction_id: str
    mod_id: str
    current_archive: Path
    staged_archive: Path
    backup_archive: Path
    original_sha256: str
    staged_sha256: str
    workshop_updated: int


@dataclass(frozen=True)
class UpdateResult:
    transaction_id: str
    mod_id: str
    archive_path: Path
    backup_path: Path
    installed_sha256: str
    workshop_updated: int


@dataclass(frozen=True)
class RecoveryResult:
    transaction_id: str
    action: str
    detail: str


class UpdateManager:
    """Stage, apply, record, and roll back archive updates."""

    def __init__(self, data_directory: Path, ledger: InstallLedger):
        self.data_directory = Path(data_directory)
        self.staging_directory = self.data_directory / "staging"
        self.backup_directory = self.data_directory / "backups"
        self.journal_directory = self.data_directory / "transactions"
        self.ledger = ledger
        self._lock = threading.RLock()

    def _journal_path(self, transaction_id: str) -> Path:
        if len(transaction_id) != 32 or any(character not in "0123456789abcdef" for character in transaction_id):
            raise UpdateError("Invalid update transaction ID")
        return self.journal_directory / f"{transaction_id}.json"

    def _write_journal(self, payload: dict[str, Any]) -> None:
        payload["updated_at_utc"] = _utc_now()
        try:
            _atomic_json_write(self._journal_path(str(payload["transaction_id"])), payload)
        except (KeyError, OSError) as error:
            raise UpdateError(f"Could not save update journal: {error}") from error

    def _load_journal(self, transaction_id: str) -> dict[str, Any]:
        path = self._journal_path(transaction_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise UpdateError(f"Could not read update journal: {error}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != JOURNAL_SCHEMA_VERSION:
            raise UpdateError("Update journal is invalid or unsupported")
        if payload.get("transaction_id") != transaction_id:
            raise UpdateError("Update journal ID mismatch")
        return payload

    def transaction_phase(self, transaction_id: str) -> str:
        """Return a transaction phase for higher-level recovery coordination."""
        return str(self._load_journal(transaction_id).get("phase", ""))

    def _paths_from_journal(self, payload: Mapping[str, Any]) -> tuple[Path, Path, Path, Path]:
        try:
            target = Path(str(payload["target_path"]))
            staged = Path(str(payload["staged_path"]))
            backup = Path(str(payload["backup_path"]))
            archive_root = Path(str(payload["archive_root"]))
        except KeyError as error:
            raise UpdateError("Update journal is missing required paths") from error
        if not _is_within(staged, self.staging_directory):
            raise UpdateError("Staged archive path is outside the update data directory")
        if not _is_within(backup, self.backup_directory):
            raise UpdateError("Backup path is outside the update data directory")
        if archive_root.resolve() != target.parent.resolve() or not _is_within(target, archive_root):
            raise UpdateError("Target archive path is outside its recorded archive folder")
        if target.suffix.lower() != ".zip" or staged.suffix.lower() != ".zip" or backup.suffix.lower() != ".zip":
            raise UpdateError("Update journal contains a non-ZIP archive path")
        if target.is_symlink() or staged.is_symlink() or backup.is_symlink():
            raise UpdateError("Update journal contains a symbolic-link archive path")
        return target, staged, backup, archive_root

    def prepare_update(
        self,
        current_archive: Path,
        candidate_archive: Path,
        *,
        expected_mod_id: str,
        workshop_updated: int,
    ) -> PreparedUpdate:
        """Validate and copy a candidate ZIP into private staging without mutating the target."""
        with self._lock:
            current_input = Path(current_archive)
            candidate_input = Path(candidate_archive)
            if current_input.is_symlink() or candidate_input.is_symlink():
                raise UpdateError("Symbolic links are not accepted for archive updates")
            current_archive = current_input.resolve()
            candidate_archive = candidate_input.resolve()
            if current_archive == candidate_archive:
                raise UpdateError("Choose a separate update archive, not the current archive")
            if not current_archive.is_file() or not candidate_archive.is_file():
                raise UpdateError("The current archive and update archive must both exist")
            if candidate_archive.suffix.lower() != ".zip":
                raise UpdateError("The staged update must be a ZIP archive")
            if not expected_mod_id.isdigit():
                raise UpdateError("Workshop ID is invalid")
            if not isinstance(workshop_updated, int) or workshop_updated <= 0:
                raise UpdateError("Workshop update timestamp is invalid")

            if self.data_directory.is_symlink():
                raise UpdateError("Private update storage cannot be a symbolic link")
            try:
                self.data_directory.mkdir(parents=True, exist_ok=True)
                required_bytes = (
                    current_archive.stat().st_size
                    + candidate_archive.stat().st_size
                    + 64 * 1024 * 1024
                )
                if shutil.disk_usage(self.data_directory).free < required_bytes:
                    raise UpdateError("Not enough free space to stage, back up, and undo this archive update")
            except OSError as error:
                raise UpdateError(f"Could not measure private update storage: {error}") from error

            try:
                inspect_archive_structure(candidate_archive)
            except ImpactError as error:
                raise UpdateError(str(error)) from error

            current_record = inspect_mod_archive(current_archive)
            candidate_record = inspect_mod_archive(candidate_archive)
            if current_record.status != "ck3_mod" or current_record.mod_id != expected_mod_id:
                raise UpdateError("The current archive does not match the selected Workshop item")
            if candidate_record.status != "ck3_mod" or candidate_record.mod_id != expected_mod_id:
                raise UpdateError("The update archive has a different or missing Workshop ID")

            transaction_id = uuid4().hex
            staged = self.staging_directory / f"{transaction_id}.zip"
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = (
                self.backup_directory
                / expected_mod_id
                / f"{timestamp}_{transaction_id}_{current_archive.name}"
            )
            try:
                _atomic_copy(candidate_archive, staged)
                original_sha256 = sha256_file(current_archive)
                staged_sha256 = sha256_file(staged)
            except OSError as error:
                staged.unlink(missing_ok=True)
                raise UpdateError(f"Could not stage update archive: {error}") from error
            if staged_sha256 == original_sha256:
                staged.unlink(missing_ok=True)
                raise UpdateError("The selected update archive is identical to the current archive")
            staged_record = inspect_mod_archive(staged)
            if staged_record.status != "ck3_mod" or staged_record.mod_id != expected_mod_id:
                staged.unlink(missing_ok=True)
                raise UpdateError("The staged copy failed archive validation")

            previous = self.ledger.load().get(expected_mod_id)
            payload: dict[str, Any] = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "transaction_id": transaction_id,
                "phase": "prepared",
                "created_at_utc": _utc_now(),
                "mod_id": expected_mod_id,
                "archive_root": str(current_archive.parent),
                "target_path": str(current_archive),
                "staged_path": str(staged),
                "backup_path": str(backup),
                "original_sha256": original_sha256,
                "staged_sha256": staged_sha256,
                "workshop_updated": workshop_updated,
                "previous_baseline": baseline_to_payload(previous) if previous else None,
                "new_baseline": None,
                "error": None,
            }
            try:
                self._write_journal(payload)
            except UpdateError:
                staged.unlink(missing_ok=True)
                raise
            return PreparedUpdate(
                transaction_id=transaction_id,
                mod_id=expected_mod_id,
                current_archive=current_archive,
                staged_archive=staged,
                backup_archive=backup,
                original_sha256=original_sha256,
                staged_sha256=staged_sha256,
                workshop_updated=workshop_updated,
            )

    def apply_update(self, transaction_id: str) -> UpdateResult:
        """Back up and atomically replace one prepared archive, rolling back on failure."""
        with self._lock:
            payload = self._load_journal(transaction_id)
            if payload.get("phase") != "prepared":
                raise UpdateError(f"Transaction is not prepared: {payload.get('phase')}")
            target, staged, backup, _archive_root = self._paths_from_journal(payload)
            mod_id = str(payload.get("mod_id", ""))
            original_sha256 = str(payload.get("original_sha256", ""))
            staged_sha256 = str(payload.get("staged_sha256", ""))
            try:
                workshop_updated = int(payload["workshop_updated"])
            except (KeyError, TypeError, ValueError) as error:
                raise UpdateError("Update journal has an invalid Workshop timestamp") from error

            try:
                if sha256_file(target) != original_sha256:
                    raise UpdateError("Current archive changed after the update was prepared")
                if sha256_file(staged) != staged_sha256:
                    raise UpdateError("Staged archive changed after validation")

                _atomic_copy(target, backup)
                if sha256_file(backup) != original_sha256:
                    raise UpdateError("Backup verification failed")
                payload["phase"] = "backup_created"
                self._write_journal(payload)

                _atomic_copy(staged, target)
                if sha256_file(target) != staged_sha256:
                    raise UpdateError("Installed archive hash does not match the staged update")
                installed_record = inspect_mod_archive(target)
                if installed_record.status != "ck3_mod" or installed_record.mod_id != mod_id:
                    raise UpdateError("Installed archive failed validation")
                payload["phase"] = "replaced"
                self._write_journal(payload)

                baseline = self.ledger.record_archive(
                    installed_record,
                    workshop_updated,
                    source="user_confirmed_update",
                )
                payload["new_baseline"] = baseline_to_payload(baseline)
                payload["phase"] = "committed"
                self._write_journal(payload)
                staged.unlink(missing_ok=True)
                return UpdateResult(
                    transaction_id=transaction_id,
                    mod_id=mod_id,
                    archive_path=target,
                    backup_path=backup,
                    installed_sha256=staged_sha256,
                    workshop_updated=workshop_updated,
                )
            except (OSError, LedgerError, UpdateError) as error:
                payload["error"] = str(error)
                if backup.is_file():
                    try:
                        self._restore_from_payload(payload, verify_current=False)
                    except (OSError, LedgerError, UpdateError) as rollback_error:
                        payload["phase"] = "rollback_failed"
                        payload["error"] = f"{error}; rollback failed: {rollback_error}"
                        self._write_journal(payload)
                        raise UpdateError(payload["error"]) from error
                    raise UpdateError(f"Update failed; the original archive was restored: {error}") from error
                payload["phase"] = "cancelled"
                self._write_journal(payload)
                staged.unlink(missing_ok=True)
                raise UpdateError(f"Update cancelled before replacement: {error}") from error

    def _previous_baseline(self, payload: Mapping[str, Any]) -> ArchiveBaseline | None:
        previous = payload.get("previous_baseline")
        if previous is None:
            return None
        return baseline_from_payload(str(payload.get("mod_id", "")), previous)

    def _restore_from_payload(
        self,
        payload: dict[str, Any],
        *,
        verify_current: bool,
    ) -> None:
        target, _staged, backup, _archive_root = self._paths_from_journal(payload)
        if not backup.is_file():
            raise UpdateError("The transaction backup is missing")
        original_sha256 = str(payload.get("original_sha256", ""))
        if sha256_file(backup) != original_sha256:
            raise UpdateError("The transaction backup failed hash verification")
        backup_record = inspect_mod_archive(backup)
        if backup_record.status != "ck3_mod" or backup_record.mod_id != str(payload.get("mod_id", "")):
            raise UpdateError("The transaction backup failed CK3 mod validation")
        if verify_current:
            new_baseline = payload.get("new_baseline")
            if not isinstance(new_baseline, dict):
                raise UpdateError("The committed transaction has no installed baseline")
            expected_current = str(new_baseline.get("archive_sha256", ""))
            if not target.is_file() or sha256_file(target) != expected_current:
                raise UpdateError("Current archive changed after this update; automatic rollback was refused")
            recorded_baseline = self.ledger.load().get(str(payload.get("mod_id", "")))
            expected_baseline = baseline_from_payload(
                str(payload.get("mod_id", "")),
                new_baseline,
            )
            if recorded_baseline != expected_baseline:
                raise UpdateError("The install baseline changed after this update; automatic rollback was refused")
        elif target.is_file():
            current_sha256 = sha256_file(target)
            expected_hashes = {
                original_sha256,
                str(payload.get("staged_sha256", "")),
            }
            if current_sha256 not in expected_hashes:
                raise UpdateError(
                    "Current archive changed outside this interrupted update; automatic recovery was refused"
                )
        _atomic_copy(backup, target)
        if sha256_file(target) != original_sha256:
            raise UpdateError("Restored archive failed hash verification")
        restored = inspect_mod_archive(target)
        if restored.status != "ck3_mod" or restored.mod_id != str(payload.get("mod_id", "")):
            raise UpdateError("Restored archive failed CK3 mod validation")
        self.ledger.replace_record(str(payload["mod_id"]), self._previous_baseline(payload))
        payload["phase"] = "rolled_back"
        payload["error"] = None
        payload["rolled_back_at_utc"] = _utc_now()
        self._write_journal(payload)

    def rollback(self, transaction_id: str) -> UpdateResult:
        """Restore the verified backup for a committed transaction."""
        with self._lock:
            payload = self._load_journal(transaction_id)
            if payload.get("phase") != "committed":
                raise UpdateError("Only a committed update can be undone manually")
            target, _staged, backup, _archive_root = self._paths_from_journal(payload)
            self._restore_from_payload(payload, verify_current=True)
            return UpdateResult(
                transaction_id=transaction_id,
                mod_id=str(payload["mod_id"]),
                archive_path=target,
                backup_path=backup,
                installed_sha256=str(payload["original_sha256"]),
                workshop_updated=int(payload["workshop_updated"]),
            )

    def cancel_prepared(self, transaction_id: str) -> None:
        """Remove private staging for a transaction that has not changed its target."""
        with self._lock:
            payload = self._load_journal(transaction_id)
            if payload.get("phase") in {"cancelled", "rolled_back"}:
                return
            if payload.get("phase") != "prepared":
                raise UpdateError("Only an unapplied prepared update can be cancelled")
            _target, staged, _backup, _archive_root = self._paths_from_journal(payload)
            staged.unlink(missing_ok=True)
            payload["phase"] = "cancelled"
            payload["error"] = "Cancelled before batch replacement"
            self._write_journal(payload)

    def recover_pending(self) -> list[RecoveryResult]:
        """Cancel untouched preparations and restore interrupted replacements."""
        with self._lock:
            if not self.journal_directory.is_dir():
                self._cleanup_orphaned_internal_files(set())
                return []
            results: list[RecoveryResult] = []
            referenced_staging: set[Path] = set()
            for path in sorted(self.journal_directory.glob("*.json")):
                transaction_id = path.stem
                try:
                    payload = self._load_journal(transaction_id)
                    phase = str(payload.get("phase", ""))
                    target, staged, backup, _archive_root = self._paths_from_journal(payload)
                    referenced_staging.add(staged.resolve())
                    self._cleanup_copy_temps(target)
                    self._cleanup_copy_temps(backup)
                    if phase in FINISHED_PHASES:
                        staged.unlink(missing_ok=True)
                        continue
                    if phase == "prepared":
                        staged.unlink(missing_ok=True)
                        payload["phase"] = "cancelled"
                        payload["error"] = "Cancelled during startup recovery before replacement"
                        self._write_journal(payload)
                        results.append(RecoveryResult(transaction_id, "cancelled", "Unused staged update removed"))
                    elif phase in RECOVERABLE_PHASES and backup.is_file():
                        self._restore_from_payload(payload, verify_current=False)
                        staged.unlink(missing_ok=True)
                        results.append(RecoveryResult(transaction_id, "rolled_back", "Original archive restored"))
                    else:
                        raise UpdateError(f"Unsupported interrupted phase: {phase}")
                except (OSError, LedgerError, UpdateError) as error:
                    results.append(RecoveryResult(transaction_id, "error", str(error)))
            self._cleanup_orphaned_internal_files(referenced_staging)
            return results

    def _cleanup_copy_temps(self, destination: Path) -> None:
        if not destination.parent.is_dir():
            return
        for path in destination.parent.glob(f".{destination.name}.*.tmp"):
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)

    def _cleanup_orphaned_internal_files(self, referenced_staging: set[Path]) -> None:
        if self.staging_directory.is_dir():
            for path in self.staging_directory.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                if path.name.startswith(".") and path.name.endswith(".tmp"):
                    path.unlink(missing_ok=True)
                elif path.suffix.lower() == ".zip" and path.resolve() not in referenced_staging:
                    path.unlink(missing_ok=True)
        if self.backup_directory.is_dir():
            for path in self.backup_directory.rglob(".*.tmp"):
                if path.is_file() and not path.is_symlink():
                    path.unlink(missing_ok=True)

    def latest_recoverable(self, mod_id: str, archive_path: Path) -> str | None:
        """Return the latest committed transaction that still has a valid backup."""
        with self._lock:
            if not self.journal_directory.is_dir():
                return None
            matches: list[tuple[str, str]] = []
            resolved_archive = Path(archive_path).resolve()
            for path in self.journal_directory.glob("*.json"):
                try:
                    payload = self._load_journal(path.stem)
                    target, _staged, backup, _archive_root = self._paths_from_journal(payload)
                except UpdateError:
                    continue
                if (
                    payload.get("phase") == "committed"
                    and payload.get("mod_id") == mod_id
                    and target.resolve() == resolved_archive
                    and backup.is_file()
                ):
                    matches.append((str(payload.get("updated_at_utc", "")), path.stem))
            return max(matches)[1] if matches else None
