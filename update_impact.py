"""Inspect structural and file-level impact before replacing a CK3 mod archive."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import stat
from zipfile import BadZipFile, ZipFile, ZipInfo


MAX_ARCHIVE_ENTRIES = 100_000
MAX_DESCRIPTOR_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000
WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
SUSPICIOUS_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".js",
    ".msi",
    ".ps1",
    ".scr",
    ".vbs",
}
REPLACE_PATH = re.compile(r'(?mi)^\s*replace_path\s*=\s*"([^"]+)"')


class ImpactError(RuntimeError):
    """Raised when an archive cannot be safely inspected."""


@dataclass(frozen=True)
class ArchiveStructure:
    path: Path
    files: dict[str, tuple[int, int]]
    total_uncompressed_bytes: int
    replace_paths: tuple[str, ...]
    suspicious_files: tuple[str, ...]


@dataclass(frozen=True)
class UpdateImpact:
    current_archive: Path
    candidate_archive: Path
    added_files: tuple[str, ...]
    removed_files: tuple[str, ...]
    changed_files: tuple[str, ...]
    unchanged_count: int
    current_uncompressed_bytes: int
    candidate_uncompressed_bytes: int
    added_replace_paths: tuple[str, ...]
    removed_replace_paths: tuple[str, ...]
    suspicious_files: tuple[str, ...]


def _safe_member_name(info: ZipInfo) -> str | None:
    raw = info.filename.replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or re.match(r"^[A-Za-z]:", raw)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ImpactError(f"Archive contains an unsafe path: {info.filename}")
    for part in path.parts:
        if part.endswith((" ", ".")) or ":" in part:
            raise ImpactError(f"Archive contains a Windows-unsafe path: {info.filename}")
        reserved_stem = part.split(".", 1)[0].casefold()
        if reserved_stem in WINDOWS_RESERVED_NAMES:
            raise ImpactError(f"Archive contains a reserved Windows path: {info.filename}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ImpactError(f"Archive contains a symbolic link: {info.filename}")
    if info.flag_bits & 0x1:
        raise ImpactError(f"Archive contains an encrypted entry: {info.filename}")
    return path.as_posix().rstrip("/") if not info.is_dir() else None


def inspect_archive_structure(path: Path) -> ArchiveStructure:
    """Read ZIP structure without extracting it and reject unsafe entries."""
    path = Path(path).resolve()
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise ImpactError("Archive contains too many entries to inspect safely")
            files: dict[str, tuple[int, int]] = {}
            path_spellings: dict[str, str] = {}
            suspicious: list[str] = []
            descriptors: list[str] = []
            total_size = 0
            for info in entries:
                name = _safe_member_name(info)
                if name is None:
                    continue
                parts = PurePosixPath(name).parts
                for index in range(1, len(parts) + 1):
                    spelling = PurePosixPath(*parts[:index]).as_posix()
                    spelling_key = spelling.casefold()
                    previous_spelling = path_spellings.get(spelling_key)
                    if previous_spelling is not None and previous_spelling != spelling:
                        raise ImpactError(
                            f"Archive contains case-colliding paths: {previous_spelling} and {spelling}"
                        )
                    path_spellings[spelling_key] = spelling
                if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ImpactError(f"Archive member is unexpectedly large: {name}")
                if (
                    info.file_size > 16 * 1024 * 1024
                    and info.compress_size > 0
                    and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise ImpactError(f"Archive member has an unsafe compression ratio: {name}")
                key = name.casefold()
                if key in files:
                    raise ImpactError(f"Archive contains duplicate file paths: {name}")
                files[key] = (info.CRC, info.file_size)
                total_size += info.file_size
                if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise ImpactError("Archive expands beyond the safe size limit")
                if PurePosixPath(name).suffix.casefold() in SUSPICIOUS_EXTENSIONS:
                    suspicious.append(name)
                if PurePosixPath(name).name.casefold().endswith(".mod"):
                    if info.file_size > MAX_DESCRIPTOR_BYTES:
                        raise ImpactError(f"Archive descriptor is unexpectedly large: {name}")
                    try:
                        descriptors.append(archive.read(info).decode("utf-8-sig", errors="replace"))
                    except RuntimeError as error:
                        raise ImpactError(f"Could not read archive descriptor: {name}") from error
    except (OSError, BadZipFile) as error:
        raise ImpactError(f"Could not inspect update archive: {error}") from error
    replace_paths = tuple(
        sorted(
            {
                match.group(1).strip()
                for descriptor in descriptors
                for match in REPLACE_PATH.finditer(descriptor)
                if match.group(1).strip()
            },
            key=str.casefold,
        )
    )
    return ArchiveStructure(
        path=path,
        files=files,
        total_uncompressed_bytes=total_size,
        replace_paths=replace_paths,
        suspicious_files=tuple(sorted(suspicious, key=str.casefold)),
    )


def compare_archives(current_archive: Path, candidate_archive: Path) -> UpdateImpact:
    current = inspect_archive_structure(current_archive)
    candidate = inspect_archive_structure(candidate_archive)
    current_names = set(current.files)
    candidate_names = set(candidate.files)
    shared = current_names & candidate_names
    changed = tuple(sorted((name for name in shared if current.files[name] != candidate.files[name])))
    unchanged_count = len(shared) - len(changed)
    return UpdateImpact(
        current_archive=current.path,
        candidate_archive=candidate.path,
        added_files=tuple(sorted(candidate_names - current_names)),
        removed_files=tuple(sorted(current_names - candidate_names)),
        changed_files=changed,
        unchanged_count=unchanged_count,
        current_uncompressed_bytes=current.total_uncompressed_bytes,
        candidate_uncompressed_bytes=candidate.total_uncompressed_bytes,
        added_replace_paths=tuple(sorted(set(candidate.replace_paths) - set(current.replace_paths))),
        removed_replace_paths=tuple(sorted(set(current.replace_paths) - set(candidate.replace_paths))),
        suspicious_files=candidate.suspicious_files,
    )


def impact_summary(impact: UpdateImpact, detail_limit: int = 8) -> str:
    """Return a concise, user-facing summary for the confirmation dialog."""
    lines = [
        f"Added files: {len(impact.added_files)}",
        f"Removed files: {len(impact.removed_files)}",
        f"Changed files: {len(impact.changed_files)}",
        f"Unchanged files: {impact.unchanged_count}",
    ]
    if impact.added_replace_paths:
        lines.append("New replace_path declarations: " + ", ".join(impact.added_replace_paths))
    if impact.removed_replace_paths:
        lines.append("Removed replace_path declarations: " + ", ".join(impact.removed_replace_paths))
    if impact.suspicious_files:
        lines.append("Executable or script files require review:")
        lines.extend(f"  • {name}" for name in impact.suspicious_files[:detail_limit])
        if len(impact.suspicious_files) > detail_limit:
            lines.append(f"  • …and {len(impact.suspicious_files) - detail_limit} more")
    return "\n".join(lines)
