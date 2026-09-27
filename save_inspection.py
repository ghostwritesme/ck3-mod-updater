"""Read the lightweight metadata section embedded at the start of CK3 saves."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


SAVE_READ_LIMIT = 4 * 1024 * 1024
QUOTED_VALUE = re.compile(r'"((?:[^"\\]|\\.)*)"')


class SaveInspectionError(RuntimeError):
    """Raised when a CK3 save does not expose readable metadata."""


@dataclass(frozen=True)
class SaveMetadata:
    path: Path
    game_version: str | None
    game_date: str | None
    player_name: str | None
    title_name: str | None
    mod_references: tuple[str, ...]
    mod_ids: tuple[str, ...]
    dlcs: tuple[str, ...]


@dataclass(frozen=True)
class SavePlaysetComparison:
    matching_mods: bool
    missing_from_playset: tuple[str, ...]
    added_since_save: tuple[str, ...]


def _metadata_text(path: Path) -> str:
    path = Path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(SAVE_READ_LIMIT)
    except OSError as error:
        raise SaveInspectionError(f"Could not read save metadata: {error}") from error
    text = raw.decode("utf-8", errors="ignore")
    marker = text.find("meta_data={")
    if marker < 0:
        raise SaveInspectionError("The save does not contain readable CK3 metadata")
    return text[marker:]


def _single_value(text: str, name: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|([^\s}}]+))", text)
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def _quoted_list(text: str, name: str) -> tuple[str, ...]:
    match = re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*\{{([^}}]*)\}}", text)
    if not match:
        return ()
    return tuple(value.replace(r'\"', '"') for value in QUOTED_VALUE.findall(match.group(1)))


def inspect_save_metadata(path: Path) -> SaveMetadata:
    """Inspect a save without reading its full game-state payload."""
    path = Path(path).resolve()
    text = _metadata_text(path)
    mod_references = _quoted_list(text, "mods")
    mod_ids = tuple(
        stem
        for reference in mod_references
        if (stem := Path(reference.replace("\\", "/")).stem).isdigit()
    )
    return SaveMetadata(
        path=path,
        game_version=_single_value(text, "version"),
        game_date=_single_value(text, "meta_date"),
        player_name=_single_value(text, "meta_player_name"),
        title_name=_single_value(text, "meta_title_name"),
        mod_references=mod_references,
        mod_ids=mod_ids,
        dlcs=_quoted_list(text, "dlcs"),
    )


def newest_save(save_directory: Path) -> Path | None:
    try:
        saves = [path for path in Path(save_directory).glob("*.ck3") if path.is_file()]
    except OSError:
        return None
    return max(saves, key=lambda path: path.stat().st_mtime, default=None)


def compare_save_to_playset(
    save: SaveMetadata,
    active_mod_ids: tuple[str, ...],
) -> SavePlaysetComparison:
    save_ids = set(save.mod_ids)
    active_ids = set(active_mod_ids)
    missing = tuple(sorted(save_ids - active_ids, key=int))
    added = tuple(sorted(active_ids - save_ids, key=int))
    return SavePlaysetComparison(
        matching_mods=not missing and not added,
        missing_from_playset=missing,
        added_since_save=added,
    )
