"""Read-only CK3 Workshop checker core.

An unknown installed Workshop revision stays unknown. Filesystem timestamps are
intentionally excluded from update classification.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, Sequence
from zipfile import BadZipFile, ZipFile
import re

import requests


WORKSHOP_DETAILS_URL = (
    "https://api.steampowered.com/ISteamRemoteStorage/"
    "GetPublishedFileDetails/v1/"
)
MOD_ID = re.compile(r"[0-9]+\Z")
ARCHIVE_ID = re.compile(r"(?P<id>[0-9]+)(?:_|\Z)")
DESCRIPTOR_VALUE = re.compile(
    r'^\s*(?P<key>name|version|supported_version|remote_file_id)\s*=\s*'
    r'(?P<value>"[^"]*"|[^\s#]+)',
    re.MULTILINE,
)
BATCH_SIZE = 20
MAX_DESCRIPTOR_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ScanRow:
    mod_id: str
    status: str
    title: str | None = None
    workshop_updated: int | None = None
    installed_workshop_updated: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ArchiveRecord:
    """Read-only identification result for one local ZIP archive."""

    archive_path: Path
    status: str
    mod_id: str | None = None
    filename_mod_id: str | None = None
    name: str | None = None
    version: str | None = None
    supported_version: str | None = None
    descriptor_path: str | None = None
    archive_modified: int | None = None
    archive_size: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ArchiveBaseline:
    """A recorded Workshop revision bound to one exact local archive."""

    mod_id: str
    archive_path: Path
    archive_sha256: str
    workshop_updated: int
    recorded_at_utc: str
    source: str


@dataclass(frozen=True)
class ArchiveScanRow:
    """One archive plus independently reported Workshop and compatibility evidence."""

    archive: ArchiveRecord
    workshop_status: str
    workshop_title: str | None = None
    workshop_updated: int | None = None
    baseline_status: str = "unknown_local_version"
    installed_workshop_updated: int | None = None
    compatibility: str = "unknown"
    timestamp_signal: str = "not_applicable"
    duplicate_count: int = 1
    detail: str | None = None


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 digest for a local file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_descriptor(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in DESCRIPTOR_VALUE.finditer(text):
        value = match.group("value")
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        values[match.group("key")] = value
    return values


def _read_descriptor(archive: ZipFile, entry_name: str) -> dict[str, str]:
    entry = archive.getinfo(entry_name)
    if entry.file_size > MAX_DESCRIPTOR_BYTES:
        raise ValueError("Descriptor is unexpectedly large")
    with archive.open(entry) as stream:
        content = stream.read(MAX_DESCRIPTOR_BYTES + 1)
    if len(content) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("Descriptor is unexpectedly large")
    return _parse_descriptor(content.decode("utf-8-sig", errors="replace"))


def inspect_mod_archive(archive_path: Path) -> ArchiveRecord:
    """Inspect a ZIP without extracting it or trusting its internal timestamps."""
    archive_path = Path(archive_path)
    filename_match = ARCHIVE_ID.match(archive_path.stem)
    filename_mod_id = filename_match.group("id") if filename_match else None
    try:
        stat = archive_path.stat()
    except OSError as error:
        return ArchiveRecord(archive_path, "unreadable_archive", detail=str(error))

    try:
        with ZipFile(archive_path) as archive:
            entry_names = [entry.filename.replace("\\", "/") for entry in archive.infolist()]
            lower_names = [name.lower() for name in entry_names]
            wallpaper = any(
                PurePosixPath(name).name in {"project.json", "scene.pkg"}
                for name in lower_names
            )
            ck3_structure = any(
                any(part in {
                    "common", "events", "decisions", "history", "localization",
                    "gfx", "gui", "music", "sound", "map_data",
                } for part in PurePosixPath(name).parts[:-1])
                for name in lower_names
            )

            descriptor_candidates = [
                name for name in entry_names
                if PurePosixPath(name.lower()).name == "descriptor.mod"
            ]
            fallback_candidates = [
                name for name in entry_names
                if PurePosixPath(name.lower()).suffix == ".mod"
                and name not in descriptor_candidates
            ]

            chosen_name: str | None = None
            chosen_values: dict[str, str] | None = None
            descriptor_error: str | None = None
            for name in descriptor_candidates + fallback_candidates:
                try:
                    values = _read_descriptor(archive, name)
                except (KeyError, OSError, ValueError) as error:
                    descriptor_error = str(error)
                    continue
                remote_id = values.get("remote_file_id")
                if remote_id and MOD_ID.fullmatch(remote_id):
                    chosen_name = name
                    chosen_values = values
                    break

            if chosen_values is None:
                if wallpaper and not ck3_structure:
                    status = "wallpaper_engine"
                elif ck3_structure or descriptor_candidates or fallback_candidates:
                    status = "invalid_mod_descriptor"
                else:
                    status = "unrelated_archive"
                return ArchiveRecord(
                    archive_path,
                    status,
                    filename_mod_id=filename_mod_id,
                    archive_modified=int(stat.st_mtime),
                    archive_size=stat.st_size,
                    detail=descriptor_error,
                )

            remote_id = chosen_values["remote_file_id"]
            detail = None
            if filename_mod_id and filename_mod_id != remote_id:
                detail = f"Filename ID {filename_mod_id} differs from descriptor ID {remote_id}"
            return ArchiveRecord(
                archive_path,
                "ck3_mod",
                mod_id=remote_id,
                filename_mod_id=filename_mod_id,
                name=chosen_values.get("name"),
                version=chosen_values.get("version"),
                supported_version=chosen_values.get("supported_version"),
                descriptor_path=chosen_name,
                archive_modified=int(stat.st_mtime),
                archive_size=stat.st_size,
                detail=detail,
            )
    except (BadZipFile, OSError, ValueError) as error:
        return ArchiveRecord(
            archive_path,
            "unreadable_archive",
            filename_mod_id=filename_mod_id,
            archive_modified=int(stat.st_mtime),
            archive_size=stat.st_size,
            detail=str(error),
        )


def inspect_archive_directory(archive_directory: Path) -> list[ArchiveRecord]:
    """Inspect every top-level ZIP and include unrelated and broken archives."""
    archive_directory = Path(archive_directory)
    if not archive_directory.is_dir():
        raise FileNotFoundError(f"Archive directory not found: {archive_directory}")
    return [
        inspect_mod_archive(path)
        for path in sorted(archive_directory.iterdir(), key=lambda item: item.name.casefold())
        if path.is_file() and path.suffix.lower() == ".zip"
    ]


def duplicate_archive_ids(records: Sequence[ArchiveRecord]) -> dict[str, list[ArchiveRecord]]:
    """Return every Workshop ID represented by more than one local archive."""
    grouped: dict[str, list[ArchiveRecord]] = {}
    for record in records:
        if record.status == "ck3_mod" and record.mod_id:
            grouped.setdefault(record.mod_id, []).append(record)
    return {mod_id: items for mod_id, items in grouped.items() if len(items) > 1}


def declared_compatibility(supported_version: str | None, game_version: str) -> str:
    """Evaluate a descriptor's declaration; this does not prove runtime compatibility."""
    if not supported_version:
        return "unknown"
    supported_parts = supported_version.strip().split(".")
    game_parts = game_version.strip().split(".")
    if not all(part == "*" or part.isdigit() for part in supported_parts):
        return "unknown"
    for index, supported_part in enumerate(supported_parts):
        if supported_part == "*":
            continue
        game_part = game_parts[index] if index < len(game_parts) else "0"
        if not game_part.isdigit() or int(supported_part) != int(game_part):
            return "declared_incompatible"
    return "declared_compatible"


def archive_timestamp_signal(record: ArchiveRecord, workshop_updated: int | None) -> str:
    """Compare timestamps as an estimate, never as a confirmed installed revision."""
    if record.status != "ck3_mod" or not record.archive_modified:
        return "not_applicable"
    if not isinstance(workshop_updated, int) or workshop_updated <= 0:
        return "unknown"
    if workshop_updated > record.archive_modified:
        return "remote_newer_than_archive_timestamp"
    return "remote_not_newer_than_archive_timestamp"


def discover_mod_ids(mod_directory: Path) -> list[str]:
    """Find exact numeric folder names and numeric .mod stems, without recursion."""
    if not mod_directory.is_dir():
        raise FileNotFoundError(f"Mod directory not found: {mod_directory}")
    ids: set[str] = set()
    for entry in mod_directory.iterdir():
        if entry.is_dir() and MOD_ID.fullmatch(entry.name):
            ids.add(entry.name)
        elif entry.is_file() and entry.suffix.lower() == ".mod":
            if MOD_ID.fullmatch(entry.stem):
                ids.add(entry.stem)
    return sorted(ids, key=int)


def fetch_workshop_batch(session: requests.Session, mod_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Fetch public Workshop metadata. This endpoint does not require a key."""
    data: dict[str, str | int] = {"itemcount": len(mod_ids)}
    for index, mod_id in enumerate(mod_ids):
        data[f"publishedfileids[{index}]"] = mod_id
    response = session.post(WORKSHOP_DETAILS_URL, data=data, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        raise ValueError("Workshop response was not an object")
    details = payload["response"].get("publishedfiledetails")
    if not isinstance(details, list):
        raise ValueError("Workshop response did not contain a details list")
    return details


def classify_mod(
    mod_id: str,
    item: Mapping[str, Any] | None,
    installed_workshop_updated: int | None = None,
) -> ScanRow:
    """Compare a known install baseline with current Workshop item metadata."""
    if item is None:
        return ScanRow(mod_id, "scan_error", detail="No response for this ID")
    if str(item.get("publishedfileid")) != mod_id:
        return ScanRow(mod_id, "scan_error", detail="Response ID mismatch")
    if item.get("result") != 1:
        return ScanRow(
            mod_id,
            "remote_unavailable",
            detail=f"Workshop result: {item.get('result', 'missing')}",
        )
    try:
        updated = int(item["time_updated"])
        if updated <= 0:
            raise ValueError("Nonpositive update timestamp")
    except (KeyError, TypeError, ValueError):
        return ScanRow(mod_id, "scan_error", detail="Invalid Workshop update timestamp")

    title = str(item.get("title") or f"Mod {mod_id}")
    if installed_workshop_updated is None:
        status = "unknown_local_version"
    elif not isinstance(installed_workshop_updated, int) or installed_workshop_updated <= 0:
        return ScanRow(mod_id, "scan_error", title, updated, detail="Invalid installed baseline")
    elif updated > installed_workshop_updated:
        status = "changed_since_recorded_install"
    else:
        status = "no_change_since_recorded_install"
    return ScanRow(mod_id, status, title, updated, installed_workshop_updated)


def scan_mods(
    mod_directory: Path,
    installed_baselines: Mapping[str, int],
    fetch_batch: Callable[[Sequence[str]], list[dict[str, Any]]],
) -> list[ScanRow]:
    """Return one truthful result per discovered ID, including batch failures."""
    mod_ids = discover_mod_ids(mod_directory)
    rows: list[ScanRow] = []
    for start in range(0, len(mod_ids), BATCH_SIZE):
        batch = mod_ids[start : start + BATCH_SIZE]
        try:
            details = fetch_batch(batch)
            if not isinstance(details, list):
                raise ValueError("Workshop response is not a list")
        except (requests.RequestException, ValueError, TypeError) as error:
            rows.extend(
                ScanRow(mod_id, "scan_error", detail=f"Batch request failed: {error}")
                for mod_id in batch
            )
            continue
        by_id = {
            str(item["publishedfileid"]): item
            for item in details
            if isinstance(item, dict) and "publishedfileid" in item
        }
        rows.extend(
            classify_mod(mod_id, by_id.get(mod_id), installed_baselines.get(mod_id))
            for mod_id in batch
        )
    return rows


def scan_archives(
    archive_directory: Path,
    game_version: str,
    fetch_batch: Callable[[Sequence[str]], list[dict[str, Any]]],
    installed_baselines: Mapping[str, ArchiveBaseline] | None = None,
) -> list[ArchiveScanRow]:
    """Inventory archives and add Workshop evidence without claiming a local revision.

    Every ZIP remains represented in the result, including unrelated and unreadable
    files. A Workshop timestamp comparison is explicitly kept as an estimate.
    """
    records = inspect_archive_directory(archive_directory)
    installed_baselines = installed_baselines or {}
    duplicate_counts = {
        mod_id: len(items) for mod_id, items in duplicate_archive_ids(records).items()
    }
    mod_ids = sorted(
        {record.mod_id for record in records if record.status == "ck3_mod" and record.mod_id},
        key=int,
    )
    workshop_items: dict[str, Mapping[str, Any]] = {}
    lookup_errors: dict[str, str] = {}

    for start in range(0, len(mod_ids), BATCH_SIZE):
        batch = mod_ids[start : start + BATCH_SIZE]
        try:
            details = fetch_batch(batch)
            if not isinstance(details, list):
                raise ValueError("Workshop response is not a list")
        except (requests.RequestException, ValueError, TypeError) as error:
            for mod_id in batch:
                lookup_errors[mod_id] = f"Batch request failed: {error}"
            continue

        for item in details:
            if isinstance(item, dict) and "publishedfileid" in item:
                workshop_items[str(item["publishedfileid"])] = item
        for mod_id in batch:
            if mod_id not in workshop_items:
                lookup_errors[mod_id] = "No response for this ID"

    rows: list[ArchiveScanRow] = []
    for record in records:
        if record.status != "ck3_mod" or not record.mod_id:
            rows.append(
                ArchiveScanRow(
                    archive=record,
                    workshop_status="not_applicable",
                    detail=record.detail,
                )
            )
            continue

        compatibility = declared_compatibility(record.supported_version, game_version)
        error = lookup_errors.get(record.mod_id)
        if error:
            rows.append(
                ArchiveScanRow(
                    archive=record,
                    workshop_status="scan_error",
                    compatibility=compatibility,
                    duplicate_count=duplicate_counts.get(record.mod_id, 1),
                    detail=error,
                )
            )
            continue

        baseline = installed_baselines.get(record.mod_id)
        installed_workshop_updated: int | None = None
        baseline_status = "unknown_local_version"
        if baseline is not None:
            installed_workshop_updated = baseline.workshop_updated
            try:
                same_path = record.archive_path.resolve() == baseline.archive_path.resolve()
                same_hash = same_path and sha256_file(record.archive_path) == baseline.archive_sha256
            except OSError:
                same_hash = False
                baseline_status = "baseline_verification_failed"
            else:
                if not same_path or not same_hash:
                    baseline_status = "local_archive_changed"

        classified = classify_mod(
            record.mod_id,
            workshop_items.get(record.mod_id),
            installed_workshop_updated if baseline_status == "unknown_local_version" and baseline else None,
        )
        workshop_status = (
            classified.status
            if classified.status in {"remote_unavailable", "scan_error"}
            else "available"
        )
        if baseline is not None and baseline_status == "unknown_local_version":
            baseline_status = classified.status
        signal = (
            archive_timestamp_signal(record, classified.workshop_updated)
            if workshop_status == "available"
            else "unknown"
        )
        rows.append(
            ArchiveScanRow(
                archive=record,
                workshop_status=workshop_status,
                workshop_title=classified.title,
                workshop_updated=classified.workshop_updated,
                baseline_status=baseline_status,
                installed_workshop_updated=installed_workshop_updated,
                compatibility=compatibility,
                timestamp_signal=signal,
                duplicate_count=duplicate_counts.get(record.mod_id, 1),
                detail=classified.detail or record.detail,
            )
        )
    return rows
