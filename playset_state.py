"""Read the active CK3 mod state without modifying launcher or Irony data."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


class PlaysetError(RuntimeError):
    """Raised when active mod state exists but cannot be read safely."""


@dataclass(frozen=True)
class PlaysetMod:
    registry_id: str | None
    game_registry_id: str
    display_name: str
    mod_id: str | None
    directory: Path | None
    descriptor_path: Path | None
    position: int
    source: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class PlaysetState:
    data_directory: Path
    provider: str
    mods: tuple[PlaysetMod, ...]
    disabled_dlcs: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def enabled_mod_ids(self) -> frozenset[str]:
        return frozenset(mod.mod_id for mod in self.mods if mod.mod_id)

    @property
    def ordered_mod_ids(self) -> tuple[str, ...]:
        return tuple(mod.mod_id for mod in self.mods if mod.mod_id)


def _read_json(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise PlaysetError(f"Active mod state file is missing: {path.name}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as error:
        raise PlaysetError(f"Could not read {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise PlaysetError(f"Active mod state file is invalid: {path.name}")
    return value


def _resolve_inside(root: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        return None
    candidate = (root / Path(relative.replace("/", "\\"))).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _numeric_mod_id(value: object, game_registry_id: str) -> str | None:
    candidate = str(value) if value is not None else ""
    if candidate.isdigit():
        return candidate
    stem = Path(game_registry_id.replace("\\", "/")).stem
    return stem if stem.isdigit() else None


def read_active_playset(data_directory: Path) -> PlaysetState:
    """Read the launcher-compatible active mod list and order in read-only mode."""
    root = Path(data_directory).resolve()
    load_state = _read_json(root / "dlc_load.json", required=False)
    registry = _read_json(root / "mods_registry.json", required=False)
    game_data = _read_json(root / "game_data.json", required=False)

    enabled_value = load_state.get("enabled_mods", [])
    enabled = [str(value) for value in enabled_value] if isinstance(enabled_value, list) else []
    disabled_value = load_state.get("disabled_dlcs", [])
    disabled_dlcs = (
        tuple(str(value) for value in disabled_value)
        if isinstance(disabled_value, list)
        else ()
    )
    registry_entries = {
        str(key): value
        for key, value in registry.items()
        if isinstance(value, dict)
    }
    by_game_id = {
        str(value.get("gameRegistryId")): (registry_id, value)
        for registry_id, value in registry_entries.items()
        if value.get("gameRegistryId")
    }
    order_value = game_data.get("modsOrder", [])
    registry_order = (
        [str(value) for value in order_value]
        if isinstance(order_value, list)
        else []
    )

    ordered_game_ids: list[str] = []
    enabled_set = set(enabled)
    for registry_id in registry_order:
        entry = registry_entries.get(registry_id)
        if not entry:
            continue
        game_registry_id = str(entry.get("gameRegistryId", ""))
        if game_registry_id in enabled_set and game_registry_id not in ordered_game_ids:
            ordered_game_ids.append(game_registry_id)
    ordered_game_ids.extend(value for value in enabled if value not in ordered_game_ids)

    warnings: list[str] = []
    mods: list[PlaysetMod] = []
    for position, game_registry_id in enumerate(ordered_game_ids, start=1):
        match = by_game_id.get(game_registry_id)
        if match:
            registry_id, entry = match
            display_name = str(entry.get("displayName") or Path(game_registry_id).stem)
            directory = _resolve_inside(root, entry.get("dirPath"))
            descriptor = _resolve_inside(root, game_registry_id)
            if entry.get("dirPath") and directory is None:
                warnings.append(f"Unsafe mod directory ignored for {display_name}")
            if descriptor is None:
                warnings.append(f"Unsafe descriptor path ignored for {display_name}")
            mods.append(
                PlaysetMod(
                    registry_id=registry_id,
                    game_registry_id=game_registry_id,
                    display_name=display_name,
                    mod_id=_numeric_mod_id(entry.get("steamId"), game_registry_id),
                    directory=directory,
                    descriptor_path=descriptor,
                    position=position,
                    source=str(entry.get("source")) if entry.get("source") else None,
                    status=str(entry.get("status")) if entry.get("status") else None,
                )
            )
        else:
            warnings.append(f"Enabled mod is missing from the registry: {game_registry_id}")
            mods.append(
                PlaysetMod(
                    registry_id=None,
                    game_registry_id=game_registry_id,
                    display_name=Path(game_registry_id).stem,
                    mod_id=_numeric_mod_id(None, game_registry_id),
                    directory=None,
                    descriptor_path=_resolve_inside(root, game_registry_id),
                    position=position,
                )
            )

    provider = (
        "Irony / Paradox launcher"
        if (root / "IronyModManager_IntegrityCheck.txt").exists()
        else "Paradox launcher"
    )
    return PlaysetState(
        data_directory=root,
        provider=provider,
        mods=tuple(mods),
        disabled_dlcs=disabled_dlcs,
        warnings=tuple(warnings),
    )
