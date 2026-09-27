"""Persistent install baselines for verified CK3 mod archives."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping

from mod_scan_core import ArchiveBaseline, ArchiveRecord, sha256_file


LEDGER_SCHEMA_VERSION = 1


class LedgerError(RuntimeError):
    """Raised when the baseline ledger is invalid or cannot be updated safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def baseline_from_payload(mod_id: str, value: object) -> ArchiveBaseline:
    if not isinstance(value, dict):
        raise LedgerError(f"Invalid baseline entry for Workshop item {mod_id}")
    try:
        archive_path = Path(value["archive_path"])
        archive_sha256 = str(value["archive_sha256"])
        workshop_updated = int(value["workshop_updated"])
        recorded_at_utc = str(value["recorded_at_utc"])
        source = str(value["source"])
    except (KeyError, TypeError, ValueError) as error:
        raise LedgerError(f"Invalid baseline entry for Workshop item {mod_id}") from error
    if (
        not mod_id.isdigit()
        or len(archive_sha256) != 64
        or any(character not in "0123456789abcdef" for character in archive_sha256.lower())
        or workshop_updated <= 0
        or not recorded_at_utc
        or not source
    ):
        raise LedgerError(f"Invalid baseline entry for Workshop item {mod_id}")
    return ArchiveBaseline(
        mod_id=mod_id,
        archive_path=archive_path,
        archive_sha256=archive_sha256,
        workshop_updated=workshop_updated,
        recorded_at_utc=recorded_at_utc,
        source=source,
    )


def baseline_to_payload(record: ArchiveBaseline) -> dict[str, object]:
    return {
        **asdict(record),
        "archive_path": str(record.archive_path),
    }


class InstallLedger:
    """Load and atomically persist exact archive-to-Workshop baselines."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, ArchiveBaseline]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise LedgerError(f"Could not read baseline ledger: {error}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != LEDGER_SCHEMA_VERSION:
            raise LedgerError("Unsupported or invalid baseline ledger")
        records = payload.get("records")
        if not isinstance(records, dict):
            raise LedgerError("Baseline ledger records are invalid")
        return {
            str(mod_id): baseline_from_payload(str(mod_id), value)
            for mod_id, value in records.items()
        }

    def _save(self, records: Mapping[str, ArchiveBaseline]) -> None:
        payload = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "updated_at_utc": _utc_now(),
            "records": {
                mod_id: baseline_to_payload(record)
                for mod_id, record in sorted(records.items(), key=lambda pair: int(pair[0]))
            },
        }
        try:
            _atomic_json_write(self.path, payload)
        except OSError as error:
            raise LedgerError(f"Could not save baseline ledger: {error}") from error

    def record_archive(
        self,
        archive: ArchiveRecord,
        workshop_updated: int,
        *,
        source: str,
    ) -> ArchiveBaseline:
        if archive.status != "ck3_mod" or not archive.mod_id:
            raise LedgerError("Only a validated CK3 archive can be recorded")
        if not isinstance(workshop_updated, int) or workshop_updated <= 0:
            raise LedgerError("Workshop update timestamp is invalid")
        try:
            digest = sha256_file(archive.archive_path)
        except OSError as error:
            raise LedgerError(f"Could not hash archive: {error}") from error
        baseline = ArchiveBaseline(
            mod_id=archive.mod_id,
            archive_path=archive.archive_path.resolve(),
            archive_sha256=digest,
            workshop_updated=workshop_updated,
            recorded_at_utc=_utc_now(),
            source=source,
        )
        records = self.load()
        records[archive.mod_id] = baseline
        self._save(records)
        return baseline

    def replace_record(self, mod_id: str, baseline: ArchiveBaseline | None) -> None:
        records = self.load()
        if baseline is None:
            records.pop(mod_id, None)
        else:
            if baseline.mod_id != mod_id:
                raise LedgerError("Baseline Workshop ID mismatch")
            records[mod_id] = baseline
        self._save(records)
