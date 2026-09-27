"""Verified, recoverable restore of setup snapshots without editing launcher databases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
from typing import Any, Iterable, Mapping
from uuid import uuid4

from install_ledger import _atomic_json_write
from installed_deployment import PROCESS_CHECK_UNAVAILABLE, running_blocked_processes
from mod_scan_core import inspect_archive_directory, inspect_mod_archive, sha256_file
from playset_state import PlaysetState
from setup_snapshots import SNAPSHOT_SCHEMA_VERSION, SnapshotError, SnapshotManager


RESTORE_SCHEMA_VERSION = 1
ACTIVE_PHASES = {"applying", "swap_started", "replaced", "rollback_failed"}
FINISHED_PHASES = {"committed", "rolled_back", "cancelled"}


class SnapshotRestoreError(RuntimeError):
    """Raised when a snapshot restore cannot proceed or recover safely."""


@dataclass(frozen=True)
class RestoreOperation:
    category: str
    source: Path
    destination: Path
    destination_root: Path
    kind: str


@dataclass(frozen=True)
class SnapshotRestorePlan:
    snapshot_directory: Path
    snapshot_name: str
    operations: tuple[RestoreOperation, ...]
    configuration_differences: tuple[str, ...]
    reconciliation: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotRestoreResult:
    transaction_id: str
    restored_files: int
    restored_directories: int
    backup_locations: tuple[Path, ...]
    restored_save: Path | None


@dataclass(frozen=True)
class SnapshotRestoreRecoveryResult:
    transaction_id: str
    action: str
    detail: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, ValueError):
        return False
    return True


def _safe_remove_tree(path: Path, root: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or path.resolve() == root.resolve() or not _is_within(path, root):
        raise SnapshotRestoreError("Refused to remove an unsafe restore work directory")
    shutil.rmtree(path)


def _directory_digest(directory: Path) -> str:
    if not directory.is_dir() or directory.is_symlink():
        raise SnapshotRestoreError(f"Restore directory is missing or unsafe: {directory}")
    digest = hashlib.sha256()
    seen: set[str] = set()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise SnapshotRestoreError(f"Symbolic-link content was refused: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        key = relative.casefold()
        if key in seen:
            raise SnapshotRestoreError(f"Directory contains a case-colliding path: {relative}")
        seen.add(key)
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _value_digest(path: Path, kind: str) -> str:
    return _directory_digest(path) if kind == "directory" else sha256_file(path)


def _path_size(path: Path, kind: str) -> int:
    if kind == "file":
        return path.stat().st_size
    total = 0
    for value in path.rglob("*"):
        if value.is_symlink():
            raise SnapshotRestoreError(f"Symbolic-link content was refused: {value}")
        if value.is_file():
            total += value.stat().st_size
    return total


def _load_snapshot_manifest(manager: SnapshotManager, snapshot_directory: Path) -> dict[str, Any]:
    problems = manager.verify(snapshot_directory)
    if problems:
        raise SnapshotRestoreError("Snapshot verification failed: " + "; ".join(problems[:10]))
    try:
        payload = json.loads((Path(snapshot_directory) / "snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise SnapshotRestoreError(f"Could not read snapshot manifest: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotRestoreError("Snapshot format is invalid or unsupported")
    return payload


def build_restore_plan(
    snapshot_manager: SnapshotManager,
    snapshot_directory: Path,
    archive_directory: Path,
    data_directory: Path,
    current_playset: PlaysetState | None,
) -> SnapshotRestorePlan:
    """Map verified snapshot content to current safe roots without using recorded absolute paths."""
    snapshot = Path(snapshot_directory).resolve()
    archive_root = Path(archive_directory).resolve()
    data_root = Path(data_directory).resolve()
    mod_root = data_root / "mod"
    save_root = data_root / "save games"
    if not archive_root.is_dir():
        raise SnapshotRestoreError("Choose an existing archive library before restoring a snapshot")
    if not mod_root.is_dir():
        raise SnapshotRestoreError("The configured CK3 mod folder does not exist")
    payload = _load_snapshot_manifest(snapshot_manager, snapshot)
    entries = payload.get("files")
    if not isinstance(entries, list):
        raise SnapshotRestoreError("Snapshot file list is invalid")

    installed_directories: set[str] = set()
    current_mods_by_id = {
        mod.mod_id: mod
        for mod in current_playset.mods
        if mod.mod_id
    } if current_playset else {}
    current_archives_by_id: dict[str, list[Path]] = {}
    try:
        for record in inspect_archive_directory(archive_root):
            if record.status == "ck3_mod" and record.mod_id:
                current_archives_by_id.setdefault(record.mod_id, []).append(record.archive_path.resolve())
    except OSError as error:
        raise SnapshotRestoreError(f"Could not inspect the current archive library: {error}") from error
    operations: list[RestoreOperation] = []
    configuration_differences: list[str] = []
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    seen_destinations: set[Path] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise SnapshotRestoreError("Snapshot contains an invalid file record")
        relative = Path(str(raw.get("snapshot_path", "")))
        source = (snapshot / relative).resolve()
        if not _is_within(source, snapshot) or not source.is_file() or source.is_symlink():
            raise SnapshotRestoreError(f"Snapshot contains an unsafe file: {relative.as_posix()}")
        category = str(raw.get("category", ""))
        parts = relative.parts
        if category == "source_archive" and len(parts) == 2 and parts[0] == "archives":
            archive_record = inspect_mod_archive(source)
            matches = (
                current_archives_by_id.get(archive_record.mod_id, [])
                if archive_record.status == "ck3_mod" and archive_record.mod_id
                else []
            )
            if len(matches) > 1:
                raise SnapshotRestoreError(
                    f"Current archive library has duplicate files for ID {archive_record.mod_id}"
                )
            destination = matches[0] if matches else archive_root / parts[1]
            operation = RestoreOperation(category, source, destination, archive_root, "file")
        elif category == "installed_descriptor" and len(parts) == 3 and parts[:2] == ("installed", "mod"):
            snapshot_id = Path(parts[2]).stem
            current_mod = current_mods_by_id.get(snapshot_id)
            destination = (
                current_mod.descriptor_path
                if current_mod and current_mod.descriptor_path
                else mod_root / parts[2]
            )
            operation = RestoreOperation(category, source, destination, mod_root, "file")
        elif category == "installed_content" and len(parts) >= 4 and parts[:2] == ("installed", "mod"):
            installed_directories.add(parts[2])
            continue
        elif category == "save" and len(parts) == 2 and parts[0] == "save":
            save_name = Path(parts[1])
            destination = save_root / f"{save_name.stem} restored {timestamp}{save_name.suffix}"
            operation = RestoreOperation(category, source, destination, save_root, "file")
        elif category == "configuration" and len(parts) == 2 and parts[0] == "configuration":
            current = data_root / parts[1]
            expected_hash = str(raw.get("sha256", ""))
            if not current.is_file() or current.is_symlink() or sha256_file(current) != expected_hash:
                configuration_differences.append(parts[1])
            continue
        else:
            raise SnapshotRestoreError(f"Unsupported snapshot entry was refused: {relative.as_posix()}")
        resolved_destination = operation.destination.resolve()
        if not _is_within(resolved_destination, operation.destination_root):
            raise SnapshotRestoreError("Snapshot restore destination escaped its selected root")
        if resolved_destination in seen_destinations:
            raise SnapshotRestoreError(f"Snapshot restore destination collision: {operation.destination.name}")
        seen_destinations.add(resolved_destination)
        operations.append(operation)

    for folder_name in sorted(installed_directories, key=str.casefold):
        source = snapshot / "installed" / "mod" / folder_name
        current_mod = current_mods_by_id.get(folder_name)
        destination = (
            current_mod.directory
            if current_mod and current_mod.directory
            else mod_root / folder_name
        )
        resolved_destination = destination.resolve()
        if not source.is_dir() or source.is_symlink() or not _is_within(source, snapshot):
            raise SnapshotRestoreError(f"Snapshot installed folder is missing or unsafe: {folder_name}")
        if resolved_destination in seen_destinations:
            raise SnapshotRestoreError(f"Snapshot restore destination collision: {folder_name}")
        seen_destinations.add(resolved_destination)
        operations.append(RestoreOperation("installed_content", source, destination, mod_root, "directory"))

    snapshot_mods = payload.get("enabled_mods", [])
    snapshot_ids = tuple(
        str(value.get("mod_id"))
        for value in snapshot_mods
        if isinstance(value, dict) and value.get("mod_id")
    )
    reconciliation: list[str] = []
    if current_playset is None:
        reconciliation.append("The active playset could not be read; no launcher configuration will be changed.")
    else:
        current_ids = current_playset.ordered_mod_ids
        missing = [value for value in snapshot_ids if value not in current_ids]
        extra = [value for value in current_ids if value not in snapshot_ids]
        if missing:
            reconciliation.append("Snapshot mods not enabled now: " + ", ".join(missing))
        if extra:
            reconciliation.append("Currently enabled mods not in snapshot: " + ", ".join(extra))
        if not missing and not extra and current_ids != snapshot_ids:
            reconciliation.append("The enabled mod set matches, but the load order differs from the snapshot.")
        if not missing and not extra and current_ids == snapshot_ids:
            reconciliation.append("The active mod set and load order match the snapshot.")
    if not operations:
        raise SnapshotRestoreError("Snapshot has no restorable archives, installed mods, or save")
    return SnapshotRestorePlan(
        snapshot,
        str(payload.get("name") or snapshot.name),
        tuple(operations),
        tuple(configuration_differences),
        tuple(reconciliation),
    )


class SnapshotRestoreManager:
    """Apply and recover a multi-volume snapshot restore while preserving every replaced path."""

    def __init__(self, journal_directory: Path, process_check=running_blocked_processes):
        self.journal_directory = Path(journal_directory)
        self.process_check = process_check
        self._lock = threading.RLock()

    def _journal_path(self, transaction_id: str) -> Path:
        if len(transaction_id) != 32 or any(value not in "0123456789abcdef" for value in transaction_id):
            raise SnapshotRestoreError("Invalid snapshot-restore transaction ID")
        return self.journal_directory / f"{transaction_id}.json"

    def _write(self, payload: dict[str, Any]) -> None:
        payload["updated_at_utc"] = _utc_now()
        try:
            _atomic_json_write(self._journal_path(str(payload["transaction_id"])), payload)
        except (KeyError, OSError) as error:
            raise SnapshotRestoreError(f"Could not save snapshot-restore journal: {error}") from error

    def _load(self, transaction_id: str) -> dict[str, Any]:
        try:
            payload = json.loads(self._journal_path(transaction_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise SnapshotRestoreError(f"Could not read snapshot-restore journal: {error}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != RESTORE_SCHEMA_VERSION:
            raise SnapshotRestoreError("Snapshot-restore journal is invalid or unsupported")
        if payload.get("transaction_id") != transaction_id or not isinstance(payload.get("operations"), list):
            raise SnapshotRestoreError("Snapshot-restore journal is malformed")
        return payload

    def _ensure_processes_closed(self) -> None:
        running = self.process_check()
        if PROCESS_CHECK_UNAVAILABLE in running:
            raise SnapshotRestoreError(
                "Could not verify whether CK3 or a mod manager is running; no files were restored"
            )
        if running:
            raise SnapshotRestoreError(
                "Close CK3, Irony Mod Manager, and the Paradox launcher before restoring files. "
                "Still running: " + ", ".join(running)
            )

    def _validate_operation(self, raw: Mapping[str, Any]) -> dict[str, Path]:
        try:
            paths = {
                name: Path(str(raw[name]))
                for name in (
                    "source",
                    "destination",
                    "destination_root",
                    "internal_root",
                    "stage",
                    "backup",
                    "quarantine",
                )
            }
        except KeyError as error:
            raise SnapshotRestoreError("Snapshot-restore operation is missing required paths") from error
        if not _is_within(paths["destination"], paths["destination_root"]):
            raise SnapshotRestoreError("Snapshot-restore target is outside its selected root")
        category = str(raw.get("category", ""))
        base_root = (
            paths["destination_root"].parent
            if category in {"installed_content", "installed_descriptor", "save"}
            else paths["destination_root"]
        )
        if paths["internal_root"].resolve() != (base_root / ".ck3-mod-updater").resolve():
            raise SnapshotRestoreError("Snapshot-restore private storage root is invalid")
        for name in ("stage", "backup", "quarantine"):
            if not _is_within(paths[name], paths["internal_root"]):
                raise SnapshotRestoreError("Snapshot-restore work path is outside its private directory")
        return paths

    def _copy_to_stage(self, source: Path, stage: Path, kind: str) -> str:
        stage.parent.mkdir(parents=True, exist_ok=True)
        if kind == "directory":
            shutil.copytree(source, stage, copy_function=shutil.copy2)
        else:
            shutil.copy2(source, stage)
        return _value_digest(stage, kind)

    def prepare(self, plan: SnapshotRestorePlan) -> str:
        with self._lock:
            self._ensure_processes_closed()
            required_by_volume: dict[str, int] = {}
            probe_by_volume: dict[str, Path] = {}
            for operation in plan.operations:
                volume = (operation.destination_root.anchor or str(operation.destination_root)).casefold()
                required_by_volume[volume] = required_by_volume.get(volume, 0) + _path_size(
                    operation.source, operation.kind
                )
                if operation.destination.exists():
                    required_by_volume[volume] += _path_size(operation.destination, operation.kind)
                probe_by_volume[volume] = operation.destination_root
            for volume, required in required_by_volume.items():
                try:
                    free = shutil.disk_usage(probe_by_volume[volume]).free
                except OSError as error:
                    raise SnapshotRestoreError(f"Could not measure restore storage: {error}") from error
                if free < required + 64 * 1024 * 1024:
                    raise SnapshotRestoreError("Not enough free space to stage, back up, and undo this restore")
            transaction_id = uuid4().hex
            payload: dict[str, Any] = {
                "schema_version": RESTORE_SCHEMA_VERSION,
                "transaction_id": transaction_id,
                "phase": "preparing",
                "created_at_utc": _utc_now(),
                "snapshot_directory": str(plan.snapshot_directory),
                "operations": [],
                "error": None,
            }
            self._write(payload)
            try:
                for index, operation in enumerate(plan.operations):
                    destination_root = operation.destination_root.resolve()
                    data_root = destination_root.parent if destination_root.name in {"mod", "save games"} else destination_root
                    internal_root = data_root / ".ck3-mod-updater"
                    if internal_root.is_symlink():
                        raise SnapshotRestoreError("Private restore storage cannot be a symbolic link")
                    work_root = internal_root / "snapshot-restores"
                    stage = work_root / "staging" / transaction_id / str(index)
                    backup = work_root / "backups" / transaction_id / str(index)
                    quarantine = work_root / "quarantine" / transaction_id / str(index)
                    if operation.kind == "file":
                        stage = stage.with_suffix(operation.destination.suffix)
                        backup = backup.with_suffix(operation.destination.suffix)
                        quarantine = quarantine.with_suffix(operation.destination.suffix)
                    if operation.destination.is_symlink():
                        raise SnapshotRestoreError(f"Symbolic-link restore target was refused: {operation.destination}")
                    original_digest = (
                        _value_digest(operation.destination, operation.kind)
                        if operation.destination.exists()
                        else None
                    )
                    raw_operation = {
                            "category": operation.category,
                            "kind": operation.kind,
                            "source": str(operation.source),
                            "destination": str(operation.destination),
                            "destination_root": str(destination_root),
                            "internal_root": str(internal_root),
                            "stage": str(stage),
                            "backup": str(backup),
                            "quarantine": str(quarantine),
                            "original_digest": original_digest,
                            "staged_digest": None,
                            "state": "staging",
                        }
                    payload["operations"].append(raw_operation)
                    self._write(payload)
                    staged_digest = self._copy_to_stage(operation.source, stage, operation.kind)
                    source_digest = _value_digest(operation.source, operation.kind)
                    if staged_digest != source_digest:
                        raise SnapshotRestoreError(f"Restore staging verification failed: {operation.destination.name}")
                    raw_operation["staged_digest"] = staged_digest
                    raw_operation["state"] = "prepared"
                    self._write(payload)
                payload["phase"] = "prepared"
                self._write(payload)
                return transaction_id
            except (OSError, SnapshotRestoreError) as error:
                payload["error"] = str(error)
                self._cleanup_staging(payload)
                payload["phase"] = "cancelled"
                self._write(payload)
                if isinstance(error, SnapshotRestoreError):
                    raise
                raise SnapshotRestoreError(f"Could not prepare snapshot restore: {error}") from error

    def apply(self, transaction_id: str) -> SnapshotRestoreResult:
        with self._lock:
            self._ensure_processes_closed()
            payload = self._load(transaction_id)
            if payload.get("phase") != "prepared":
                raise SnapshotRestoreError(f"Snapshot restore is not prepared: {payload.get('phase')}")
            try:
                payload["phase"] = "applying"
                self._write(payload)
                for raw in payload["operations"]:
                    paths = self._validate_operation(raw)
                    kind = str(raw.get("kind"))
                    destination = paths["destination"]
                    current = _value_digest(destination, kind) if destination.exists() else None
                    if current != raw.get("original_digest"):
                        raise SnapshotRestoreError(f"Restore target changed after preparation: {destination}")
                    if _value_digest(paths["stage"], kind) != raw.get("staged_digest"):
                        raise SnapshotRestoreError(f"Restore staging changed after verification: {destination.name}")
                    raw["state"] = "swap_started"
                    payload["phase"] = "swap_started"
                    self._write(payload)
                    if destination.exists():
                        paths["backup"].parent.mkdir(parents=True, exist_ok=True)
                        os.replace(destination, paths["backup"])
                        if _value_digest(paths["backup"], kind) != raw.get("original_digest"):
                            raise SnapshotRestoreError(f"Restore backup verification failed: {destination.name}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(paths["stage"], destination)
                    raw["state"] = "replaced"
                    payload["phase"] = "replaced"
                    self._write(payload)
                    if _value_digest(destination, kind) != raw.get("staged_digest"):
                        raise SnapshotRestoreError(f"Restored path failed verification: {destination.name}")
                payload["phase"] = "committed"
                payload["error"] = None
                self._write(payload)
                return self._result(payload)
            except (OSError, SnapshotRestoreError) as error:
                payload["error"] = str(error)
                recovery_errors = self._rollback_payload(payload, verify_committed=False)
                payload["phase"] = "rollback_failed" if recovery_errors else "rolled_back"
                if recovery_errors:
                    payload["error"] += "; recovery needs attention: " + "; ".join(recovery_errors)
                self._write(payload)
                if recovery_errors:
                    raise SnapshotRestoreError(payload["error"]) from error
                raise SnapshotRestoreError(f"Snapshot restore failed; changed paths were restored: {error}") from error

    def _copy_backup(
        self,
        source: Path,
        destination: Path,
        kind: str,
        expected: str,
        temporary: Path,
        internal_root: Path,
    ) -> None:
        if temporary.exists():
            if temporary.is_dir():
                _safe_remove_tree(temporary, internal_root)
            else:
                temporary.unlink()
        temporary.parent.mkdir(parents=True, exist_ok=True)
        if kind == "directory":
            shutil.copytree(source, temporary, copy_function=shutil.copy2)
        else:
            temporary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, temporary)
        if _value_digest(temporary, kind) != expected:
            if kind == "directory":
                _safe_remove_tree(temporary, internal_root)
            else:
                temporary.unlink(missing_ok=True)
            raise SnapshotRestoreError(f"Backup restore verification failed: {destination.name}")
        os.replace(temporary, destination)

    def _rollback_payload(self, payload: Mapping[str, Any], *, verify_committed: bool) -> list[str]:
        errors: list[str] = []
        for raw in reversed(payload.get("operations", [])):
            if not isinstance(raw, dict):
                errors.append("Invalid restore operation")
                continue
            try:
                paths = self._validate_operation(raw)
                kind = str(raw.get("kind"))
                state = str(raw.get("state", ""))
                if state in {"staging", "prepared", "rolled_back"}:
                    continue
                destination = paths["destination"]
                current = _value_digest(destination, kind) if destination.exists() else None
                original = raw.get("original_digest")
                staged = raw.get("staged_digest")
                if verify_committed and current != staged:
                    raise SnapshotRestoreError(f"Restored path changed after commit: {destination}")
                if current not in {original, staged, None}:
                    raise SnapshotRestoreError(f"Restore target changed outside this transaction: {destination}")
                changed_by_transaction = (
                    state == "replaced"
                    or paths["backup"].exists()
                    or (original is None and current == staged)
                    or current != original
                )
                if current == staged and current != original and changed_by_transaction:
                    paths["quarantine"].parent.mkdir(parents=True, exist_ok=True)
                    if paths["quarantine"].exists():
                        raise SnapshotRestoreError("Restore quarantine already contains this path")
                    os.replace(destination, paths["quarantine"])
                if original is not None:
                    if not destination.exists():
                        if not paths["backup"].exists() or _value_digest(paths["backup"], kind) != original:
                            raise SnapshotRestoreError(f"Restore backup is missing or invalid: {destination.name}")
                        self._copy_backup(
                            paths["backup"],
                            destination,
                            kind,
                            str(original),
                            paths["stage"],
                            paths["internal_root"],
                        )
                raw["state"] = "rolled_back"
            except (OSError, SnapshotRestoreError) as error:
                errors.append(str(error))
        self._cleanup_staging(payload)
        return errors

    def _cleanup_staging(self, payload: Mapping[str, Any]) -> None:
        roots: set[Path] = set()
        for raw in payload.get("operations", []):
            if not isinstance(raw, dict):
                continue
            try:
                paths = self._validate_operation(raw)
            except SnapshotRestoreError:
                continue
            stage = paths["stage"]
            if stage.exists():
                if stage.is_dir():
                    _safe_remove_tree(stage, paths["internal_root"])
                else:
                    stage.unlink()
            roots.add(stage.parent)
        for root in sorted(roots, key=lambda value: len(value.parts), reverse=True):
            try:
                root.rmdir()
            except OSError:
                pass

    def _result(self, payload: Mapping[str, Any]) -> SnapshotRestoreResult:
        operations = [value for value in payload.get("operations", []) if isinstance(value, dict)]
        backups = tuple(
            Path(str(value["backup"]))
            for value in operations
            if Path(str(value["backup"])).exists()
        )
        restored_save = next(
            (Path(str(value["destination"])) for value in operations if value.get("category") == "save"),
            None,
        )
        return SnapshotRestoreResult(
            str(payload["transaction_id"]),
            sum(1 for value in operations if value.get("kind") == "file"),
            sum(1 for value in operations if value.get("kind") == "directory"),
            backups,
            restored_save,
        )

    def rollback(self, transaction_id: str) -> SnapshotRestoreResult:
        with self._lock:
            self._ensure_processes_closed()
            payload = self._load(transaction_id)
            if payload.get("phase") != "committed":
                raise SnapshotRestoreError("Only a committed snapshot restore can be undone")
            errors = self._rollback_payload(payload, verify_committed=True)
            payload["phase"] = "rollback_failed" if errors else "rolled_back"
            payload["error"] = "; ".join(errors) if errors else None
            self._write(payload)
            if errors:
                raise SnapshotRestoreError(payload["error"])
            return self._result(payload)

    def latest_recoverable(self) -> str | None:
        """Return the newest committed restore transaction, if one remains undoable."""
        with self._lock:
            if not self.journal_directory.is_dir():
                return None
            matches: list[tuple[str, str]] = []
            for path in self.journal_directory.glob("*.json"):
                try:
                    payload = self._load(path.stem)
                except SnapshotRestoreError:
                    continue
                if payload.get("phase") == "committed":
                    matches.append((str(payload.get("updated_at_utc", "")), path.stem))
            return max(matches)[1] if matches else None

    def recover_pending(self) -> list[SnapshotRestoreRecoveryResult]:
        with self._lock:
            if not self.journal_directory.is_dir():
                return []
            results: list[SnapshotRestoreRecoveryResult] = []
            for path in sorted(self.journal_directory.glob("*.json")):
                try:
                    payload = self._load(path.stem)
                    phase = str(payload.get("phase", ""))
                    if phase in FINISHED_PHASES:
                        continue
                    if phase in {"preparing", "prepared"}:
                        self._cleanup_staging(payload)
                        payload["phase"] = "cancelled"
                        payload["error"] = "Cancelled during startup recovery before replacement"
                        self._write(payload)
                        results.append(SnapshotRestoreRecoveryResult(path.stem, "cancelled", "Unused restore staging removed"))
                    elif phase in ACTIVE_PHASES:
                        self._ensure_processes_closed()
                        errors = self._rollback_payload(payload, verify_committed=False)
                        payload["phase"] = "rollback_failed" if errors else "rolled_back"
                        payload["error"] = "; ".join(errors) if errors else None
                        self._write(payload)
                        results.append(
                            SnapshotRestoreRecoveryResult(
                                path.stem,
                                "error" if errors else "rolled_back",
                                payload["error"] or "Interrupted snapshot restore was rolled back",
                            )
                        )
                    else:
                        results.append(SnapshotRestoreRecoveryResult(path.stem, "error", f"Unknown restore phase: {phase}"))
                except (OSError, SnapshotRestoreError) as error:
                    results.append(SnapshotRestoreRecoveryResult(path.stem, "error", str(error)))
            return results
