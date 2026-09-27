"""Automatic Crusader Kings III installation and version detection."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Iterable


VERSION_NUMBER = re.compile(r"\d+(?:\.\d+){1,3}")


@dataclass(frozen=True)
class CK3Installation:
    version: str
    game_root: Path
    launcher_settings: Path


def read_launcher_version(path: Path) -> CK3Installation | None:
    """Read and validate one CK3 launcher settings file."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    game_id = str(payload.get("gameId") or "").casefold()
    display_name = str(payload.get("displayName") or "").casefold()
    if game_id != "ck3" and "crusader kings iii" not in display_name:
        return None
    value = str(payload.get("rawVersion") or payload.get("version") or "").strip()
    match = VERSION_NUMBER.search(value)
    if not match:
        return None
    game_root = path.parent.parent if path.parent.name.casefold() == "launcher" else path.parent
    return CK3Installation(match.group(0), game_root, path)


def _irony_game_roots(appdata: Path) -> Iterable[Path]:
    database_root = appdata / "Mario" / "IronyModManager"
    try:
        databases = sorted(
            database_root.glob("Database_*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    roots: list[Path] = []
    for database in databases:
        try:
            entries = json.loads(database.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(entries, list):
            continue
        settings_entry = next(
            (entry for entry in entries if isinstance(entry, dict) and entry.get("Name") == "GameSettings"),
            None,
        )
        values = settings_entry.get("Value", []) if settings_entry else []
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, dict) or value.get("Type") != "CrusaderKings3":
                continue
            executable = value.get("ExecutableLocation")
            if isinstance(executable, str) and executable and not executable.startswith("steam://"):
                executable_path = Path(executable)
                roots.append(executable_path.parent.parent if executable_path.parent.name.casefold() == "binaries" else executable_path.parent)
        if roots:
            break
    return roots


def _steam_library_roots() -> Iterable[Path]:
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")
    steam_root = program_files_x86 / "Steam"
    roots = [steam_root]
    library_file = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        text = library_file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        text = ""
    for value in re.findall(r'"path"\s+"([^"]+)"', text, flags=re.IGNORECASE):
        roots.append(Path(value.replace("\\\\", "\\")))
    return roots


def _candidate_settings(extra_game_roots: Iterable[Path]) -> Iterable[Path]:
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    game_roots: list[Path] = list(extra_game_roots)
    game_roots.extend(_irony_game_roots(appdata))
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = Path(f"{letter}:\\")
        game_roots.append(drive / "Games" / "Crusader Kings III")
    for steam_root in _steam_library_roots():
        game_roots.append(steam_root / "steamapps" / "common" / "Crusader Kings III")

    seen: set[str] = set()
    for root in game_roots:
        root = Path(root)
        for path in (root / "launcher" / "launcher-settings.json", root / "launcher-settings.json"):
            key = str(path).casefold()
            if key not in seen:
                seen.add(key)
                yield path


def detect_ck3_installation(extra_game_roots: Iterable[Path] = ()) -> CK3Installation | None:
    """Return the first validated CK3 installation from known local sources."""
    for path in _candidate_settings(extra_game_roots):
        installation = read_launcher_version(path)
        if installation:
            return installation
    return None
