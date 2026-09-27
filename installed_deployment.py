"""Recoverable deployment of validated CK3 archives into installed mod folders."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from install_ledger import _atomic_json_write
from mod_scan_core import inspect_mod_archive, sha256_file
from playset_state import PlaysetMod
from update_impact import ImpactError, inspect_archive_structure


DEPLOYMENT_SCHEMA_VERSION = 1
ACTIVE_PHASES = {
    "content_swap_started",
    "content_backed_up",
    "content_replaced",
    "descriptor_swap_started",
    "descriptor_backed_up",
    "descriptor_replaced",
    "rollback_failed",
}
FINISHED_PHASES = {"committed", "rolled_back", "cancelled"}
BLOCKED_PROCESS_NAMES = {
    "ck3.exe",
    "bootstrapper-v2.exe",
    "dowser.exe",
    "ironymodmanager.exe",
    "paradox launcher.exe",
    "paradox launcher v2.exe",
}
PROCESS_CHECK_UNAVAILABLE = "<process check unavailable>"
DESCRIPTOR_PATH_LINE = re.compile(r"(?mi)^\s*(?:path|archive)\s*=.*(?:\r?\n|$)")
MAX_DESCRIPTOR_BYTES = 2 * 1024 * 1024


class DeploymentError(RuntimeError):
    """Raised when installed content cannot be deployed or recovered safely."""


@dataclass(frozen=True)
class PreparedInstalledDeployment:
    transaction_id: str
    mod_id: str
    target_directory: Path
    descriptor_path: Path


@dataclass(frozen=True)
class InstalledDeploymentResult:
    transaction_id: str
    mod_id: str
    target_directory: Path
    descriptor_path: Path
    backup_directory: Path | None
    backup_descriptor: Path | None


@dataclass(frozen=True)
class DeploymentRecoveryResult:
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
        raise DeploymentError("Refused to remove an unsafe internal directory")
    shutil.rmtree(path)


def _directory_manifest(directory: Path) -> dict[str, tuple[int, str]]:
    if not directory.is_dir() or directory.is_symlink():
        raise DeploymentError(f"Installed mod folder is missing or unsafe: {directory}")
    manifest: dict[str, tuple[int, str]] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise DeploymentError(f"Symbolic-link installed content was refused: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        key = relative.casefold()
        if key in manifest:
            raise DeploymentError(f"Installed folder contains a case-colliding path: {relative}")
        try:
            manifest[key] = (path.stat().st_size, sha256_file(path))
        except OSError as error:
            raise DeploymentError(f"Could not verify installed content: {error}") from error
    return manifest


def _manifest_digest(manifest: Mapping[str, tuple[int, str]]) -> str:
    digest = hashlib.sha256()
    for relative, (size, file_hash) in sorted(manifest.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _directory_digest(directory: Path) -> str:
    return _manifest_digest(_directory_manifest(directory))


def running_blocked_processes() -> tuple[str, ...]:
    """Return CK3/mod-manager process names that should be closed before deployment."""
    if sys.platform != "win32":
        return ()

    class ProcessEntry(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    except (AttributeError, OSError):
        return (PROCESS_CHECK_UNAVAILABLE,)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot in (None, invalid_handle):
        return (PROCESS_CHECK_UNAVAILABLE,)
    found: set[str] = set()
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(ProcessEntry)
    try:
        available = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while available:
            process_name = str(entry.szExeFile)
            if process_name.casefold() in BLOCKED_PROCESS_NAMES:
                found.add(process_name)
            available = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(sorted(found, key=str.casefold))


class InstalledDeploymentManager:
    """Stage, atomically swap, verify, and roll back an enabled mod installation."""

    def __init__(
        self,
        journal_directory: Path,
        process_check: Callable[[], tuple[str, ...]] = running_blocked_processes,
    ):
        self.journal_directory = Path(journal_directory)
        self.process_check = process_check
        self._lock = threading.RLock()

    def _journal_path(self, transaction_id: str) -> Path:
        if len(transaction_id) != 32 or any(value not in "0123456789abcdef" for value in transaction_id):
            raise DeploymentError("Invalid installed-deployment transaction ID")
        return self.journal_directory / f"{transaction_id}.json"

    def _write_journal(self, payload: dict[str, Any]) -> None:
        payload["updated_at_utc"] = _utc_now()
        try:
            _atomic_json_write(self._journal_path(str(payload["transaction_id"])), payload)
        except (KeyError, OSError) as error:
            raise DeploymentError(f"Could not save installed-deployment journal: {error}") from error

    def _load_journal(self, transaction_id: str) -> dict[str, Any]:
        try:
            payload = json.loads(self._journal_path(transaction_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise DeploymentError(f"Could not read installed-deployment journal: {error}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != DEPLOYMENT_SCHEMA_VERSION:
            raise DeploymentError("Installed-deployment journal is invalid or unsupported")
        if payload.get("transaction_id") != transaction_id:
            raise DeploymentError("Installed-deployment journal ID mismatch")
        return payload

    def transaction_phase(self, transaction_id: str) -> str:
        return str(self._load_journal(transaction_id).get("phase", ""))

    def _paths(self, payload: Mapping[str, Any]) -> dict[str, Path]:
        required = (
            "data_root",
            "internal_root",
            "target_directory",
            "descriptor_path",
            "stage_root",
            "staged_content",
            "staged_descriptor",
            "backup_directory",
            "backup_descriptor",
            "quarantine_directory",
            "quarantine_descriptor",
        )
        try:
            paths = {name: Path(str(payload[name])) for name in required}
        except KeyError as error:
            raise DeploymentError("Installed-deployment journal is missing required paths") from error
        data_root = paths["data_root"].resolve()
        internal_root = paths["internal_root"].resolve()
        mod_root = data_root / "mod"
        if internal_root != data_root / ".ck3-mod-updater":
            raise DeploymentError("Installed-deployment internal root is invalid")
        if not _is_within(paths["target_directory"], mod_root):
            raise DeploymentError("Installed mod target is outside the CK3 mod folder")
        if not _is_within(paths["descriptor_path"], mod_root):
            raise DeploymentError("Installed descriptor target is outside the CK3 mod folder")
        for name in required[4:]:
            if not _is_within(paths[name], internal_root):
                raise DeploymentError("Installed-deployment work path is outside its private directory")
        return paths

    def _ensure_processes_closed(self) -> None:
        running = self.process_check()
        if PROCESS_CHECK_UNAVAILABLE in running:
            raise DeploymentError(
                "Could not verify whether CK3 or a mod manager is running; no installed files were changed"
            )
        if running:
            raise DeploymentError(
                "Close CK3, Irony Mod Manager, and the Paradox launcher before changing installed "
                "mod files. Still running: " + ", ".join(running)
            )

    def prepare(
        self,
        candidate_archive: Path,
        playset_mod: PlaysetMod,
        data_directory: Path,
    ) -> PreparedInstalledDeployment:
        """Extract and verify a candidate into private same-volume staging."""
        with self._lock:
            self._ensure_processes_closed()
            if not playset_mod.mod_id or not playset_mod.mod_id.isdigit():
                raise DeploymentError("Enabled mod does not have a valid Workshop ID")
            if not playset_mod.directory or not playset_mod.descriptor_path:
                raise DeploymentError("Enabled mod is missing its installed folder or descriptor path")
            data_root = Path(data_directory).resolve()
            mod_root = data_root / "mod"
            target = Path(playset_mod.directory).resolve()
            descriptor = Path(playset_mod.descriptor_path).resolve()
            if not _is_within(target, mod_root) or not _is_within(descriptor, mod_root):
                raise DeploymentError("Enabled mod paths are outside the configured CK3 mod folder")
            if _is_within(descriptor, target):
                raise DeploymentError("Enabled mod descriptor must be separate from its installed content folder")
            if target == mod_root or target.is_symlink() or descriptor.is_symlink():
                raise DeploymentError("Enabled mod uses an unsafe installed path")
            if descriptor.exists() and not descriptor.is_file():
                raise DeploymentError("Enabled mod descriptor path is not a regular file")

            candidate = Path(candidate_archive).resolve()
            if not candidate.is_file() or candidate.is_symlink():
                raise DeploymentError("The update archive is missing or is a symbolic link")
            try:
                structure = inspect_archive_structure(candidate)
            except ImpactError as error:
                raise DeploymentError(str(error)) from error
            record = inspect_mod_archive(candidate)
            if record.status != "ck3_mod" or record.mod_id != playset_mod.mod_id or not record.descriptor_path:
                raise DeploymentError("The update archive does not match the enabled mod")

            try:
                original_manifest = _directory_manifest(target) if target.exists() else None
                original_digest = _manifest_digest(original_manifest) if original_manifest is not None else None
                original_bytes = (
                    sum(size for size, _file_hash in original_manifest.values())
                    if original_manifest is not None
                    else 0
                )
                original_descriptor_hash = sha256_file(descriptor) if descriptor.is_file() else None
            except OSError as error:
                raise DeploymentError(f"Could not verify the existing installed mod: {error}") from error

            internal_root = data_root / ".ck3-mod-updater"
            if internal_root.is_symlink():
                raise DeploymentError("Private installed-update storage cannot be a symbolic link")
            transaction_id = uuid4().hex
            stage_root = internal_root / "staging" / transaction_id
            staged_content = stage_root / "content"
            staged_descriptor = stage_root / "external.mod"
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_root = internal_root / "backups" / playset_mod.mod_id / f"{timestamp}_{transaction_id}"
            backup_directory = backup_root / "content"
            backup_descriptor = backup_root / "external.mod"
            quarantine_root = internal_root / "quarantine" / playset_mod.mod_id / transaction_id
            quarantine_directory = quarantine_root / "content"
            quarantine_descriptor = quarantine_root / "external.mod"

            required_bytes = structure.total_uncompressed_bytes + original_bytes + 64 * 1024 * 1024
            try:
                internal_root.mkdir(parents=True, exist_ok=True)
                if shutil.disk_usage(internal_root).free < required_bytes:
                    raise DeploymentError("Not enough free space to stage and verify the installed mod update")
                staged_content.mkdir(parents=True)
                descriptor_name = PurePosixPath(record.descriptor_path)
                content_prefix = descriptor_name.parent
                external_bytes: bytes | None = None
                with ZipFile(candidate) as archive:
                    infos = {info.filename.replace("\\", "/"): info for info in archive.infolist()}
                    external_candidates = [
                        name
                        for name in infos
                        if PurePosixPath(name).parent == PurePosixPath(".")
                        and PurePosixPath(name).suffix.casefold() == ".mod"
                    ]
                    descriptor_info = infos.get(record.descriptor_path)
                    if descriptor_info is None:
                        raise DeploymentError("Validated archive descriptor disappeared during staging")
                    if descriptor_info.file_size > MAX_DESCRIPTOR_BYTES:
                        raise DeploymentError("Archive descriptor is unexpectedly large")
                    external_bytes = archive.read(descriptor_info)

                    extracted = 0
                    for name, info in infos.items():
                        member = PurePosixPath(name)
                        if info.is_dir():
                            continue
                        try:
                            relative = member.relative_to(content_prefix)
                        except ValueError:
                            continue
                        if (
                            content_prefix == PurePosixPath(".")
                            and member in {PurePosixPath(value) for value in external_candidates}
                            and member.name.casefold() != "descriptor.mod"
                        ):
                            continue
                        destination = staged_content / Path(*relative.parts)
                        if not _is_within(destination, staged_content):
                            raise DeploymentError("Archive extraction escaped private staging")
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info) as source, destination.open("xb") as output:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                        extracted += info.file_size
                    if extracted <= 0:
                        raise DeploymentError("The update archive has no installable mod content")

                internal_descriptor = staged_content / "descriptor.mod"
                if not internal_descriptor.is_file():
                    internal_descriptor.write_bytes(external_bytes)

                descriptor_text = external_bytes.decode("utf-8-sig", errors="replace")
                descriptor_text = DESCRIPTOR_PATH_LINE.sub("", descriptor_text).rstrip()
                relative_target = target.relative_to(data_root).as_posix()
                descriptor_text += f'\npath="{relative_target}"\n'
                staged_descriptor.write_text(descriptor_text, encoding="utf-8", newline="\n")

                staged_digest = _directory_digest(staged_content)
                staged_descriptor_hash = sha256_file(staged_descriptor)
            except (BadZipFile, OSError, UnicodeError, DeploymentError) as error:
                if stage_root.exists():
                    _safe_remove_tree(stage_root, internal_root)
                if isinstance(error, DeploymentError):
                    raise
                raise DeploymentError(f"Could not stage installed mod content: {error}") from error

            payload: dict[str, Any] = {
                "schema_version": DEPLOYMENT_SCHEMA_VERSION,
                "transaction_id": transaction_id,
                "phase": "prepared",
                "created_at_utc": _utc_now(),
                "mod_id": playset_mod.mod_id,
                "data_root": str(data_root),
                "internal_root": str(internal_root),
                "target_directory": str(target),
                "descriptor_path": str(descriptor),
                "stage_root": str(stage_root),
                "staged_content": str(staged_content),
                "staged_descriptor": str(staged_descriptor),
                "backup_directory": str(backup_directory),
                "backup_descriptor": str(backup_descriptor),
                "quarantine_directory": str(quarantine_directory),
                "quarantine_descriptor": str(quarantine_descriptor),
                "original_content_digest": original_digest,
                "original_descriptor_sha256": original_descriptor_hash,
                "staged_content_digest": staged_digest,
                "staged_descriptor_sha256": staged_descriptor_hash,
                "error": None,
            }
            try:
                self._write_journal(payload)
            except DeploymentError:
                _safe_remove_tree(stage_root, internal_root)
                raise
            return PreparedInstalledDeployment(
                transaction_id,
                playset_mod.mod_id,
                target,
                descriptor,
            )

    def apply(self, transaction_id: str) -> InstalledDeploymentResult:
        """Replace staged installed content and keep verified backups indefinitely."""
        with self._lock:
            self._ensure_processes_closed()
            payload = self._load_journal(transaction_id)
            if payload.get("phase") != "prepared":
                raise DeploymentError(f"Installed deployment is not prepared: {payload.get('phase')}")
            paths = self._paths(payload)
            target = paths["target_directory"]
            descriptor = paths["descriptor_path"]
            staged_content = paths["staged_content"]
            staged_descriptor = paths["staged_descriptor"]
            backup_directory = paths["backup_directory"]
            backup_descriptor = paths["backup_descriptor"]
            try:
                original_digest = payload.get("original_content_digest")
                current_digest = _directory_digest(target) if target.exists() else None
                if current_digest != original_digest:
                    raise DeploymentError("Installed mod folder changed after the update was prepared")
                original_descriptor = payload.get("original_descriptor_sha256")
                current_descriptor = sha256_file(descriptor) if descriptor.is_file() else None
                if current_descriptor != original_descriptor:
                    raise DeploymentError("Installed mod descriptor changed after the update was prepared")
                if _directory_digest(staged_content) != payload.get("staged_content_digest"):
                    raise DeploymentError("Staged installed content changed after validation")
                if sha256_file(staged_descriptor) != payload.get("staged_descriptor_sha256"):
                    raise DeploymentError("Staged installed descriptor changed after validation")

                backup_directory.parent.mkdir(parents=True, exist_ok=True)
                payload["phase"] = "content_swap_started"
                self._write_journal(payload)
                if target.exists():
                    os.replace(target, backup_directory)
                    if _directory_digest(backup_directory) != original_digest:
                        raise DeploymentError("Installed mod folder backup verification failed")
                payload["phase"] = "content_backed_up"
                self._write_journal(payload)

                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_content, target)
                payload["phase"] = "content_replaced"
                self._write_journal(payload)

                backup_descriptor.parent.mkdir(parents=True, exist_ok=True)
                payload["phase"] = "descriptor_swap_started"
                self._write_journal(payload)
                if descriptor.exists():
                    os.replace(descriptor, backup_descriptor)
                    if sha256_file(backup_descriptor) != original_descriptor:
                        raise DeploymentError("Installed descriptor backup verification failed")
                payload["phase"] = "descriptor_backed_up"
                self._write_journal(payload)

                descriptor.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_descriptor, descriptor)
                payload["phase"] = "descriptor_replaced"
                self._write_journal(payload)

                if _directory_digest(target) != payload.get("staged_content_digest"):
                    raise DeploymentError("Installed mod content failed post-replacement verification")
                if sha256_file(descriptor) != payload.get("staged_descriptor_sha256"):
                    raise DeploymentError("Installed mod descriptor failed post-replacement verification")
                payload["phase"] = "committed"
                payload["error"] = None
                self._write_journal(payload)
                stage_root = paths["stage_root"]
                if stage_root.exists():
                    _safe_remove_tree(stage_root, paths["internal_root"])
                return self._result(payload, paths)
            except (OSError, DeploymentError) as error:
                payload["error"] = str(error)
                if payload.get("phase") in ACTIVE_PHASES:
                    try:
                        self._restore(payload, verify_current=False)
                    except (OSError, DeploymentError) as rollback_error:
                        payload["phase"] = "rollback_failed"
                        payload["error"] = f"{error}; rollback failed: {rollback_error}"
                        self._write_journal(payload)
                        raise DeploymentError(payload["error"]) from error
                    raise DeploymentError(
                        f"Installed deployment failed; previous installed files were restored: {error}"
                    ) from error
                payload["phase"] = "cancelled"
                self._write_journal(payload)
                if paths["stage_root"].exists():
                    _safe_remove_tree(paths["stage_root"], paths["internal_root"])
                raise DeploymentError(f"Installed deployment cancelled before replacement: {error}") from error

    def _result(self, payload: Mapping[str, Any], paths: Mapping[str, Path]) -> InstalledDeploymentResult:
        return InstalledDeploymentResult(
            str(payload["transaction_id"]),
            str(payload["mod_id"]),
            paths["target_directory"],
            paths["descriptor_path"],
            paths["backup_directory"] if paths["backup_directory"].exists() else None,
            paths["backup_descriptor"] if paths["backup_descriptor"].exists() else None,
        )

    def _copy_directory_verified(
        self,
        source: Path,
        destination: Path,
        expected_digest: str,
        temporary: Path,
    ) -> None:
        try:
            if temporary.exists():
                _safe_remove_tree(temporary, temporary.parent.parent)
            temporary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, temporary, copy_function=shutil.copy2)
            if _directory_digest(temporary) != expected_digest:
                raise DeploymentError("Restored installed folder failed hash verification")
            os.replace(temporary, destination)
        except (OSError, DeploymentError):
            if temporary.exists():
                _safe_remove_tree(temporary, temporary.parent.parent)
            raise

    def _quarantine_current(self, current: Path, quarantine: Path) -> None:
        if not current.exists():
            return
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        if quarantine.exists():
            raise DeploymentError("Recovery quarantine already contains a path for this transaction")
        os.replace(current, quarantine)

    def _restore(self, payload: dict[str, Any], *, verify_current: bool) -> None:
        paths = self._paths(payload)
        phase = str(payload.get("phase", ""))
        target = paths["target_directory"]
        descriptor = paths["descriptor_path"]
        original_digest = payload.get("original_content_digest")
        staged_digest = str(payload.get("staged_content_digest", ""))
        current_digest = _directory_digest(target) if target.exists() else None
        allowed_content = {original_digest, staged_digest, None}
        if current_digest not in allowed_content:
            raise DeploymentError("Installed mod folder changed outside this update; rollback was refused")
        if verify_current and current_digest != staged_digest:
            raise DeploymentError("Installed mod folder no longer matches the committed update")

        current_descriptor = sha256_file(descriptor) if descriptor.is_file() else None
        original_descriptor = payload.get("original_descriptor_sha256")
        staged_descriptor = str(payload.get("staged_descriptor_sha256", ""))
        if current_descriptor not in {original_descriptor, staged_descriptor, None}:
            raise DeploymentError("Installed descriptor changed outside this update; rollback was refused")
        if verify_current and current_descriptor != staged_descriptor:
            raise DeploymentError("Installed descriptor no longer matches the committed update")

        content_was_replaced = (
            phase in {"content_replaced", "descriptor_swap_started", "descriptor_backed_up", "descriptor_replaced", "committed", "rollback_failed"}
            or paths["backup_directory"].exists()
            or (original_digest is None and current_digest == staged_digest)
            or current_digest != original_digest
        )
        if (
            current_digest == staged_digest
            and current_digest != original_digest
            and content_was_replaced
        ):
            self._quarantine_current(target, paths["quarantine_directory"])
        if original_digest is not None:
            if not target.exists():
                backup = paths["backup_directory"]
                if not backup.is_dir() or _directory_digest(backup) != original_digest:
                    raise DeploymentError("Installed mod folder backup is missing or failed verification")
                self._copy_directory_verified(
                    backup,
                    target,
                    str(original_digest),
                    paths["staged_content"],
                )
        elif target.exists() and current_digest != original_digest:
            self._quarantine_current(target, paths["quarantine_directory"])

        descriptor_was_replaced = (
            phase in {"descriptor_replaced", "committed", "rollback_failed"}
            or paths["backup_descriptor"].exists()
            or (original_descriptor is None and current_descriptor == staged_descriptor)
            or current_descriptor != original_descriptor
        )
        if (
            current_descriptor == staged_descriptor
            and current_descriptor != original_descriptor
            and descriptor_was_replaced
        ):
            self._quarantine_current(descriptor, paths["quarantine_descriptor"])
        if original_descriptor is not None:
            if not descriptor.exists():
                backup_descriptor = paths["backup_descriptor"]
                if not backup_descriptor.is_file() or sha256_file(backup_descriptor) != original_descriptor:
                    raise DeploymentError("Installed descriptor backup is missing or failed verification")
                descriptor.parent.mkdir(parents=True, exist_ok=True)
                temporary = paths["staged_descriptor"]
                temporary.parent.mkdir(parents=True, exist_ok=True)
                if temporary.exists():
                    if temporary.is_dir():
                        _safe_remove_tree(temporary, paths["internal_root"])
                    else:
                        temporary.unlink()
                shutil.copy2(backup_descriptor, temporary)
                if sha256_file(temporary) != original_descriptor:
                    temporary.unlink(missing_ok=True)
                    raise DeploymentError("Restored installed descriptor failed hash verification")
                os.replace(temporary, descriptor)

        payload["phase"] = "rolled_back"
        payload["error"] = None
        payload["rolled_back_at_utc"] = _utc_now()
        self._write_journal(payload)
        if paths["stage_root"].exists():
            _safe_remove_tree(paths["stage_root"], paths["internal_root"])

    def rollback(self, transaction_id: str) -> InstalledDeploymentResult:
        with self._lock:
            self._ensure_processes_closed()
            payload = self._load_journal(transaction_id)
            if payload.get("phase") != "committed":
                raise DeploymentError("Only a committed installed deployment can be undone")
            paths = self._paths(payload)
            self._restore(payload, verify_current=True)
            return self._result(payload, paths)

    def cancel_prepared(self, transaction_id: str) -> None:
        with self._lock:
            payload = self._load_journal(transaction_id)
            if payload.get("phase") in {"cancelled", "rolled_back"}:
                return
            if payload.get("phase") != "prepared":
                raise DeploymentError("Only an unapplied installed deployment can be cancelled")
            paths = self._paths(payload)
            if paths["stage_root"].exists():
                _safe_remove_tree(paths["stage_root"], paths["internal_root"])
            payload["phase"] = "cancelled"
            payload["error"] = "Cancelled before installed replacement"
            self._write_journal(payload)

    def recover_pending(self) -> list[DeploymentRecoveryResult]:
        with self._lock:
            if not self.journal_directory.is_dir():
                return []
            results: list[DeploymentRecoveryResult] = []
            for journal in sorted(self.journal_directory.glob("*.json")):
                transaction_id = journal.stem
                try:
                    payload = self._load_journal(transaction_id)
                    phase = str(payload.get("phase", ""))
                    if phase in FINISHED_PHASES:
                        continue
                    if phase == "prepared":
                        self.cancel_prepared(transaction_id)
                        results.append(DeploymentRecoveryResult(transaction_id, "cancelled", "Unused installed staging removed"))
                    elif phase in ACTIVE_PHASES:
                        self._ensure_processes_closed()
                        self._restore(payload, verify_current=False)
                        results.append(DeploymentRecoveryResult(transaction_id, "rolled_back", "Interrupted installed replacement restored"))
                    else:
                        results.append(DeploymentRecoveryResult(transaction_id, "error", f"Unknown deployment phase: {phase}"))
                except (OSError, DeploymentError) as error:
                    results.append(DeploymentRecoveryResult(transaction_id, "error", str(error)))
            return results
