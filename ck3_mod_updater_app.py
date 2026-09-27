"""Desktop interface for the CK3 mod archive updater.

This interface deliberately separates three facts:
1. what is present in a local archive,
2. what the descriptor declares about CK3 compatibility, and
3. what Steam currently reports for the Workshop item.

Archive file dates are shown only as an estimate and never as proof of the
installed Workshop revision.
"""

from __future__ import annotations

import csv
import json
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

import requests
from batch_update import (
    BatchPlan,
    BatchPlanItem,
    build_batch_plan,
)
from ck3_game_detection import CK3Installation, detect_ck3_installation
from coordinated_update import (
    CoordinatedRecoveryResult,
    CoordinatedRollbackResult,
    CoordinatedUpdateError,
    CoordinatedUpdateManager,
    CoordinatedUpdateResult,
)
from install_ledger import InstallLedger, LedgerError
from installed_deployment import (
    PROCESS_CHECK_UNAVAILABLE,
    InstalledDeploymentManager,
    running_blocked_processes,
)
from mod_scan_core import (
    ArchiveScanRow,
    discover_mod_ids,
    fetch_workshop_batch,
    scan_archives,
)
from playset_state import PlaysetError, PlaysetState, read_active_playset
from preflight import PreflightReport, preflight_summary, run_preflight
from return_history import (
    ReturnHistory,
    compare_history,
    history_summary,
    load_history,
    save_history,
)
from save_inspection import (
    SaveInspectionError,
    SaveMetadata,
    compare_save_to_playset,
    inspect_save_metadata,
    newest_save,
)
from setup_snapshots import SnapshotError, SnapshotManager, SnapshotResult
from snapshot_restore import (
    SnapshotRestoreError,
    SnapshotRestoreManager,
    SnapshotRestorePlan,
    SnapshotRestoreResult,
    build_restore_plan,
)
from update_impact import ImpactError, compare_archives, impact_summary
from update_workflow import UpdateError, UpdateManager, UpdateResult


APP_NAME = "CK3 Mod Updater"
APP_VERSION = "0.5.0"
WORKSHOP_ITEM_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"


def _app_data_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return base / "CK3ModUpdater"


SETTINGS_FILE = _app_data_directory() / "settings.json"
LEDGER_FILE = _app_data_directory() / "install-baselines.json"
UPDATE_DATA_DIRECTORY = _app_data_directory() / "updates"
SNAPSHOT_DIRECTORY = _app_data_directory() / "snapshots"
RETURN_HISTORY_FILE = _app_data_directory() / "return-history.json"
INSTALLED_UPDATE_DIRECTORY = _app_data_directory() / "installed-updates"
COORDINATED_UPDATE_DIRECTORY = _app_data_directory() / "coordinated-updates"
SNAPSHOT_RESTORE_DIRECTORY = _app_data_directory() / "snapshot-restores"


def _application_asset(filename: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / filename


def _default_archive_directory() -> str:
    return ""


def _default_mod_directory() -> str:
    return str(
        Path.home()
        / "Documents"
        / "Paradox Interactive"
        / "Crusader Kings III"
        / "mod"
    )


def load_settings() -> dict[str, str]:
    defaults = {
        "archive_directory": _default_archive_directory(),
        "mod_directory": _default_mod_directory(),
        "game_root": "",
        "theme": "dark",
    }
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return defaults
    if not isinstance(saved, dict):
        return defaults
    for key in defaults:
        value = saved.get(key)
        if isinstance(value, str):
            defaults[key] = value
    if defaults["theme"] == "darkly":
        defaults["theme"] = "dark"
    elif defaults["theme"] == "flatly":
        defaults["theme"] = "light"
    return defaults


def save_settings(settings: dict[str, str]) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_FILE)


def _format_utc(timestamp: int | None) -> str:
    if not timestamp:
        return "—"
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _compatibility_label(status: str) -> str:
    return {
        "declared_compatible": "Declared compatible",
        "declared_incompatible": "Different CK3 version",
        "unknown": "Not declared",
    }.get(status, status.replace("_", " ").title())


def _workshop_label(row: ArchiveScanRow) -> str:
    return {
        "available": _format_utc(row.workshop_updated),
        "remote_unavailable": "Unavailable/private",
        "scan_error": "Lookup failed",
        "not_applicable": "Not a CK3 mod",
    }.get(row.workshop_status, row.workshop_status.replace("_", " ").title())


def _signal_label(signal: str) -> str:
    return {
        "remote_newer_than_archive_timestamp": "Workshop newer than ZIP date",
        "remote_not_newer_than_archive_timestamp": "No newer timestamp seen",
        "unknown": "Unknown",
        "not_applicable": "—",
    }.get(signal, signal.replace("_", " ").title())


def _baseline_label(status: str) -> str:
    return {
        "unknown_local_version": "No recorded baseline",
        "changed_since_recorded_install": "Update available",
        "no_change_since_recorded_install": "Matches recorded install",
        "local_archive_changed": "Archive changed since record",
        "baseline_verification_failed": "Baseline verification failed",
    }.get(status, status.replace("_", " ").title())


def _natural_text_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Return a case-insensitive key that orders embedded numbers numerically."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )


def _column_sort_value(item: ResultItem, column: str) -> object | None:
    row = item.row
    archive = row.archive
    if column == "name":
        return _natural_text_key(
            archive.name or row.workshop_title or archive.archive_path.name
        )
    if column == "id":
        return int(archive.mod_id) if archive.mod_id and archive.mod_id.isdigit() else None
    if column == "installed":
        return item.installed
    if column == "playset":
        return item.playset_position
    if column == "version":
        return _natural_text_key(archive.version) if archive.version else None
    if column == "compatibility":
        return _compatibility_label(row.compatibility).casefold()
    if column == "workshop":
        return row.workshop_updated if row.workshop_status == "available" else None
    if column == "baseline":
        return _baseline_label(row.baseline_status).casefold()
    raise ValueError(f"Unknown results column: {column}")


def sort_result_items(
    items: list[ResultItem],
    column: str | None,
    descending: bool,
) -> list[ResultItem]:
    """Sort table results while keeping unavailable values at the bottom."""
    if column is None:
        return list(items)
    available: list[tuple[object, ResultItem]] = []
    unavailable: list[ResultItem] = []
    for item in items:
        value = _column_sort_value(item, column)
        if value is None:
            unavailable.append(item)
        else:
            available.append((value, item))
    available.sort(key=lambda pair: pair[0], reverse=descending)
    return [item for _value, item in available] + unavailable


@dataclass(frozen=True)
class ResultItem:
    row: ArchiveScanRow
    installed: bool
    enabled: bool = False
    playset_position: int | None = None


def build_report_rows(results: list[ResultItem]) -> list[dict[str, Any]]:
    """Convert CK3 scan results to serializable report rows."""
    rows: list[dict[str, Any]] = []
    for item in results:
        row = item.row
        archive = row.archive
        if archive.status != "ck3_mod":
            continue
        rows.append(
            {
                "archive_file": archive.archive_path.name,
                "archive_status": archive.status,
                "workshop_id": archive.mod_id,
                "descriptor_name": archive.name,
                "descriptor_version": archive.version,
                "supported_ck3": archive.supported_version,
                "compatibility": row.compatibility,
                "workshop_status": row.workshop_status,
                "workshop_title": row.workshop_title,
                "workshop_updated_utc": _format_utc(row.workshop_updated),
                "baseline_status": row.baseline_status,
                "recorded_workshop_updated_utc": _format_utc(
                    row.installed_workshop_updated
                ),
                "archive_timestamp_signal": row.timestamp_signal,
                "duplicate_count": row.duplicate_count,
                "installed": item.installed,
                "enabled_in_playset": item.enabled,
                "playset_position": item.playset_position,
                "detail": row.detail,
            }
        )
    return rows


class CK3ModUpdaterApp(tk.Tk):
    def __init__(self) -> None:
        self.settings = load_settings()
        self.ledger = InstallLedger(LEDGER_FILE)
        self.update_manager = UpdateManager(UPDATE_DATA_DIRECTORY, self.ledger)
        self.installed_update_manager = InstalledDeploymentManager(INSTALLED_UPDATE_DIRECTORY)
        self.coordinated_update_manager = CoordinatedUpdateManager(
            COORDINATED_UPDATE_DIRECTORY,
            self.update_manager,
            self.installed_update_manager,
        )
        self.snapshot_manager = SnapshotManager(SNAPSHOT_DIRECTORY)
        self.snapshot_restore_manager = SnapshotRestoreManager(SNAPSHOT_RESTORE_DIRECTORY)
        super().__init__()
        self.title(APP_NAME)
        icon_path = _application_asset("icon.ico")
        if sys.platform == "win32" and icon_path.is_file():
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.geometry("1420x820")
        self.minsize(1080, 650)
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self._apply_theme(self.settings["theme"])
        self.results: list[ResultItem] = []
        self.visible_results: list[ResultItem] = []
        self.result_by_item: dict[str, ResultItem] = {}
        self.sort_column: str | None = None
        self.sort_descending = False
        self.worker: threading.Thread | None = None
        self.last_scan_utc: str | None = None
        self.playset: PlaysetState | None = None
        self.latest_save: SaveMetadata | None = None
        self.context_warning: str | None = None
        self.preflight_report: PreflightReport | None = None
        self.previous_history = load_history(RETURN_HISTORY_FILE)
        self.closing = False
        self.ui_events: queue.Queue[tuple[Callable[..., None], tuple[Any, ...]]] = queue.Queue()

        self.archive_var = tk.StringVar(value=self.settings["archive_directory"])
        self.mod_var = tk.StringVar(value=self.settings["mod_directory"])
        root_hint = self.settings.get("game_root", "").strip()
        self.installation: CK3Installation | None = detect_ck3_installation(
            [Path(root_hint)] if root_hint else []
        )
        self.version_var = tk.StringVar(
            value=self.installation.version if self.installation else "Not detected"
        )
        self.version_source_var = tk.StringVar(
            value=str(self.installation.game_root) if self.installation else "CK3 installation not found"
        )
        self.search_var = tk.StringVar()
        self.filter_var = tk.StringVar(value="CK3 mods")
        self.status_var = tk.StringVar(value="Ready to inspect your archive folder")
        self.playset_summary_var = tk.StringVar(value="Active playset not detected")
        self.save_summary_var = tk.StringVar(value="No recent save detected")
        self.snapshot_summary_var = tk.StringVar(value="No setup snapshots yet")
        self.return_summary_var = tk.StringVar(value="Scan to build a return plan")
        self.summary_vars = {
            "archives": tk.StringVar(value="0"),
            "ck3": tk.StringVar(value="0"),
            "review": tk.StringVar(value="0"),
            "installed": tk.StringVar(value="0"),
            "enabled": tk.StringVar(value="0"),
        }

        self._refresh_local_context()
        if self.playset and self.playset.mods:
            self.filter_var.set("Enabled playset")
        self._build_ui()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_ui_events)
        self.after(200, self._start_recovery_check)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, font=("Segoe UI", 22, "bold")).pack(side="left")
        ttk.Label(
            header,
            text="Workshop archive checker and updater",
            style="Secondary.TLabel",
            font=("Segoe UI", 11),
        ).pack(side="left", padx=(12, 0), pady=(7, 0))
        self.theme_button = ttk.Button(
            header,
            text="Light theme" if self.settings["theme"] == "dark" else "Dark theme",
            command=self._toggle_theme,
            style="Secondary.TButton",
        )
        self.theme_button.pack(side="right")

        ttk.Label(
            outer,
            text=(
                "Recorded baselines can confirm Workshop changes. Archive updates are validated, "
                "backed up, and replaced transactionally."
            ),
            style="Warning.TLabel",
        ).pack(fill="x", pady=(6, 14))

        return_frame = ttk.Labelframe(outer, text="Return-from-hiatus overview", padding=10)
        return_frame.pack(fill="x", pady=(0, 12))
        for column in range(3):
            return_frame.columnconfigure(column, weight=1)
        ttk.Label(return_frame, textvariable=self.playset_summary_var).grid(
            row=0, column=0, sticky="w", padx=(0, 12)
        )
        ttk.Label(return_frame, textvariable=self.save_summary_var).grid(
            row=0, column=1, sticky="w", padx=(0, 12)
        )
        ttk.Label(return_frame, textvariable=self.snapshot_summary_var).grid(
            row=0, column=2, sticky="w", padx=(0, 12)
        )
        ttk.Label(
            return_frame,
            textvariable=self.return_summary_var,
            style="Secondary.TLabel",
            wraplength=1050,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(7, 0))
        return_actions = ttk.Frame(return_frame)
        return_actions.grid(row=0, column=3, rowspan=2, sticky="e")
        self.snapshot_button = ttk.Button(
            return_actions,
            text="Preserve setup",
            command=self.preserve_current_setup,
            state="disabled",
            style="Primary.TButton",
        )
        self.snapshot_button.pack(side="left", padx=(8, 6))
        self.preflight_button = ttk.Button(
            return_actions,
            text="View preflight",
            command=self.show_preflight,
            state="disabled",
            style="Secondary.TButton",
        )
        self.preflight_button.pack(side="left", padx=(0, 6))
        ttk.Button(
            return_actions,
            text="Manage snapshots",
            command=self.manage_snapshots,
            style="Secondary.TButton",
        ).pack(side="left")

        paths = ttk.Labelframe(outer, text="Scan setup", padding=12)
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)

        ttk.Label(paths, text="Archive folder").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(paths, textvariable=self.archive_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(
            paths,
            text="Browse",
            command=lambda: self._browse_directory(self.archive_var),
            style="Secondary.TButton",
        ).grid(row=0, column=2, padx=(8, 0), pady=4)

        ttk.Label(paths, text="Installed mods").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(paths, textvariable=self.mod_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(
            paths,
            text="Browse",
            command=lambda: self._browse_directory(self.mod_var),
            style="Secondary.TButton",
        ).grid(row=1, column=2, padx=(8, 0), pady=4)

        version_frame = ttk.Frame(paths)
        version_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        ttk.Label(version_frame, text="CK3 version").pack(side="left")
        ttk.Label(version_frame, textvariable=self.version_var, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(8, 8))
        ttk.Label(version_frame, textvariable=self.version_source_var, style="Secondary.TLabel").pack(side="left", padx=(0, 12))
        ttk.Button(
            version_frame,
            text="Detect again",
            command=lambda: self._refresh_game_detection(show_error=True),
            style="Secondary.TButton",
        ).pack(side="left", padx=(0, 12))
        self.scan_button = ttk.Button(
            version_frame,
            text="Scan archives",
            command=self.start_scan,
            style="Primary.TButton",
            width=18,
        )
        self.scan_button.pack(side="left")
        self.progress = ttk.Progressbar(version_frame, mode="indeterminate", length=180)
        self.progress.pack(side="left", padx=(14, 0))
        ttk.Label(version_frame, textvariable=self.status_var).pack(side="left", padx=(12, 0))

        cards = ttk.Frame(outer)
        cards.pack(fill="x", pady=14)
        for index, (key, label) in enumerate(
            (
                ("archives", "ZIP files"),
                ("ck3", "CK3 archives"),
                ("review", "Needs review"),
                ("installed", "Installed IDs"),
                ("enabled", "Enabled mods"),
            )
        ):
            cards.columnconfigure(index, weight=1)
            card = ttk.Labelframe(cards, text=label, padding=(12, 7))
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 5, 0))
            ttk.Label(card, textvariable=self.summary_vars[key], font=("Segoe UI", 18, "bold")).pack()

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Label(controls, text="Search").pack(side="left")
        ttk.Entry(controls, textvariable=self.search_var, width=36).pack(side="left", padx=(8, 14))
        ttk.Label(controls, text="Show").pack(side="left")
        filter_box = ttk.Combobox(
            controls,
            textvariable=self.filter_var,
            state="readonly",
            width=27,
            values=(
                "Enabled playset",
                "CK3 mods",
                "Recorded updates available",
                "All files",
                "Possible Workshop changes",
                "Declared for another CK3 version",
                "Installed",
                "Workshop unavailable/errors",
                "Duplicate archives",
            ),
        )
        filter_box.pack(side="left", padx=(8, 0))
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self._apply_filter())
        self.export_button = ttk.Button(
            controls,
            text="Export report",
            command=self.export_report,
            state="disabled",
            style="Secondary.TButton",
        )
        self.export_button.pack(side="right")
        self.batch_button = ttk.Button(
            controls,
            text="Batch update files…",
            command=self.queue_batch_updates,
            state="disabled",
            style="Primary.TButton",
        )
        self.batch_button.pack(side="right", padx=(0, 8))

        pane = ttk.Panedwindow(outer, orient="vertical")
        pane.pack(fill="both", expand=True)
        table_frame = ttk.Frame(pane)
        details_frame = ttk.Labelframe(pane, text="Selected archive evidence", padding=10)
        pane.add(table_frame, weight=4)
        pane.add(details_frame, weight=1)

        columns = (
            "name",
            "id",
            "installed",
            "playset",
            "version",
            "compatibility",
            "workshop",
            "baseline",
        )
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=19)
        self.column_headings = {
            "name": "Mod / archive",
            "id": "Workshop ID",
            "installed": "Installed",
            "playset": "Playset",
            "version": "Mod version",
            "compatibility": "Descriptor vs CK3",
            "workshop": "Workshop updated",
            "baseline": "Recorded baseline",
        }
        widths = {
            "name": 280,
            "id": 115,
            "installed": 80,
            "playset": 85,
            "version": 100,
            "compatibility": 170,
            "workshop": 165,
            "baseline": 210,
        }
        for column in columns:
            self.tree.heading(
                column,
                text=self.column_headings[column],
                command=lambda selected=column: self._sort_by_column(selected),
            )
            self.tree.column(column, width=widths[column], minwidth=70, anchor="w")
        self.tree.tag_configure("review", foreground="#f0ad4e")
        self.tree.tag_configure("error", foreground="#e06c75")
        self.tree.tag_configure("nonmod", foreground="#8b949e")
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_details)
        self.tree.bind("<Double-1>", lambda _event: self.open_workshop())

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.details = tk.Text(details_frame, height=7, wrap="word", state="disabled", relief="flat")
        self.details.pack(side="left", fill="both", expand=True)
        detail_buttons = ttk.Frame(details_frame)
        detail_buttons.pack(side="right", fill="y", padx=(10, 0))
        self.workshop_button = ttk.Button(
            detail_buttons,
            text="Open Workshop",
            command=self.open_workshop,
            state="disabled",
            style="Primary.TButton",
            width=18,
        )
        self.workshop_button.pack(fill="x", pady=(0, 6))
        self.folder_button = ttk.Button(
            detail_buttons,
            text="Show archive",
            command=self.show_archive,
            state="disabled",
            style="Secondary.TButton",
            width=18,
        )
        self.folder_button.pack(fill="x")
        self.record_button = ttk.Button(
            detail_buttons,
            text="Record baseline",
            command=self.record_selected_baseline,
            state="disabled",
            style="Secondary.TButton",
            width=18,
        )
        self.record_button.pack(fill="x", pady=(6, 0))
        self.update_button = ttk.Button(
            detail_buttons,
            text="Apply update file…",
            command=self.apply_selected_update,
            state="disabled",
            style="Primary.TButton",
            width=18,
        )
        self.update_button.pack(fill="x", pady=(6, 0))
        self.undo_button = ttk.Button(
            detail_buttons,
            text="Undo last update",
            command=self.undo_selected_update,
            state="disabled",
            style="Secondary.TButton",
            width=18,
        )
        self.undo_button.pack(fill="x", pady=(6, 0))

        ttk.Label(
            outer,
            text=(
                f"{APP_VERSION}  •  Baseline-aware scanning with validated backups and "
                "recoverable archive and installed-mod replacement."
            ),
            style="Secondary.TLabel",
        ).pack(fill="x", pady=(9, 0))

        self._apply_theme(self.settings["theme"])

    def _browse_directory(self, variable: tk.StringVar) -> None:
        initial = variable.get().strip()
        chosen = filedialog.askdirectory(initialdir=initial if Path(initial).is_dir() else None)
        if chosen:
            variable.set(chosen)
            if variable is self.mod_var:
                self._refresh_local_context()

    def _ck3_data_directory(self) -> Path:
        mod_directory = Path(self.mod_var.get().strip())
        return mod_directory.parent if mod_directory.name.casefold() == "mod" else mod_directory

    def _refresh_local_context(self) -> None:
        data_directory = self._ck3_data_directory()
        self.context_warning = None
        try:
            self.playset = read_active_playset(data_directory)
        except PlaysetError as error:
            self.playset = None
            self.context_warning = str(error)

        self.latest_save = None
        save_path = newest_save(data_directory / "save games")
        if save_path:
            try:
                self.latest_save = inspect_save_metadata(save_path)
            except SaveInspectionError as error:
                self.context_warning = self.context_warning or str(error)

        if self.playset and self.playset.mods:
            self.playset_summary_var.set(
                f"Active: {len(self.playset.mods)} mods via {self.playset.provider}"
            )
        else:
            self.playset_summary_var.set("Active playset: no enabled mods detected")

        if self.latest_save:
            save_text = f"Latest save: CK3 {self.latest_save.game_version or 'unknown'}"
            if self.playset:
                comparison = compare_save_to_playset(
                    self.latest_save,
                    self.playset.ordered_mod_ids,
                )
                save_text += (
                    ", mod set matches"
                    if comparison.matching_mods
                    else ", mod set differs"
                )
            self.save_summary_var.set(save_text)
        else:
            self.save_summary_var.set("Latest save: not detected")

        snapshots = self.snapshot_manager.list_snapshots()
        if snapshots:
            latest = snapshots[0]
            version = latest.game_version or "unknown version"
            self.snapshot_summary_var.set(f"Latest snapshot: {latest.name} ({version})")
        else:
            self.snapshot_summary_var.set("Snapshots: none yet")
        if self.context_warning:
            self.return_summary_var.set(f"Local context needs attention: {self.context_warning}")
        elif not self.results:
            self.return_summary_var.set("Scan to compare the active setup with current Workshop metadata")
        if hasattr(self, "snapshot_button"):
            self.snapshot_button.configure(
                state="normal" if self.results and self.playset and self.playset.mods else "disabled"
            )

    def _toggle_theme(self) -> None:
        theme = "light" if self.settings.get("theme") == "dark" else "dark"
        self.settings["theme"] = theme
        self._apply_theme(theme)
        self.theme_button.configure(text="Light theme" if theme == "dark" else "Dark theme")
        try:
            save_settings(self._current_settings())
        except OSError:
            pass

    def _current_settings(self) -> dict[str, str]:
        return {
            "archive_directory": self.archive_var.get().strip(),
            "mod_directory": self.mod_var.get().strip(),
            "game_root": str(self.installation.game_root) if self.installation else "",
            "theme": self.settings.get("theme", "dark"),
        }

    def _refresh_game_detection(self, show_error: bool = False) -> CK3Installation | None:
        hints = [self.installation.game_root] if self.installation else []
        self.installation = detect_ck3_installation(hints)
        if self.installation:
            self.version_var.set(self.installation.version)
            self.version_source_var.set(str(self.installation.game_root))
            self.settings["game_root"] = str(self.installation.game_root)
            return self.installation
        self.version_var.set("Not detected")
        self.version_source_var.set("CK3 installation not found")
        if show_error:
            messagebox.showerror(
                "CK3 not detected",
                "The updater could not find a valid CK3 launcher-settings.json file.\n\n"
                "Install or launch CK3 once, then try detection again.",
            )
        return None

    def _apply_theme(self, theme: str) -> None:
        dark = theme == "dark"
        background = "#15181d" if dark else "#f4f6f8"
        panel = "#20242b" if dark else "#ffffff"
        field = "#292f38" if dark else "#ffffff"
        foreground = "#f2f4f7" if dark else "#1f2933"
        muted = "#aab2bf" if dark else "#5f6b76"
        border = "#3a424d" if dark else "#c7d0d9"
        selected = "#245b93" if dark else "#d7e9fb"
        selected_foreground = "#ffffff" if dark else "#17212b"
        self.configure(background=background)
        self.style.configure("TFrame", background=background)
        self.style.configure("TLabel", background=background, foreground=foreground)
        self.style.configure("Secondary.TLabel", background=background, foreground=muted)
        self.style.configure("Warning.TLabel", background=background, foreground="#e5a93d" if dark else "#8a5a00")
        self.style.configure("TLabelframe", background=background, bordercolor=border)
        self.style.configure("TLabelframe.Label", background=background, foreground=foreground)
        self.style.configure("TEntry", fieldbackground=field, foreground=foreground, bordercolor=border)
        self.style.configure("TCombobox", fieldbackground=field, foreground=foreground, bordercolor=border)
        self.style.map("TCombobox", fieldbackground=[("readonly", field)], foreground=[("readonly", foreground)])
        self.style.configure("TButton", background=panel, foreground=foreground, bordercolor=border)
        self.style.configure("Secondary.TButton", background=panel, foreground=foreground, bordercolor=border)
        self.style.configure("Primary.TButton", background="#2f81f7", foreground="#ffffff", bordercolor="#2f81f7")
        self.style.map("Primary.TButton", background=[("active", "#1f6feb")])
        self.style.configure(
            "Treeview",
            background=panel,
            fieldbackground=panel,
            foreground=foreground,
            bordercolor=border,
            rowheight=27,
        )
        self.style.configure("Treeview.Heading", background=field, foreground=foreground, bordercolor=border)
        self.style.map(
            "Treeview",
            background=[("selected", selected)],
            foreground=[("selected", selected_foreground)],
        )
        if hasattr(self, "details"):
            self.details.configure(
                background=panel,
                foreground=foreground,
                insertbackground=foreground,
                selectbackground=selected,
                selectforeground=selected_foreground,
            )
        if hasattr(self, "tree"):
            self.tree.tag_configure("review", foreground="#f0ad4e" if dark else "#8a5a00")
            self.tree.tag_configure("error", foreground="#ff7b72" if dark else "#b42318")
            self.tree.tag_configure("nonmod", foreground=muted)

    def start_scan(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self._refresh_local_context()
        self.preflight_report = None
        self.preflight_button.configure(state="disabled")
        archive_directory = Path(self.archive_var.get().strip())
        mod_directory = Path(self.mod_var.get().strip())
        installation = self._refresh_game_detection(show_error=True)
        if not installation:
            return
        game_version = installation.version
        if not archive_directory.is_dir():
            messagebox.showerror("Archive folder not found", f"Choose an existing folder:\n{archive_directory}")
            return
        try:
            save_settings(self._current_settings())
        except OSError as error:
            messagebox.showwarning("Settings not saved", str(error))

        self.scan_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.progress.start(12)
        self.status_var.set("Reading ZIP descriptors and checking Workshop…")
        self.worker = threading.Thread(
            target=self._scan_worker,
            args=(archive_directory, mod_directory, game_version, self.playset),
            daemon=True,
        )
        self.worker.start()

    def _scan_worker(
        self,
        archive_directory: Path,
        mod_directory: Path,
        game_version: str,
        playset: PlaysetState | None,
    ) -> None:
        try:
            baselines = self.ledger.load()
            with requests.Session() as session:
                rows = scan_archives(
                    archive_directory,
                    game_version,
                    lambda ids: fetch_workshop_batch(session, ids),
                    baselines,
                )
            installed_ids = set(discover_mod_ids(mod_directory)) if mod_directory.is_dir() else set()
            playset_positions = {
                mod.mod_id: mod.position
                for mod in playset.mods
                if mod.mod_id
            } if playset else {}
            results = [
                ResultItem(
                    row=row,
                    installed=bool(row.archive.mod_id in installed_ids),
                    enabled=bool(row.archive.mod_id in playset_positions),
                    playset_position=playset_positions.get(row.archive.mod_id),
                )
                for row in rows
            ]
        except Exception as error:
            self.ui_events.put((self._scan_failed, (str(error),)))
            return
        self.ui_events.put((self._scan_finished, (results,)))

    def _process_ui_events(self) -> None:
        if self.closing:
            return
        while True:
            try:
                callback, arguments = self.ui_events.get_nowait()
            except queue.Empty:
                break
            callback(*arguments)
        self.after(100, self._process_ui_events)

    def _scan_failed(self, detail: str) -> None:
        self.worker = None
        self.progress.stop()
        self.scan_button.configure(state="normal")
        self.export_button.configure(
            state="normal"
            if any(item.row.archive.status == "ck3_mod" for item in self.results)
            else "disabled"
        )
        self.batch_button.configure(
            state="normal"
            if any(item.row.archive.status == "ck3_mod" for item in self.results)
            else "disabled"
        )
        self.status_var.set("Scan failed")
        messagebox.showerror("Scan failed", detail)

    def _scan_finished(self, results: list[ResultItem]) -> None:
        self.worker = None
        self.results = results
        self.last_scan_utc = datetime.now(timezone.utc).isoformat()
        self.progress.stop()
        self.scan_button.configure(state="normal")
        ck3 = [item for item in results if item.row.archive.status == "ck3_mod"]
        self.export_button.configure(state="normal" if ck3 else "disabled")
        self.batch_button.configure(state="normal" if ck3 else "disabled")
        review = [
            item
            for item in ck3
            if item.row.timestamp_signal == "remote_newer_than_archive_timestamp"
            or item.row.baseline_status in {
                "changed_since_recorded_install",
                "local_archive_changed",
                "baseline_verification_failed",
            }
            or item.row.compatibility == "declared_incompatible"
            or item.row.workshop_status != "available"
            or item.row.duplicate_count > 1
        ]
        self.summary_vars["archives"].set(str(len(results)))
        self.summary_vars["ck3"].set(str(len(ck3)))
        self.summary_vars["review"].set(str(len(review)))
        self.summary_vars["installed"].set(str(sum(item.installed for item in results)))
        enabled_count = len(self.playset.mods) if self.playset else 0
        self.summary_vars["enabled"].set(str(enabled_count))
        ignored = len(results) - len(ck3)
        self.status_var.set(
            f"Finished: {len(ck3)} CK3 archives, {len(review)} needing review, {ignored} non-mod ZIPs ignored"
        )
        if self.playset and self.playset.mods:
            enabled_ids = self.playset.enabled_mod_ids
            found_ids = frozenset(
                item.row.archive.mod_id
                for item in ck3
                if item.row.archive.mod_id in enabled_ids
            )
            updates = sum(
                item.row.baseline_status == "changed_since_recorded_install"
                for item in ck3
                if item.enabled
            )
            baseline_coverage = sum(
                item.row.installed_workshop_updated is not None
                and item.row.baseline_status != "baseline_verification_failed"
                for item in ck3
                if item.enabled
            )
            missing = len(enabled_ids - found_ids)
            self.return_summary_var.set(
                f"Return plan: {len(found_ids)}/{len(enabled_ids)} enabled archives found • "
                f"{updates} confirmed updates • {baseline_coverage}/{len(enabled_ids)} baselines • "
                f"{missing} missing archives"
            )
            self.preflight_report = run_preflight(
                (item.row for item in ck3),
                self.playset,
                self.latest_save,
                self.version_var.get(),
            )
            self.return_summary_var.set(
                self.return_summary_var.get() + " • " + preflight_summary(self.preflight_report)
            )
            self.preflight_button.configure(state="normal")
            self.snapshot_button.configure(state="normal")
        else:
            self.return_summary_var.set("No enabled playset was detected; showing the full CK3 archive library")
            self.snapshot_button.configure(state="disabled")
            self.preflight_button.configure(state="disabled")
        enabled_ids = self.playset.ordered_mod_ids if self.playset else ()
        workshop_updated = {
            item.row.archive.mod_id: item.row.workshop_updated
            for item in ck3
            if item.row.archive.mod_id in set(enabled_ids) and item.row.workshop_updated
        }
        if self.previous_history:
            comparison = compare_history(
                self.previous_history,
                game_version=self.version_var.get(),
                enabled_mod_ids=enabled_ids,
                workshop_updated=workshop_updated,
            )
            self.return_summary_var.set(
                history_summary(comparison, self.version_var.get())
                + "\n"
                + self.return_summary_var.get()
            )
        else:
            self.return_summary_var.set(
                "First recorded return scan\n" + self.return_summary_var.get()
            )
        try:
            save_history(
                RETURN_HISTORY_FILE,
                ReturnHistory(
                    scanned_at_utc=datetime.now(timezone.utc).isoformat(),
                    game_version=self.version_var.get(),
                    enabled_mod_ids=enabled_ids,
                    workshop_updated=workshop_updated,
                ),
            )
        except OSError:
            pass
        self._apply_filter()

    def _matches_filter(self, item: ResultItem) -> bool:
        row = item.row
        selected = self.filter_var.get()
        if selected == "Enabled playset":
            return item.enabled
        if selected == "CK3 mods":
            return row.archive.status == "ck3_mod"
        if selected == "Recorded updates available":
            return row.baseline_status == "changed_since_recorded_install"
        if selected == "Possible Workshop changes":
            return row.timestamp_signal == "remote_newer_than_archive_timestamp"
        if selected == "Declared for another CK3 version":
            return row.compatibility == "declared_incompatible"
        if selected == "Installed":
            return item.installed
        if selected == "Workshop unavailable/errors":
            return row.workshop_status in {"remote_unavailable", "scan_error"}
        if selected == "Duplicate archives":
            return row.duplicate_count > 1
        return True

    def _sort_by_column(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column = column
            self.sort_descending = False
        for name, label in self.column_headings.items():
            indicator = " ▲" if name == self.sort_column and not self.sort_descending else ""
            if name == self.sort_column and self.sort_descending:
                indicator = " ▼"
            self.tree.heading(name, text=f"{label}{indicator}")
        self._apply_filter()

    def _apply_filter(self) -> None:
        search = self.search_var.get().strip().casefold()
        matching_results: list[ResultItem] = []
        self.result_by_item.clear()
        for tree_item in self.tree.get_children():
            self.tree.delete(tree_item)
        for item in self.results:
            row = item.row
            archive = row.archive
            haystack = " ".join(
                filter(None, (archive.name, row.workshop_title, archive.mod_id, archive.archive_path.name))
            ).casefold()
            if search and search not in haystack:
                continue
            if not self._matches_filter(item):
                continue
            matching_results.append(item)
        self.visible_results = sort_result_items(
            matching_results,
            self.sort_column,
            self.sort_descending,
        )
        for item in self.visible_results:
            row = item.row
            archive = row.archive
            display_name = archive.name or row.workshop_title or archive.archive_path.name
            if row.duplicate_count > 1:
                display_name = f"{display_name}  ({row.duplicate_count} copies)"
            tags: tuple[str, ...] = ()
            if archive.status != "ck3_mod":
                tags = ("nonmod",)
            elif row.workshop_status in {"remote_unavailable", "scan_error"}:
                tags = ("error",)
            elif (
                row.baseline_status in {
                    "changed_since_recorded_install",
                    "local_archive_changed",
                    "baseline_verification_failed",
                }
                or
                row.timestamp_signal == "remote_newer_than_archive_timestamp"
                or row.compatibility == "declared_incompatible"
                or row.duplicate_count > 1
            ):
                tags = ("review",)
            tree_item = self.tree.insert(
                "",
                "end",
                values=(
                    display_name,
                    archive.mod_id or "—",
                    "Yes" if item.installed else "No",
                    f"#{item.playset_position}" if item.playset_position else "No",
                    archive.version or "—",
                    _compatibility_label(row.compatibility),
                    _workshop_label(row),
                    _baseline_label(row.baseline_status),
                ),
                tags=tags,
            )
            self.result_by_item[tree_item] = item
        self._set_details("Select an archive to see exactly what each status means.")
        self.workshop_button.configure(state="disabled")
        self.folder_button.configure(state="disabled")
        self.record_button.configure(state="disabled")
        self.update_button.configure(state="disabled")
        self.undo_button.configure(state="disabled")

    def _selected_result(self) -> ResultItem | None:
        selected = self.tree.selection()
        return self.result_by_item.get(selected[0]) if selected else None

    def _show_selected_details(self, _event: Any = None) -> None:
        item = self._selected_result()
        if not item:
            return
        row = item.row
        archive = row.archive
        lines = [
            f"Archive: {archive.archive_path}",
            f"Detected type: {archive.status.replace('_', ' ')}",
            f"Workshop ID: {archive.mod_id or 'Not found'}",
            f"Descriptor name: {archive.name or 'Not found'}",
            f"Descriptor version: {archive.version or 'Not declared'}",
            f"Supported CK3 pattern: {archive.supported_version or 'Not declared'}",
            f"Compatibility result: {_compatibility_label(row.compatibility)}",
            f"Workshop result: {_workshop_label(row)}",
            f"Recorded baseline: {_baseline_label(row.baseline_status)}",
            f"Recorded Workshop revision: {_format_utc(row.installed_workshop_updated)}",
            f"Archive-date signal: {_signal_label(row.timestamp_signal)}",
            f"Installed in selected mod folder: {'Yes' if item.installed else 'No'}",
            f"Enabled in active playset: {'Yes' if item.enabled else 'No'}",
        ]
        if item.playset_position:
            lines.append(f"Active playset position: {item.playset_position}")
        if row.duplicate_count > 1:
            lines.append(f"Duplicate archives for this ID: {row.duplicate_count}")
        if row.detail:
            lines.append(f"Detail: {row.detail}")
        lines.append(
            "Note: a ZIP file date cannot prove which Workshop revision is inside the archive."
        )
        self._set_details("\n".join(lines))
        self.workshop_button.configure(state="normal" if archive.mod_id else "disabled")
        self.folder_button.configure(state="normal")
        eligible = bool(
            archive.status == "ck3_mod"
            and archive.mod_id
            and row.workshop_status == "available"
            and row.workshop_updated
            and row.duplicate_count == 1
        )
        self.record_button.configure(state="normal" if eligible else "disabled")
        self.update_button.configure(state="normal" if eligible else "disabled")
        try:
            transaction_id = None
            if archive.mod_id:
                transaction_id = self.coordinated_update_manager.latest_recoverable(archive.mod_id)
                if not transaction_id:
                    transaction_id = self.update_manager.latest_recoverable(
                        archive.mod_id, archive.archive_path
                    )
        except (UpdateError, CoordinatedUpdateError):
            transaction_id = None
        self.undo_button.configure(state="normal" if transaction_id else "disabled")

    def _set_details(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def open_workshop(self) -> None:
        item = self._selected_result()
        if item and item.row.archive.mod_id:
            webbrowser.open(WORKSHOP_ITEM_URL.format(item.row.archive.mod_id))

    def show_archive(self) -> None:
        item = self._selected_result()
        if not item:
            return
        path = item.row.archive.archive_path
        if sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{path}"])
        else:
            webbrowser.open(path.parent.as_uri())

    def _active_archives_by_mod_id(self) -> dict[str, list[Path]]:
        active_ids = self.playset.enabled_mod_ids if self.playset else frozenset()
        archives: dict[str, list[Path]] = {}
        for item in self.results:
            mod_id = item.row.archive.mod_id
            if mod_id and mod_id in active_ids and item.row.archive.status == "ck3_mod":
                archives.setdefault(mod_id, []).append(item.row.archive.archive_path)
        return archives

    def _enabled_mods_by_id(self) -> dict[str, Any]:
        return {
            mod.mod_id: mod
            for mod in self.playset.mods
            if mod.mod_id
        } if self.playset else {}

    def _mod_apps_are_closed(self, *, parent: tk.Misc | None = None) -> bool:
        running = running_blocked_processes()
        if PROCESS_CHECK_UNAVAILABLE in running:
            messagebox.showerror(
                "Process check unavailable",
                "The program could not verify whether CK3 or a mod manager is running. "
                "No installed files will be changed.",
                parent=parent,
            )
            return False
        if running:
            messagebox.showwarning(
                "Close CK3 and mod tools",
                "Close CK3, Irony Mod Manager, and the Paradox launcher, then try again.\n\n"
                "Still running: " + ", ".join(running),
                parent=parent,
            )
            return False
        return True

    def show_preflight(self) -> None:
        report = self.preflight_report
        if not report:
            messagebox.showinfo("Preflight unavailable", "Run a scan of the active playset first.")
            return
        lines = [
            preflight_summary(report),
            f"Enabled archives structurally checked: {report.checked_mods}",
            "",
            "This checks structure and recorded evidence; it does not claim the mods work in-game.",
        ]
        if report.issues:
            lines.extend(("", "Findings:"))
            lines.extend(
                f"• {issue.severity.title()}: {issue.message}"
                for issue in report.issues
            )
        else:
            lines.extend(("", "No structural or save/playset consistency findings were detected."))
        messagebox.showinfo("Active setup preflight", "\n".join(lines))

    def preserve_current_setup(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.playset or not self.playset.mods or not self.results:
            messagebox.showinfo(
                "Nothing to preserve",
                "Scan an active playset before creating a complete setup snapshot.",
            )
            return
        include_save = False
        if self.latest_save:
            answer = messagebox.askyesnocancel(
                "Include latest save",
                "Include the latest save in this snapshot?\n\n"
                f"{self.latest_save.path.name}\n"
                f"CK3 {self.latest_save.game_version or 'unknown version'}\n\n"
                "Yes includes a complete copy of the save. No preserves only the setup.",
            )
            if answer is None:
                return
            include_save = answer
        default_name = f"CK3 {self.version_var.get()} working setup"
        name = simpledialog.askstring(
            "Name setup snapshot",
            "Choose a recognizable name for this working setup:",
            initialvalue=default_name,
            parent=self,
        )
        if not name:
            return
        archives = self._active_archives_by_mod_id()
        save = self.latest_save if include_save else None
        try:
            estimate = self.snapshot_manager.estimate(self.playset, archives, save)
        except SnapshotError as error:
            messagebox.showerror("Snapshot unavailable", str(error))
            return
        warning_text = ""
        if estimate.warnings:
            warning_text = f"\n\nWarnings: {len(estimate.warnings)}. They will be recorded in the snapshot."
        if not messagebox.askyesno(
            "Preserve complete setup",
            f"Files: {estimate.file_count}\n"
            f"Required space: {_format_bytes(estimate.total_bytes)}\n"
            f"Storage: {SNAPSHOT_DIRECTORY}"
            f"{warning_text}\n\n"
            "The program will copy and hash-verify the active installed mods, matching source "
            "archives, launcher-compatible configuration, and the selected save. Continue?",
        ):
            return
        self._begin_archive_operation("Creating and verifying setup snapshot…")
        self.snapshot_button.configure(state="disabled")
        self.worker = threading.Thread(
            target=self._snapshot_worker,
            args=(name, self.version_var.get(), self.playset, archives, save),
            daemon=True,
        )
        self.worker.start()

    def _snapshot_worker(
        self,
        name: str,
        game_version: str,
        playset: PlaysetState,
        archives: dict[str, list[Path]],
        save: SaveMetadata | None,
    ) -> None:
        try:
            result = self.snapshot_manager.create(
                name,
                game_version,
                playset,
                archives,
                save,
            )
        except SnapshotError as error:
            self.ui_events.put((self._archive_operation_failed, (str(error),)))
            return
        self.ui_events.put((self._snapshot_finished, (result,)))

    def _snapshot_finished(self, result: SnapshotResult) -> None:
        self.worker = None
        self.progress.stop()
        self.scan_button.configure(state="normal")
        self.export_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self.status_var.set("Working setup preserved")
        self._refresh_local_context()
        self.preflight_button.configure(state="normal" if self.preflight_report else "disabled")
        warning_text = f"\n\nWarnings recorded: {len(result.warnings)}" if result.warnings else ""
        messagebox.showinfo(
            "Setup preserved",
            f"Verified files: {result.file_count}\n"
            f"Snapshot size: {_format_bytes(result.total_bytes)}\n"
            f"Location: {result.directory}"
            f"{warning_text}",
        )
        self._apply_filter()

    def open_snapshots(self) -> None:
        try:
            SNAPSHOT_DIRECTORY.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            messagebox.showerror("Snapshots unavailable", str(error))
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(SNAPSHOT_DIRECTORY)])
        else:
            webbrowser.open(SNAPSHOT_DIRECTORY.as_uri())

    def manage_snapshots(self) -> None:
        snapshots = self.snapshot_manager.list_snapshots()
        window = tk.Toplevel(self)
        window.title("Setup snapshots")
        window.geometry("900x430")
        window.minsize(720, 360)
        window.transient(self)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Snapshots and transaction backups are kept until you remove them manually.",
            style="Warning.TLabel",
        ).pack(fill="x", pady=(0, 10))
        columns = ("name", "created", "version", "files", "size")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=12)
        headings = {
            "name": "Snapshot",
            "created": "Created (UTC)",
            "version": "CK3 version",
            "files": "Files",
            "size": "Size",
        }
        widths = {"name": 260, "created": 190, "version": 100, "files": 80, "size": 100}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor="w")
        snapshot_by_item: dict[str, SnapshotResult] = {}
        for snapshot in snapshots:
            item_id = tree.insert(
                "",
                "end",
                values=(
                    snapshot.name,
                    snapshot.created_at_utc or "—",
                    snapshot.game_version or "—",
                    snapshot.file_count,
                    _format_bytes(snapshot.total_bytes),
                ),
            )
            snapshot_by_item[item_id] = snapshot
        tree.pack(fill="both", expand=True)
        if snapshots:
            first = tree.get_children()[0]
            tree.selection_set(first)
            tree.focus(first)
        else:
            ttk.Label(frame, text="No setup snapshots have been created yet.").place(relx=0.5, rely=0.45, anchor="center")

        def selected_snapshot() -> SnapshotResult | None:
            selection = tree.selection()
            return snapshot_by_item.get(selection[0]) if selection else None

        def verify_selected() -> None:
            selected = selected_snapshot()
            if not selected:
                return
            try:
                problems = self.snapshot_manager.verify(selected.directory)
            except SnapshotError as error:
                messagebox.showerror("Snapshot verification failed", str(error), parent=window)
                return
            if problems:
                messagebox.showerror(
                    "Snapshot verification failed",
                    "\n".join(problems[:20]),
                    parent=window,
                )
            else:
                messagebox.showinfo(
                    "Snapshot verified",
                    f"All {selected.file_count} recorded files match their saved hashes.",
                    parent=window,
                )

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Open storage", command=self.open_snapshots).pack(side="left")
        ttk.Button(actions, text="Verify", command=verify_selected).pack(side="left", padx=(8, 0))
        ttk.Button(
            actions,
            text="Restore selected…",
            command=lambda: self.restore_snapshot(selected_snapshot(), window),
            style="Primary.TButton",
        ).pack(side="left", padx=(8, 0))
        undo_state = "normal" if self.snapshot_restore_manager.latest_recoverable() else "disabled"
        ttk.Button(
            actions,
            text="Undo last restore",
            command=lambda: self.undo_last_snapshot_restore(window),
            state=undo_state,
            style="Secondary.TButton",
        ).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Close", command=window.destroy).pack(side="right")

    def restore_snapshot(self, snapshot: SnapshotResult | None, parent: tk.Toplevel) -> None:
        if self.worker and self.worker.is_alive():
            return
        if snapshot is None:
            messagebox.showinfo("Choose a snapshot", "Select a snapshot to restore.", parent=parent)
            return
        if not self.results or not self.playset or not self.playset.mods:
            messagebox.showinfo(
                "Scan required",
                "Run a fresh scan of the current active setup before restoring a snapshot.",
                parent=parent,
            )
            return
        if not self._mod_apps_are_closed(parent=parent):
            return
        archive_directory = Path(self.archive_var.get().strip())
        data_directory = self._ck3_data_directory()
        try:
            plan = build_restore_plan(
                self.snapshot_manager,
                snapshot.directory,
                archive_directory,
                data_directory,
                self.playset,
            )
            safety_estimate = self.snapshot_manager.estimate(
                self.playset,
                self._active_archives_by_mod_id(),
                self.latest_save,
            )
        except (SnapshotError, SnapshotRestoreError) as error:
            messagebox.showerror("Snapshot cannot be restored", str(error), parent=parent)
            return
        file_count = sum(operation.kind == "file" for operation in plan.operations)
        directory_count = sum(operation.kind == "directory" for operation in plan.operations)
        details = [
            f"Snapshot: {plan.snapshot_name}",
            f"Restore targets: {directory_count} mod folder(s), {file_count} file(s)",
            "",
            "A new verified snapshot of the current setup will be created first.",
            f"Safety snapshot: {safety_estimate.file_count} files, {_format_bytes(safety_estimate.total_bytes)}",
            "",
            "Launcher and Irony configuration will not be changed.",
        ]
        if plan.configuration_differences:
            details.append("Configuration differs: " + ", ".join(plan.configuration_differences))
        details.extend(("", *plan.reconciliation))
        details.extend(
            (
                "",
                "Existing restore targets receive verified backups. A saved game is restored "
                "under a new filename. CK3, Irony, and the Paradox launcher must be closed.",
            )
        )
        if not messagebox.askyesno("Restore verified snapshot", "\n".join(details), parent=parent):
            return
        parent.destroy()
        self._begin_archive_operation("Creating a safety snapshot before restore…")
        self.worker = threading.Thread(
            target=self._snapshot_restore_worker,
            args=(plan, self.playset, self._active_archives_by_mod_id(), self.latest_save, self.version_var.get()),
            daemon=True,
        )
        self.worker.start()

    def _snapshot_restore_worker(
        self,
        plan: SnapshotRestorePlan,
        playset: PlaysetState,
        archives: dict[str, list[Path]],
        save: SaveMetadata | None,
        game_version: str,
    ) -> None:
        try:
            safety_snapshot = self.snapshot_manager.create(
                f"Before restoring {plan.snapshot_name}",
                game_version,
                playset,
                archives,
                save,
            )
            transaction_id = self.snapshot_restore_manager.prepare(plan)
            result = self.snapshot_restore_manager.apply(transaction_id)
        except (SnapshotError, SnapshotRestoreError) as error:
            self.ui_events.put((self._archive_operation_failed, (str(error),)))
            return
        self.ui_events.put((self._snapshot_restore_finished, (result, safety_snapshot, plan)))

    def _snapshot_restore_finished(
        self,
        result: SnapshotRestoreResult,
        safety_snapshot: SnapshotResult,
        plan: SnapshotRestorePlan,
    ) -> None:
        self.worker = None
        self.progress.stop()
        self.scan_button.configure(state="normal")
        self.status_var.set("Snapshot restored and verified")
        save_text = f"\nRestored save: {result.restored_save}" if result.restored_save else ""
        reconcile = "\n\n" + "\n".join(plan.reconciliation) if plan.reconciliation else ""
        messagebox.showinfo(
            "Snapshot restored",
            f"Restored mod folders: {result.restored_directories}\n"
            f"Restored files: {result.restored_files}\n"
            f"Safety snapshot: {safety_snapshot.directory}"
            f"{save_text}{reconcile}\n\n"
            "Launcher and Irony configuration was left unchanged. A fresh preflight scan will now run.",
        )
        self.start_scan()

    def undo_last_snapshot_restore(self, parent: tk.Toplevel) -> None:
        transaction_id = self.snapshot_restore_manager.latest_recoverable()
        if not transaction_id:
            messagebox.showinfo("Restore backup unavailable", "No committed snapshot restore can be undone.", parent=parent)
            return
        if not self._mod_apps_are_closed(parent=parent):
            return
        if not messagebox.askyesno(
            "Undo last snapshot restore",
            "Restore every verified path backup from the latest snapshot restore?\n\n"
            "This is refused if any restored path changed afterward. CK3, Irony, and the launcher must be closed.",
            parent=parent,
        ):
            return
        parent.destroy()
        self._begin_archive_operation("Restoring files from before the snapshot restore…")
        self.worker = threading.Thread(
            target=self._undo_snapshot_restore_worker,
            args=(transaction_id,),
            daemon=True,
        )
        self.worker.start()

    def _undo_snapshot_restore_worker(self, transaction_id: str) -> None:
        try:
            result = self.snapshot_restore_manager.rollback(transaction_id)
        except SnapshotRestoreError as error:
            self.ui_events.put((self._archive_operation_failed, (str(error),)))
            return
        self.ui_events.put((self._undo_snapshot_restore_finished, (result,)))

    def _undo_snapshot_restore_finished(self, result: SnapshotRestoreResult) -> None:
        self.worker = None
        self.progress.stop()
        self.scan_button.configure(state="normal")
        self.status_var.set("Snapshot restore undone")
        messagebox.showinfo(
            "Snapshot restore undone",
            f"Previous files restored: {result.restored_files + result.restored_directories}\n\n"
            "A fresh preflight scan will now run.",
        )
        self.start_scan()

    def queue_batch_updates(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        candidates = filedialog.askopenfilenames(
            title="Choose downloaded CK3 update archives",
            initialdir=self.archive_var.get().strip() or None,
            filetypes=(("ZIP archives", "*.zip"),),
        )
        if not candidates:
            return
        plan = build_batch_plan(
            (item.row for item in self.results),
            (Path(path) for path in candidates),
        )
        if plan.issues:
            details = "\n".join(
                f"• {issue.candidate_archive.name}: {issue.message}"
                for issue in plan.issues[:20]
            )
            if len(plan.issues) > 20:
                details += f"\n• …and {len(plan.issues) - 20} more"
            messagebox.showerror(
                "Batch needs attention",
                "No files were updated. Resolve or remove these candidates, then choose the "
                f"batch again:\n\n{details}",
            )
            return
        if not plan.items:
            messagebox.showinfo("No updates matched", "No selected file matched a scanned archive.")
            return

        snapshot_estimate = None
        archives = self._active_archives_by_mod_id()
        if self.playset and self.playset.mods:
            try:
                snapshot_estimate = self.snapshot_manager.estimate(
                    self.playset,
                    archives,
                    self.latest_save,
                )
            except SnapshotError as error:
                messagebox.showerror("Batch snapshot unavailable", str(error))
                return

        lines = [
            f"Validated updates: {len(plan.items)}",
            "",
        ]
        for item in plan.items[:20]:
            lines.append(
                f"• {item.name}: +{len(item.impact.added_files)} / "
                f"−{len(item.impact.removed_files)} / Δ{len(item.impact.changed_files)} files"
            )
        if len(plan.items) > 20:
            lines.append(f"• …and {len(plan.items) - 20} more")
        affected_save_mods = (
            sorted(
                {item.mod_id for item in plan.items} & set(self.latest_save.mod_ids),
                key=int,
            )
            if self.latest_save
            else []
        )
        if affected_save_mods:
            lines.extend(
                (
                    "",
                    f"Save-aware warning: {len(affected_save_mods)} selected update(s) are used by "
                    "the latest save.",
                )
            )
        installed_update_count = sum(
            item.mod_id in self._enabled_mods_by_id()
            for item in plan.items
        )
        if installed_update_count:
            if not self._mod_apps_are_closed():
                return
            lines.extend(
                (
                    "",
                    f"Enabled installations to replace: {installed_update_count}",
                    "Those extracted mod folders and descriptors will be backed up, replaced, "
                    "and verified together with the archive library. Close CK3, Irony, and the launcher first.",
                )
            )
        if snapshot_estimate:
            lines.extend(
                (
                    "",
                    "A complete setup snapshot will be created first:",
                    f"{snapshot_estimate.file_count} files, "
                    f"{_format_bytes(snapshot_estimate.total_bytes)}",
                )
            )
        else:
            lines.extend(
                (
                    "",
                    "No active playset snapshot is available. Each archive will still receive "
                    "its own verified transaction backup.",
                )
            )
        lines.extend(
            (
                "",
                "Only continue if every selected ZIP was obtained for the Workshop revision "
                "shown by the current scan. The program will not infer that automatically.",
            )
        )
        if not messagebox.askyesno("Apply validated batch", "\n".join(lines)):
            return

        self._begin_archive_operation(f"Preparing {len(plan.items)} validated updates…")
        self.worker = threading.Thread(
            target=self._batch_update_worker,
            args=(
                plan,
                archives,
                self.playset,
                self.latest_save,
                self.version_var.get(),
                self._ck3_data_directory(),
            ),
            daemon=True,
        )
        self.worker.start()

    def _batch_update_worker(
        self,
        plan: BatchPlan,
        archives: dict[str, list[Path]],
        playset: PlaysetState | None,
        save: SaveMetadata | None,
        game_version: str,
        data_directory: Path,
    ) -> None:
        snapshot: SnapshotResult | None = None
        try:
            if playset and playset.mods:
                snapshot = self.snapshot_manager.create(
                    f"Before batch update {datetime.now().strftime('%Y-%m-%d %H-%M')}",
                    game_version,
                    playset,
                    archives,
                    save,
                )
            enabled_mods = {
                mod.mod_id: mod
                for mod in playset.mods
                if mod.mod_id
            } if playset else {}
            result = self.coordinated_update_manager.apply(
                plan.items,
                enabled_mods,
                data_directory,
            )
        except (CoordinatedUpdateError, SnapshotError) as error:
            self.ui_events.put((self._archive_operation_failed, (str(error),)))
            return
        self.ui_events.put((self._batch_update_finished, (result, snapshot)))

    def _batch_update_finished(
        self,
        result: CoordinatedUpdateResult,
        snapshot: SnapshotResult | None,
    ) -> None:
        self.worker = None
        self.progress.stop()
        self.scan_button.configure(state="normal")
        self.status_var.set(
            f"Batch committed: {len(result.archive_updates)} archive(s), "
            f"{len(result.installed_updates)} installed mod(s) updated"
        )
        snapshot_text = f"\n\nPre-update snapshot: {snapshot.directory}" if snapshot else ""
        messagebox.showinfo(
            "Batch update complete",
            f"Updated and verified archives: {len(result.archive_updates)}\n"
            f"Updated enabled installations: {len(result.installed_updates)}"
            f"{snapshot_text}\n\nA fresh preflight scan will now run.",
        )
        self.start_scan()

    def _start_recovery_check(self) -> None:
        if self.worker and self.worker.is_alive():
            self.after(250, self._start_recovery_check)
            return
        self.scan_button.configure(state="disabled")
        self.status_var.set("Checking unfinished update transactions…")
        self.worker = threading.Thread(target=self._recovery_worker, daemon=True)
        self.worker.start()

    def _recovery_worker(self) -> None:
        try:
            results: list[Any] = list(self.coordinated_update_manager.recover_pending())
            results.extend(self.snapshot_restore_manager.recover_pending())
        except (LedgerError, OSError, UpdateError, CoordinatedUpdateError, SnapshotRestoreError) as error:
            results = [CoordinatedRecoveryResult("startup", "error", str(error))]
        self.ui_events.put((self._recovery_finished, (results,)))

    def _recovery_finished(self, results: list[Any]) -> None:
        self.worker = None
        self.scan_button.configure(state="normal")
        errors = [result.detail for result in results if result.action == "error"]
        recovered = [result for result in results if result.action != "error"]
        if errors:
            self.status_var.set("Update recovery needs attention")
            messagebox.showerror(
                "Update recovery failed",
                "One or more interrupted updates could not be recovered:\n\n"
                + "\n".join(errors),
            )
        elif recovered:
            self.status_var.set(f"Recovered {len(recovered)} interrupted update operation(s)")
            messagebox.showinfo(
                "Update recovery complete",
                "Interrupted update work was safely cancelled or rolled back.",
            )
        else:
            self.status_var.set("Ready to inspect your archive folder")

    def record_selected_baseline(self) -> None:
        item = self._selected_result()
        if not item or not item.row.archive.mod_id or not item.row.workshop_updated:
            return
        archive = item.row.archive
        if not messagebox.askyesno(
            "Record current baseline",
            "Only continue if this exact archive is the current Workshop revision.\n\n"
            f"Archive: {archive.archive_path.name}\n"
            f"Workshop updated: {_format_utc(item.row.workshop_updated)}\n\n"
            "The archive hash and Workshop timestamp will be recorded locally.",
        ):
            return
        try:
            self.ledger.record_archive(
                archive,
                item.row.workshop_updated,
                source="manual_confirmation",
            )
        except LedgerError as error:
            messagebox.showerror("Baseline not recorded", str(error))
            return
        messagebox.showinfo(
            "Baseline recorded",
            "This exact archive can now be compared reliably with future Workshop updates.",
        )
        self.start_scan()

    def _begin_archive_operation(self, status: str) -> None:
        self.scan_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.record_button.configure(state="disabled")
        self.update_button.configure(state="disabled")
        self.undo_button.configure(state="disabled")
        self.snapshot_button.configure(state="disabled")
        self.preflight_button.configure(state="disabled")
        self.progress.start(12)
        self.status_var.set(status)

    def apply_selected_update(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        item = self._selected_result()
        if not item or not item.row.archive.mod_id or not item.row.workshop_updated:
            return
        archive = item.row.archive
        candidate = filedialog.askopenfilename(
            title="Choose the downloaded update archive",
            initialdir=str(archive.archive_path.parent),
            filetypes=(("ZIP archives", "*.zip"),),
        )
        if not candidate:
            return
        try:
            impact = compare_archives(archive.archive_path, Path(candidate))
        except ImpactError as error:
            messagebox.showerror("Update file rejected", str(error))
            return
        size_change = impact.candidate_uncompressed_bytes - impact.current_uncompressed_bytes
        size_text = (
            f"+{_format_bytes(size_change)}"
            if size_change >= 0
            else f"-{_format_bytes(abs(size_change))}"
        )
        active_mod = self._enabled_mods_by_id().get(archive.mod_id)
        snapshot_estimate = None
        if active_mod and self.playset:
            if not self._mod_apps_are_closed():
                return
            try:
                snapshot_estimate = self.snapshot_manager.estimate(
                    self.playset,
                    self._active_archives_by_mod_id(),
                    self.latest_save,
                )
            except SnapshotError as error:
                messagebox.showerror("Pre-update snapshot unavailable", str(error))
                return
        installed_text = (
            "\n\nThis mod is enabled. Its extracted installed folder and descriptor will also "
            "be replaced from the validated ZIP, with verified backups. CK3, Irony, and the "
            "Paradox launcher must be closed."
            if active_mod
            else "\n\nThis mod is not enabled in the detected playset, so only its archive-library ZIP will change."
        )
        snapshot_text = (
            f"\nA pre-update setup snapshot will preserve {snapshot_estimate.file_count} files "
            f"({_format_bytes(snapshot_estimate.total_bytes)})."
            if snapshot_estimate
            else ""
        )
        if not messagebox.askyesno(
            "Apply mod update",
            "The selected ZIP will be validated against the Workshop ID, copied into private "
            "staging, and verified. The current archive will then be backed up before an atomic "
            "replacement.\n\n"
            f"Current: {archive.archive_path}\n"
            f"Update file: {candidate}\n"
            f"Workshop revision to record: {_format_utc(item.row.workshop_updated)}\n\n"
            "Impact preview:\n"
            f"{impact_summary(impact)}\n"
            f"Uncompressed size change: {size_text}\n\n"
            "Only continue if this ZIP was obtained for the displayed Workshop revision. "
            "A ZIP descriptor identifies the mod but cannot prove the revision by itself.\n\n"
            f"{installed_text}{snapshot_text}\n\n"
            "Continue?",
        ):
            return
        self._begin_archive_operation("Validating and applying staged update…")
        plan_item = BatchPlanItem(
            archive.mod_id,
            archive.name or item.row.workshop_title or archive.archive_path.name,
            archive.archive_path,
            Path(candidate),
            item.row.workshop_updated,
            impact,
        )
        self.worker = threading.Thread(
            target=self._update_worker,
            args=(
                plan_item,
                active_mod is not None,
                self.playset,
                self._active_archives_by_mod_id(),
                self.latest_save,
                self.version_var.get(),
                self._enabled_mods_by_id(),
                self._ck3_data_directory(),
            ),
            daemon=True,
        )
        self.worker.start()

    def _update_worker(
        self,
        item: BatchPlanItem,
        preserve_setup: bool,
        playset: PlaysetState | None,
        archives: dict[str, list[Path]],
        save: SaveMetadata | None,
        game_version: str,
        enabled_mods: dict[str, Any],
        data_directory: Path,
    ) -> None:
        snapshot: SnapshotResult | None = None
        try:
            if preserve_setup and playset:
                snapshot = self.snapshot_manager.create(
                    f"Before updating {item.name}",
                    game_version,
                    playset,
                    archives,
                    save,
                )
            result = self.coordinated_update_manager.apply(
                [item],
                enabled_mods,
                data_directory,
            )
        except (CoordinatedUpdateError, SnapshotError) as error:
            self.ui_events.put((self._archive_operation_failed, (str(error),)))
            return
        self.ui_events.put((self._update_finished, (result, snapshot)))

    def _archive_operation_failed(self, detail: str) -> None:
        self.worker = None
        self.progress.stop()
        self.scan_button.configure(state="normal")
        self.export_button.configure(
            state="normal"
            if any(item.row.archive.status == "ck3_mod" for item in self.results)
            else "disabled"
        )
        self.batch_button.configure(
            state="normal"
            if any(item.row.archive.status == "ck3_mod" for item in self.results)
            else "disabled"
        )
        self.status_var.set("Operation failed")
        messagebox.showerror("Operation failed", detail)
        self._refresh_local_context()
        self.preflight_button.configure(state="normal" if self.preflight_report else "disabled")
        self._show_selected_details()

    def _update_finished(
        self,
        result: CoordinatedUpdateResult,
        snapshot: SnapshotResult | None,
    ) -> None:
        self.worker = None
        self.progress.stop()
        self.scan_button.configure(state="normal")
        self.status_var.set("Update committed and verified")
        archive_result = result.archive_updates[0]
        installed_text = (
            f"\nEnabled installation updated: {result.installed_updates[0].target_directory}"
            if result.installed_updates
            else "\nNo enabled installation was changed."
        )
        snapshot_text = f"\nPre-update snapshot: {snapshot.directory}" if snapshot else ""
        messagebox.showinfo(
            "Mod updated",
            "The replacement passed validation and previous files were backed up.\n\n"
            f"Archive: {archive_result.archive_path}\n"
            f"Archive backup: {archive_result.backup_path}"
            f"{installed_text}{snapshot_text}",
        )
        self.start_scan()

    def undo_selected_update(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        item = self._selected_result()
        if not item or not item.row.archive.mod_id:
            return
        try:
            coordinated_id = self.coordinated_update_manager.latest_recoverable(
                item.row.archive.mod_id
            )
            legacy_id = None if coordinated_id else self.update_manager.latest_recoverable(
                item.row.archive.mod_id, item.row.archive.archive_path
            )
        except (UpdateError, CoordinatedUpdateError) as error:
            messagebox.showerror("Backup unavailable", str(error))
            return
        transaction_id = coordinated_id or legacy_id
        if not transaction_id:
            messagebox.showinfo("Backup unavailable", "No recoverable update exists for this archive.")
            return
        coordinated = coordinated_id is not None
        if coordinated and not self._mod_apps_are_closed():
            return
        scope_text = (
            "This update may be part of a batch. Every archive and enabled installation in "
            "that transaction will be restored together."
            if coordinated
            else "Only the selected legacy archive transaction will be restored."
        )
        if not messagebox.askyesno(
            "Undo last mod update",
            "Restore the verified backups from the last committed update?\n\n"
            f"{scope_text}\n\n"
            "Rollback is refused if any managed path changed after that update. "
            "CK3, Irony, and the launcher must be closed for an installed-mod rollback.",
        ):
            return
        self._begin_archive_operation("Restoring the previous mod files…")
        self.worker = threading.Thread(
            target=self._undo_worker,
            args=(transaction_id, coordinated),
            daemon=True,
        )
        self.worker.start()

    def _undo_worker(self, transaction_id: str, coordinated: bool) -> None:
        try:
            result = (
                self.coordinated_update_manager.rollback(transaction_id)
                if coordinated
                else self.update_manager.rollback(transaction_id)
            )
        except (LedgerError, UpdateError, CoordinatedUpdateError) as error:
            self.ui_events.put((self._archive_operation_failed, (str(error),)))
            return
        self.ui_events.put((self._undo_finished, (result,)))

    def _undo_finished(self, result: UpdateResult | CoordinatedRollbackResult) -> None:
        self.worker = None
        self.progress.stop()
        self.scan_button.configure(state="normal")
        self.status_var.set("Previous mod files restored")
        if isinstance(result, CoordinatedRollbackResult):
            detail = (
                f"Restored archives: {result.archive_count}\n"
                f"Restored enabled installations: {result.installed_count}"
            )
        else:
            detail = f"The previous archive was restored and verified:\n{result.archive_path}"
        messagebox.showinfo(
            "Update undone",
            detail,
        )
        self.start_scan()

    def _report_rows(self) -> list[dict[str, Any]]:
        return build_report_rows(self.results)

    def export_report(self) -> None:
        if not self.results:
            return
        path = filedialog.asksaveasfilename(
            title="Export CK3 mod report",
            defaultextension=".json",
            filetypes=(("JSON report", "*.json"), ("CSV report", "*.csv")),
            initialfile=f"ck3-mod-scan-{datetime.now():%Y%m%d-%H%M%S}.json",
        )
        if not path:
            return
        try:
            report_rows = self._report_rows()
            if Path(path).suffix.lower() == ".csv":
                with open(path, "w", newline="", encoding="utf-8-sig") as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(report_rows[0]))
                    writer.writeheader()
                    writer.writerows(report_rows)
            else:
                payload = {
                    "application_version": APP_VERSION,
                    "scan_time_utc": self.last_scan_utc,
                    "ck3_version": self.version_var.get().strip(),
                    "ignored_non_ck3_zip_count": sum(
                        item.row.archive.status != "ck3_mod" for item in self.results
                    ),
                    "disclaimer": (
                        "Archive timestamps are estimates and do not prove an installed Workshop revision."
                    ),
                    "results": report_rows,
                }
                Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as error:
            messagebox.showerror("Export failed", str(error))
            return
        messagebox.showinfo("Report exported", f"Saved the complete scan to:\n{path}")

    def _on_close(self) -> None:
        self.closing = True
        try:
            save_settings(self._current_settings())
        except OSError:
            pass
        self.destroy()


def main() -> None:
    app = CK3ModUpdaterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
