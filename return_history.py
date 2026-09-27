"""Persist scan checkpoints used by the return-from-hiatus dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Mapping


HISTORY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ReturnHistory:
    scanned_at_utc: str
    game_version: str
    enabled_mod_ids: tuple[str, ...]
    workshop_updated: dict[str, int]


@dataclass(frozen=True)
class HistoryComparison:
    elapsed_days: int
    game_version_changed: bool
    previous_game_version: str
    changed_workshop_ids: tuple[str, ...]
    added_mod_ids: tuple[str, ...]
    removed_mod_ids: tuple[str, ...]


def load_history(path: Path) -> ReturnHistory | None:
    path = Path(path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != HISTORY_SCHEMA_VERSION:
            return None
        enabled = payload.get("enabled_mod_ids", [])
        workshop = payload.get("workshop_updated", {})
        if not isinstance(enabled, list) or not isinstance(workshop, dict):
            return None
        return ReturnHistory(
            scanned_at_utc=str(payload["scanned_at_utc"]),
            game_version=str(payload["game_version"]),
            enabled_mod_ids=tuple(str(value) for value in enabled if str(value).isdigit()),
            workshop_updated={
                str(mod_id): int(timestamp)
                for mod_id, timestamp in workshop.items()
                if str(mod_id).isdigit() and isinstance(timestamp, int) and timestamp > 0
            },
        )
    except (KeyError, OSError, TypeError, ValueError):
        return None


def save_history(path: Path, history: ReturnHistory) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    payload = {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "scanned_at_utc": history.scanned_at_utc,
        "game_version": history.game_version,
        "enabled_mod_ids": list(history.enabled_mod_ids),
        "workshop_updated": history.workshop_updated,
    }
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def compare_history(
    previous: ReturnHistory,
    *,
    game_version: str,
    enabled_mod_ids: tuple[str, ...],
    workshop_updated: Mapping[str, int],
    now: datetime | None = None,
) -> HistoryComparison:
    current_time = now or datetime.now(timezone.utc)
    try:
        previous_time = datetime.fromisoformat(previous.scanned_at_utc)
        if previous_time.tzinfo is None:
            previous_time = previous_time.replace(tzinfo=timezone.utc)
        elapsed_days = max(0, (current_time - previous_time).days)
    except ValueError:
        elapsed_days = 0
    previous_ids = set(previous.enabled_mod_ids)
    current_ids = set(enabled_mod_ids)
    changed = tuple(
        sorted(
            (
                mod_id
                for mod_id in previous_ids & current_ids
                if mod_id in previous.workshop_updated
                and mod_id in workshop_updated
                and previous.workshop_updated[mod_id] != workshop_updated[mod_id]
            ),
            key=int,
        )
    )
    return HistoryComparison(
        elapsed_days=elapsed_days,
        game_version_changed=previous.game_version != game_version,
        previous_game_version=previous.game_version,
        changed_workshop_ids=changed,
        added_mod_ids=tuple(sorted(current_ids - previous_ids, key=int)),
        removed_mod_ids=tuple(sorted(previous_ids - current_ids, key=int)),
    )


def history_summary(comparison: HistoryComparison, game_version: str) -> str:
    game_text = (
        f"CK3 {comparison.previous_game_version} → {game_version}"
        if comparison.game_version_changed
        else f"CK3 {game_version} unchanged"
    )
    playset_changes = len(comparison.added_mod_ids) + len(comparison.removed_mod_ids)
    return (
        f"Since last scan ({comparison.elapsed_days} day(s)): {game_text} • "
        f"{len(comparison.changed_workshop_ids)} Workshop revision change(s) • "
        f"{playset_changes} playset membership change(s)"
    )
