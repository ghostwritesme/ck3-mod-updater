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
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk
from typing import Any, Callable

import requests
import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk
from background_art import (
    choose_background_art,
    render_background_art,
    render_fallback_texture,
)
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
    inspect_mod_archive,
    scan_archives,
    sha256_file,
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
APP_VERSION = "0.6.0"
WORKSHOP_ITEM_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
SMODS_SEARCH_URL = "https://catalogue.smods.ru/?q={}"


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


def _explorer_select_command(path: Path) -> str:
    """Build Explorer's exact select-file command line, including paths with spaces."""
    return f'explorer.exe /select,"{Path(path).resolve()}"'


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
        "background_mode": "ck3",
        "custom_background": "",
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
    if defaults["background_mode"] not in {"ck3", "custom", "none"}:
        defaults["background_mode"] = "ck3"
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


@dataclass(frozen=True)
class UpdateCandidateProblem:
    title: str
    message: str


def validate_update_candidate(
    current_archive: Path,
    candidate_archive: Path,
    expected_mod_id: str,
    expected_name: str,
) -> UpdateCandidateProblem | None:
    """Explain common selection mistakes before snapshots or transactions begin."""
    current = Path(current_archive).resolve()
    candidate = Path(candidate_archive).resolve()
    if current == candidate:
        return UpdateCandidateProblem(
            "Current archive selected",
            "You selected the ZIP that is already in the archive library.\n\n"
            f"Current archive:\n{current}\n\n"
            f"Selected file:\n{candidate}\n\n"
            "Choose the copied or newly downloaded ZIP from a different folder. "
            "No files were changed.",
        )
    if not candidate.is_file() or candidate.is_symlink():
        return UpdateCandidateProblem(
            "Update file rejected",
            f"The selected update file is missing or unsafe:\n{candidate}\n\nNo files were changed.",
        )
    selected = inspect_mod_archive(candidate)
    if selected.status != "ck3_mod" or not selected.mod_id:
        return UpdateCandidateProblem(
            "Invalid CK3 mod archive",
            f"The selected ZIP has no valid CK3 Workshop ID:\n{candidate}\n\nNo files were changed.",
        )
    if selected.mod_id != expected_mod_id:
        selected_name = selected.name or candidate.name
        return UpdateCandidateProblem(
            "Different mod selected",
            f"Expected:\n{expected_name} — Workshop ID {expected_mod_id}\n\n"
            f"Selected:\n{selected_name} — Workshop ID {selected.mod_id}\n"
            f"{candidate}\n\n"
            "The selected ZIP belongs to another mod. No files were changed.",
        )
    try:
        identical = sha256_file(current) == sha256_file(candidate)
    except OSError as error:
        return UpdateCandidateProblem(
            "Update file unreadable",
            f"The selected ZIP could not be compared with the current archive:\n{error}\n\n"
            "No files were changed.",
        )
    if identical:
        return UpdateCandidateProblem(
            "Identical archive selected",
            "The selected ZIP is a separate file, but it is byte-for-byte identical to the "
            "current archive. This confirms the identical-copy safety check.\n\n"
            f"Current archive:\n{current}\n\n"
            f"Selected copy:\n{candidate}\n\n"
            "No files were changed.",
        )
    return None


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
        self._high_resolution_timer = False
        if sys.platform == "win32":
            try:
                self._high_resolution_timer = ctypes.windll.winmm.timeBeginPeriod(1) == 0
            except (AttributeError, OSError):
                pass
        self.withdraw()
        self.title(APP_NAME)
        icon_path = _application_asset("icon.ico")
        if sys.platform == "win32" and icon_path.is_file():
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.geometry("1180x760")
        self.minsize(900, 620)
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
        self._background_source: Path | None = None
        self._background_photo: ImageTk.PhotoImage | None = None
        self._background_rendered_image: Image.Image | None = None
        self._shell_background_photo: ImageTk.PhotoImage | None = None
        self._main_canvas_art_photo: ImageTk.PhotoImage | None = None
        self._main_canvas_art_cache_key: tuple[Any, ...] | None = None
        self._background_render_job: str | None = None
        self._action_result: ResultItem | None = None
        self._pending_update_candidate: Path | None = None
        self._initial_scan_started = False
        self._loaded_private_fonts: list[Path] = []
        self.previous_history = load_history(RETURN_HISTORY_FILE)
        self.closing = False
        self.ui_events: queue.Queue[tuple[Callable[..., None], tuple[Any, ...]]] = queue.Queue()

        self.archive_var = tk.StringVar(value=self.settings["archive_directory"])
        self.mod_var = tk.StringVar(value=self.settings["mod_directory"])
        root_hint = self.settings.get("game_root", "").strip()
        self.installation: CK3Installation | None = detect_ck3_installation(
            [Path(root_hint)] if root_hint else []
        )
        self._load_interface_fonts()
        self.version_var = tk.StringVar(
            value=self.installation.version if self.installation else "Not detected"
        )
        self.version_source_var = tk.StringVar(
            value=str(self.installation.game_root) if self.installation else "CK3 installation not found"
        )
        self.search_var = tk.StringVar()
        self.filter_var = tk.StringVar(value="CK3 mods")
        self.main_scope_var = tk.StringVar(value="Enabled" if self.installation else "All")
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
            self.main_scope_var.set("Enabled")
        else:
            self.main_scope_var.set("All")
        self._build_ui()
        self._build_advanced_window()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_ui_events)
        self.after(200, self._start_recovery_check)
        self._reveal_initial_window()

    def _apply_windows_frame_theme(self, dark: bool, window: tk.Misc | None = None) -> None:
        if sys.platform != "win32":
            return
        target = window or self
        try:
            target.update_idletasks()
            child = target.winfo_id()
            get_parent = ctypes.windll.user32.GetParent
            get_parent.argtypes = (ctypes.c_void_p,)
            get_parent.restype = ctypes.c_void_p
            frame = get_parent(ctypes.c_void_p(child)) or child
            enabled = ctypes.c_int(1 if dark else 0)
            for attribute in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(frame),
                    attribute,
                    ctypes.byref(enabled),
                    ctypes.sizeof(enabled),
                )

            def colorref(value: str) -> ctypes.c_uint:
                red, green, blue = (int(value[index : index + 2], 16) for index in (1, 3, 5))
                return ctypes.c_uint(red | green << 8 | blue << 16)

            for attribute, color in (
                (34, "#3B3833" if dark else "#9D8D76"),
                (35, "#211416" if dark else "#D8C7AE"),
                (36, "#DED6BF" if dark else "#2D2923"),
            ):
                native_color = colorref(color)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(frame),
                    attribute,
                    ctypes.byref(native_color),
                    ctypes.sizeof(native_color),
                )
        except (AttributeError, OSError, tk.TclError):
            pass

    def _reveal_initial_window(self) -> None:
        self.update_idletasks()
        if self._background_render_job:
            self.after_cancel(self._background_render_job)
            self._background_render_job = None
        self._render_background()
        self.update_idletasks()
        self._apply_windows_frame_theme(self.settings.get("theme") == "dark")
        try:
            self.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        self.deiconify()

    def _load_interface_fonts(self) -> None:
        """Load locally installed CK3 interface fonts for this process when available."""
        if sys.platform != "win32" or not self.installation:
            return
        fonts_root = self.installation.game_root / "game" / "fonts"
        candidates = (
            fonts_root / "Fondamento" / "Fondamento-Regular.ttf",
            fonts_root / "Open_Sans" / "OpenSans-Regular.ttf",
            fonts_root / "Open_Sans" / "OpenSans-SemiBold.ttf",
        )
        add_font = ctypes.windll.gdi32.AddFontResourceExW
        for path in candidates:
            if not path.is_file():
                continue
            try:
                if add_font(str(path), 0x10, 0):
                    self._loaded_private_fonts.append(path)
            except OSError:
                continue

    def _build_ui(self) -> None:
        dark = self.settings.get("theme", "dark") == "dark"
        ctk.set_appearance_mode("Dark" if dark else "Light")
        colors = {
            "background": "#0F1214" if dark else "#E7E1D3",
            "panel": "#151719" if dark else "#F4EFE4",
            "field": "#1A1A21" if dark else "#FBF8F0",
            "header": "#211416" if dark else "#D8C7AE",
            "text": "#DED6BF" if dark else "#2D2923",
            "muted": "#9C998F" if dark else "#6A6359",
            "border": "#3B3833" if dark else "#9D8D76",
            "bronze": "#AD8F69" if dark else "#725237",
            "primary": "#725237",
            "primary_hover": "#866040",
            "attention": "#D1C47F" if dark else "#7A651F",
        }
        outer = tk.Frame(self, borderwidth=0, highlightthickness=0)
        outer.pack(fill="both", expand=True)
        self.outer = outer
        self.background_label = tk.Label(outer, borderwidth=0, highlightthickness=0)
        self.background_label.place(x=0, y=0, relwidth=1, relheight=1)
        self.background_label.lower()
        outer.bind("<Configure>", self._schedule_background_render)

        self.main_shell = ctk.CTkFrame(
            outer,
            fg_color=colors["panel"],
            border_width=1,
            border_color=colors["border"],
            corner_radius=3,
        )
        self.main_shell.pack(fill="both", expand=True, padx=16, pady=16)
        self.main_shell_background_label = tk.Label(
            self.main_shell,
            borderwidth=0,
            highlightthickness=0,
            background=colors["panel"],
        )
        self.main_shell_background_label.place(x=0, y=0, relwidth=1, relheight=1)

        self.main_header = ctk.CTkFrame(
            self.main_shell,
            height=68,
            fg_color=colors["header"],
            border_width=1,
            border_color=colors["bronze"],
            corner_radius=2,
        )
        self.main_header.pack(fill="x")
        self.main_header.pack_propagate(False)
        self.header_pattern_photo = self._header_pattern(colors["header"], dark)
        self.header_pattern_label = ctk.CTkLabel(
            self.main_header,
            image=self.header_pattern_photo,
            text="",
            fg_color="transparent",
        )
        self.header_pattern_label.place(x=0, y=0, relwidth=1, relheight=1)
        icon_path = _application_asset("icon.ico")
        self.app_icon_photo: ctk.CTkImage | None = None
        if icon_path.is_file():
            try:
                with Image.open(icon_path) as opened:
                    icon = opened.convert("RGBA")
                self.app_icon_photo = ctk.CTkImage(icon, size=(40, 40))
            except OSError:
                pass
        self.main_icon_label: ctk.CTkLabel | None = None
        if self.app_icon_photo:
            self.main_icon_label = ctk.CTkLabel(
                self.main_header,
                image=self.app_icon_photo,
                text="",
                fg_color="transparent",
                width=40,
                height=40,
            )
            self.main_icon_label.pack(side="left", padx=(28, 12))
        identity = ctk.CTkFrame(self.main_header, fg_color="transparent")
        identity.pack(side="left")
        self.main_title_label = ctk.CTkLabel(
            identity,
            text=APP_NAME,
            text_color=colors["text"],
            fg_color="transparent",
            font=ctk.CTkFont(family="Fondamento", size=21),
            height=25,
        )
        self.main_title_label.pack(anchor="w")
        self.main_subtitle_label = ctk.CTkLabel(
            identity,
            text="Keep your enabled mods current",
            text_color=colors["muted"],
            fg_color="transparent",
            font=ctk.CTkFont(family="Open Sans", size=11),
            height=18,
        )
        self.main_subtitle_label.pack(anchor="w")

        self.advanced_button = ctk.CTkButton(
            self.main_header,
            text="Advanced",
            command=self._show_advanced,
            width=90,
            height=32,
            corner_radius=3,
            border_width=1,
            border_color=colors["border"],
            fg_color=colors["field"],
            hover_color="#25231F" if dark else "#E4D7C5",
            text_color=colors["text"],
            font=ctk.CTkFont(family="Open Sans", size=12),
        )
        self.advanced_button.pack(side="right", padx=(8, 28))
        self.theme_button = ctk.CTkButton(
            self.main_header,
            text="Theme",
            command=self._toggle_theme,
            width=82,
            height=32,
            corner_radius=3,
            border_width=0,
            fg_color="transparent",
            hover_color="#2A2020" if dark else "#CDB99D",
            text_color=colors["muted"],
            font=ctk.CTkFont(family="Open Sans", size=12),
        )
        self.theme_button.pack(side="right", padx=(0, 8))

        self.main_summary = ctk.CTkFrame(
            self.main_shell,
            height=52,
            fg_color=colors["field"],
            border_width=1,
            border_color=colors["border"],
            corner_radius=3,
        )
        self.main_summary.pack(fill="x", padx=28, pady=(24, 0))
        self.main_summary.pack_propagate(False)
        self.main_status_label = ctk.CTkLabel(
            self.main_summary,
            textvariable=self.status_var,
            text_color=colors["text"],
            fg_color="transparent",
            font=ctk.CTkFont(family="Open Sans", size=13),
        )
        self.main_status_label.pack(side="left", padx=14)
        self.scan_button = ctk.CTkButton(
            self.main_summary,
            text="Scan again",
            command=self.start_scan,
            width=90,
            height=32,
            corner_radius=3,
            border_width=1,
            border_color=colors["border"],
            fg_color=colors["field"],
            hover_color="#25231F" if dark else "#E4D7C5",
            text_color=colors["text"],
            font=ctk.CTkFont(family="Open Sans", size=12),
        )
        self.scan_button.pack(side="right", padx=10)
        self.progress = ctk.CTkProgressBar(
            self.main_summary,
            width=96,
            height=6,
            corner_radius=3,
            mode="indeterminate",
            fg_color=colors["border"],
            progress_color=colors["bronze"],
        )
        self.progress.set(0)

        self.main_tabs = ctk.CTkFrame(
            self.main_shell,
            height=42,
            fg_color=colors["panel"],
            corner_radius=0,
        )
        self.main_tabs.pack(fill="x", padx=28, pady=(20, 0))
        self.main_tabs.pack_propagate(False)
        self.tabs_separator = ctk.CTkFrame(
            self.main_tabs,
            height=1,
            fg_color=colors["border"],
            corner_radius=0,
        )
        self.tabs_separator.place(x=0, rely=1, y=-1, relwidth=1)
        self.scope_buttons: dict[str, ctk.CTkButton] = {}
        self.scope_indicators: dict[str, ctk.CTkFrame] = {}
        for scope in ("Enabled", "Installed", "Downloaded", "All"):
            tab = ctk.CTkFrame(
                self.main_tabs,
                width=100,
                fg_color="transparent",
                corner_radius=0,
            )
            tab.pack(side="left", fill="y")
            tab.pack_propagate(False)
            button = ctk.CTkButton(
                tab,
                text=scope,
                command=lambda selected=scope: self._set_main_scope(selected),
                width=100,
                height=38,
                corner_radius=0,
                border_width=0,
                fg_color="transparent",
                hover_color="#201F1E" if dark else "#E9E1D3",
                text_color=colors["muted"],
                font=ctk.CTkFont(family="Open Sans", size=12),
            )
            button.pack(side="top")
            indicator = ctk.CTkFrame(
                tab,
                height=2,
                fg_color="transparent",
                corner_radius=0,
            )
            indicator.pack(side="bottom", fill="x")
            self.scope_buttons[scope] = button
            self.scope_indicators[scope] = indicator
        self.tab_indicator_canvas = tk.Canvas(
            self.main_tabs,
            height=2,
            background=colors["panel"],
            borderwidth=0,
            highlightthickness=0,
        )
        self.tab_indicator_canvas.place(x=0, y=40, relwidth=1)
        self.active_tab_indicator = self.tab_indicator_canvas.create_rectangle(
            0,
            0,
            100,
            2,
            fill=colors["bronze"],
            outline="",
        )
        self._tab_indicator_job: str | None = None

        self.main_rows_inner = ctk.CTkFrame(
            self.main_shell,
            fg_color=colors["panel"],
            border_width=1,
            border_color=colors["border"],
            corner_radius=3,
        )
        self.main_rows_inner.pack(fill="both", expand=True, padx=28, pady=(12, 0))
        self.main_rows_canvas = tk.Canvas(
            self.main_rows_inner,
            background=colors["panel"],
            borderwidth=0,
            highlightthickness=0,
            takefocus=True,
        )
        self.main_rows_canvas.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        self.main_rows_scrollbar = ctk.CTkScrollbar(
            self.main_rows_inner,
            command=self.main_rows_canvas.yview,
            width=12,
            corner_radius=3,
            fg_color=colors["panel"],
            button_color="#514A40" if dark else "#9D8D76",
            button_hover_color=colors["bronze"],
        )
        self.main_rows_scrollbar.pack(side="right", fill="y", padx=(1, 2), pady=3)
        self.main_rows_canvas.configure(yscrollcommand=self.main_rows_scrollbar.set)
        self._main_name_canvas_font = tkfont.Font(
            self,
            family="Open Sans",
            size=10,
            weight="bold",
        )
        self._main_status_canvas_font = tkfont.Font(
            self,
            family="Open Sans",
            size=9,
            weight="bold",
        )
        self._main_icon_canvas_font = tkfont.Font(
            self,
            family="Open Sans",
            size=12,
            weight="bold",
        )
        self._main_action_canvas_font = tkfont.Font(self, family="Open Sans", size=9)
        self._main_canvas_rows: list[
            tuple[str, str, str | None, str, str, str, str, ResultItem | None]
        ] = []
        self._main_action_regions: list[tuple[float, float, float, float, Callable[[], None]]] = []
        self._main_row_items: list[int] = []
        self._main_row_colors: list[str] = []
        self._main_button_items: list[tuple[int, int, str, str, str, str, str, str]] = []
        self._main_hover_row: int | None = None
        self._main_hover_button: int | None = None
        self._main_canvas_draw_job: str | None = None
        self._main_scroll_job: str | None = None
        self._main_scroll_target = 0.0
        self._main_row_hover_jobs: dict[int, str] = {}
        self._main_keyboard_row = 0
        self.main_rows_canvas.bind("<Configure>", self._schedule_main_canvas_draw)
        self.main_rows_canvas.bind("<Motion>", self._main_canvas_motion)
        self.main_rows_canvas.bind("<Leave>", self._main_canvas_leave)
        self.main_rows_canvas.bind("<Button-1>", self._main_canvas_click)
        self.main_rows_canvas.bind("<MouseWheel>", self._main_canvas_wheel)
        self.main_rows_canvas.bind("<Up>", lambda event: self._move_main_keyboard_row(-1))
        self.main_rows_canvas.bind("<Down>", lambda event: self._move_main_keyboard_row(1))
        self.main_rows_canvas.bind("<Return>", self._activate_main_keyboard_row)
        self.main_rows_canvas.bind("<space>", self._activate_main_keyboard_row)

        self.main_footer = ctk.CTkFrame(
            self.main_shell,
            height=72,
            fg_color=colors["panel"],
            border_width=0,
            border_color=colors["border"],
            corner_radius=0,
        )
        self.main_footer.pack(fill="x")
        self.main_footer.pack_propagate(False)
        self.footer_separator = ctk.CTkFrame(
            self.main_footer,
            height=1,
            fg_color=colors["border"],
            corner_radius=0,
        )
        self.footer_separator.pack(fill="x", padx=28)
        self.main_footer_label = ctk.CTkLabel(
            self.main_footer,
            text="Updates are downloaded from the matching catalogue.",
            text_color=colors["muted"],
            fg_color="transparent",
            font=ctk.CTkFont(family="Open Sans", size=11),
        )
        self.main_footer_label.pack(side="left", padx=28)
        self.install_zip_button = ctk.CTkButton(
            self.main_footer,
            text="Install downloaded ZIP…",
            command=self._install_downloaded_zip,
            width=160,
            height=32,
            corner_radius=3,
            border_width=1,
            border_color=colors["border"],
            fg_color=colors["field"],
            hover_color="#25231F" if dark else "#E4D7C5",
            text_color=colors["text"],
            font=ctk.CTkFont(family="Open Sans", size=12),
        )
        self.install_zip_button.pack(side="right", padx=28)

        self._apply_theme(self.settings["theme"])
        self._set_main_scope(self.main_scope_var.get())
        self._refresh_background_art()

    @staticmethod
    def _header_pattern(header_color: str, dark: bool) -> ctk.CTkImage:
        scale = 3
        width, height = 1800 * scale, 68 * scale
        image = Image.new("RGB", (width, height), header_color)
        draw = ImageDraw.Draw(image)
        line_color = "#2A1C1D" if dark else "#C8B598"
        tile = 32 * scale
        for x in range(-tile, width + tile, tile):
            for y in range(-tile, height + tile, tile):
                draw.line((x, y + tile // 2, x + tile // 2, y), fill=line_color, width=2)
                draw.line((x, y + tile // 2, x + tile // 2, y + tile), fill=line_color, width=2)
        image = image.resize((1800, 68), Image.Resampling.LANCZOS)
        return ctk.CTkImage(light_image=image, dark_image=image, size=(1800, 68))

    def _show_advanced(self) -> None:
        self._apply_windows_frame_theme(
            self.settings.get("theme") == "dark",
            self.advanced_window,
        )
        self.advanced_window.deiconify()
        self.advanced_window.lift()
        self.advanced_window.focus_force()

    def _set_main_scope(self, scope: str) -> None:
        if scope not in {"Enabled", "Installed", "Downloaded", "All"}:
            scope = "All"
        self.main_scope_var.set(scope)
        palette = getattr(self, "_theme_palette", {})
        text = palette.get("text", "#DED6BF")
        muted = palette.get("muted", "#9C998F")
        bronze = palette.get("bronze", "#AD8F69")
        for name, button in self.scope_buttons.items():
            selected = name == scope
            button.configure(text_color=text if selected else muted)
            self.scope_indicators[name].configure(fg_color="transparent")
        self.tab_indicator_canvas.itemconfigure(self.active_tab_indicator, fill=bronze)
        self._animate_tab_indicator(("Enabled", "Installed", "Downloaded", "All").index(scope) * 100)
        self._refresh_main_rows()

    def _animate_tab_indicator(self, target_x: int) -> None:
        if self._tab_indicator_job:
            try:
                self.after_cancel(self._tab_indicator_job)
            except (tk.TclError, ValueError):
                pass
        coordinates = self.tab_indicator_canvas.coords(self.active_tab_indicator)
        start_x = coordinates[0] if coordinates else 0
        started = time.perf_counter()
        duration = 0.22

        def advance() -> None:
            self._tab_indicator_job = None
            progress = min(1.0, (time.perf_counter() - started) / duration)
            eased = 1 - (1 - progress) ** 3
            x = start_x + (target_x - start_x) * eased
            self.tab_indicator_canvas.coords(
                self.active_tab_indicator,
                x,
                0,
                x + 100,
                2,
            )
            if progress < 1:
                self._tab_indicator_job = self.after(5, advance)

        advance()

    def _main_matches_scope(self, item: ResultItem) -> bool:
        if item.row.archive.status != "ck3_mod":
            return False
        scope = self.main_scope_var.get()
        if scope == "Enabled":
            return item.enabled
        if scope == "Installed":
            return item.installed
        if scope == "Downloaded":
            return not item.installed
        return True

    @staticmethod
    def _main_status(item: ResultItem) -> tuple[str, str, str, str]:
        row = item.row
        eligible = bool(
            row.archive.mod_id
            and row.workshop_status == "available"
            and row.workshop_updated
            and row.duplicate_count == 1
        )
        if row.workshop_status in {"remote_unavailable", "scan_error"}:
            return "×", "Check failed", "Open source", "source"
        if row.baseline_status == "changed_since_recorded_install":
            return "↑", "Update available", "Get update", "source"
        if row.baseline_status == "no_change_since_recorded_install":
            return "✓", "Up to date", "Open source", "source"
        if eligible:
            return "!", "Version not recorded", "Record installed version", "record"
        return "!", "Version not recorded", "Open source", "source"

    def _run_main_action(self, item: ResultItem, action: str) -> None:
        if action == "source":
            self._open_smods(item.row.archive.mod_id)
            return
        self._action_result = item
        try:
            if action == "record":
                self.record_selected_baseline()
        finally:
            self._action_result = None

    @staticmethod
    def _open_smods(mod_id: str | None) -> None:
        if mod_id:
            webbrowser.open(SMODS_SEARCH_URL.format(mod_id))

    def _main_rows_for_scope(
        self,
        scope: str,
    ) -> list[tuple[str, str, str | None, str, str, str, str, ResultItem | None]]:
        items = [
            item
            for item in self.results
            if item.row.archive.status == "ck3_mod"
            and (
                scope == "All"
                or (scope == "Enabled" and item.enabled)
                or (scope == "Installed" and item.installed)
                or (scope == "Downloaded" and not item.installed)
            )
        ]
        if scope == "Enabled":
            items.sort(key=lambda item: item.playset_position or 100000)
        else:
            items.sort(
                key=lambda item: _natural_text_key(
                    item.row.archive.name
                    or item.row.workshop_title
                    or item.row.archive.archive_path.name
                )
            )

        rows: list[
            tuple[str, str, str | None, str, str, str, str, ResultItem | None]
        ] = []
        for item in items:
            icon, status, action_label, action = self._main_status(item)
            name = (
                item.row.archive.name
                or item.row.workshop_title
                or item.row.archive.archive_path.name
            )
            key = "archive:" + str(item.row.archive.archive_path.resolve()).casefold()
            rows.append(
                (key, name, item.row.archive.mod_id, icon, status, action_label, action, item)
            )

        if scope == "Enabled" and self.playset:
            available_ids = {
                item.row.archive.mod_id
                for item in self.results
                if item.row.archive.status == "ck3_mod" and item.row.archive.mod_id
            }
            for mod in self.playset.mods:
                if mod.mod_id and mod.mod_id not in available_ids:
                    rows.append(
                        (
                            f"missing:{mod.mod_id}",
                            mod.display_name,
                            mod.mod_id,
                            "↓",
                            "Download missing",
                            "Find download",
                            "missing",
                            None,
                        )
                    )
        return rows

    @staticmethod
    def _blend_canvas_color(start: str, end: str, amount: float) -> str:
        start_rgb = tuple(int(start[index : index + 2], 16) for index in (1, 3, 5))
        end_rgb = tuple(int(end[index : index + 2], 16) for index in (1, 3, 5))
        return "#" + "".join(
            f"{round(source + (target - source) * amount):02X}"
            for source, target in zip(start_rgb, end_rgb)
        )

    def _animate_main_canvas_row(self, index: int, target: str) -> None:
        if not 0 <= index < len(self._main_row_items):
            return
        pending = self._main_row_hover_jobs.pop(index, None)
        if pending:
            try:
                self.after_cancel(pending)
            except (tk.TclError, ValueError):
                pass
        item = self._main_row_items[index]
        start = self.main_rows_canvas.itemcget(item, "fill")
        if not start.startswith("#"):
            start = target
        started = time.perf_counter()
        duration = 0.14

        def advance() -> None:
            self._main_row_hover_jobs.pop(index, None)
            if not self.main_rows_canvas.winfo_exists():
                return
            progress = min(1.0, (time.perf_counter() - started) / duration)
            eased = 1 - (1 - progress) ** 3
            color = self._blend_canvas_color(start, target, eased)
            self.main_rows_canvas.itemconfigure(item, fill=color)
            if progress < 1:
                self._main_row_hover_jobs[index] = self.after(5, advance)

        advance()

    def _reset_main_canvas(self) -> None:
        if not hasattr(self, "main_rows_canvas"):
            return
        if self._main_canvas_draw_job:
            self.after_cancel(self._main_canvas_draw_job)
            self._main_canvas_draw_job = None
        if self._main_scroll_job:
            self.after_cancel(self._main_scroll_job)
            self._main_scroll_job = None
        for pending in self._main_row_hover_jobs.values():
            try:
                self.after_cancel(pending)
            except (tk.TclError, ValueError):
                pass
        self._main_row_hover_jobs.clear()
        self._main_hover_row = None
        self._main_hover_button = None
        self.main_rows_canvas.delete("all")

    def _schedule_main_canvas_draw(self, _event: Any = None) -> None:
        if self._main_canvas_draw_job:
            self.after_cancel(self._main_canvas_draw_job)
        self._main_canvas_draw_job = self.after(16, self._draw_main_canvas)

    def _ellipsize_main_name(self, value: str, width: int) -> str:
        if width <= 20:
            return ""
        if self._main_name_canvas_font.measure(value) <= width:
            return value
        suffix = "…"
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            if self._main_name_canvas_font.measure(value[:middle] + suffix) <= width:
                low = middle
            else:
                high = middle - 1
        return value[:low] + suffix

    def _draw_main_canvas(self) -> None:
        self._main_canvas_draw_job = None
        canvas = self.main_rows_canvas
        if not canvas.winfo_exists():
            return
        palette = self._theme_palette
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        for pending in self._main_row_hover_jobs.values():
            try:
                self.after_cancel(pending)
            except (tk.TclError, ValueError):
                pass
        self._main_row_hover_jobs.clear()
        canvas.delete("all")
        self._main_action_regions.clear()
        self._main_row_items.clear()
        self._main_row_colors.clear()
        self._main_button_items.clear()
        self._main_hover_row = None
        self._main_hover_button = None
        rows = self._main_canvas_rows
        total_height = max(height, len(rows) * 67)
        canvas.configure(
            background=palette["panel"],
            scrollregion=(0, 0, width, total_height),
        )
        canvas_art = self._main_canvas_background(width, height)
        if canvas_art is not None:
            canvas.create_image(0, 0, image=canvas_art, anchor="nw")
        if not rows:
            message = "Scan to check your mods." if not self.results else "No mods are available in this view."
            canvas.create_text(
                width / 2,
                min(height, 160) / 2,
                text=message,
                fill=palette["muted"],
                font=self._main_action_canvas_font,
            )
            return

        status_colors = {
            "Up to date": palette["success"],
            "Update available": palette["bronze"],
            "Version not recorded": palette["attention"],
            "Download missing": palette["attention"],
            "Check failed": palette["error"],
        }
        action_right = width - 18
        action_left = max(480, action_right - 156)
        status_x = max(250, action_left - 220)
        name_width = max(40, status_x - 48)
        for index, spec in enumerate(rows):
            _key, name, mod_id, icon, status, action_label, action, item = spec
            top = index * 67
            center = top + 33
            resting = palette["alternate"] if index % 2 else palette["panel"]
            row_item = canvas.create_rectangle(
                0,
                top,
                width,
                top + 66,
                fill=resting,
                outline="",
            )
            self._main_row_items.append(row_item)
            self._main_row_colors.append(resting)
            canvas.create_line(0, top + 66, width, top + 66, fill=palette["border"])
            canvas.create_text(
                20,
                center,
                text=self._ellipsize_main_name(name, name_width),
                fill=palette["text"],
                font=self._main_name_canvas_font,
                anchor="w",
            )
            color = status_colors[status]
            canvas.create_text(
                status_x,
                center,
                text=icon,
                fill=color,
                font=self._main_icon_canvas_font,
                anchor="w",
            )
            canvas.create_text(
                status_x + 28,
                center,
                text=status,
                fill=color,
                font=self._main_status_canvas_font,
                anchor="w",
            )
            is_primary = status == "Update available"
            is_attention = status == "Download missing"
            rest_fill = palette["primary"] if is_primary else palette["field"]
            hover_fill = palette["primary_hover"] if is_primary else palette["hover"]
            rest_outline = palette["bronze"] if is_primary else color if is_attention else palette["border"]
            hover_outline = palette["bronze"] if is_primary else color if is_attention else palette["bronze"]
            rest_text = "#F1E8D3" if is_primary else color if is_attention else palette["text"]
            hover_text = "#F1E8D3" if is_primary else color if is_attention else palette["bronze"]
            button_top, button_bottom = center - 16, center + 16
            button_item = canvas.create_rectangle(
                action_left,
                button_top,
                action_right,
                button_bottom,
                fill=rest_fill,
                outline=rest_outline,
                width=1,
            )
            button_text = canvas.create_text(
                (action_left + action_right) / 2,
                center,
                text=action_label,
                fill=rest_text,
                font=self._main_action_canvas_font,
            )
            command = (
                (lambda selected=item, selected_action=action: self._run_main_action(selected, selected_action))
                if item
                else (lambda selected_id=mod_id: self._open_smods(selected_id))
            )
            self._main_action_regions.append(
                (action_left, button_top, action_right, button_bottom, command)
            )
            self._main_button_items.append(
                (
                    button_item,
                    button_text,
                    rest_fill,
                    hover_fill,
                    rest_outline,
                    hover_outline,
                    rest_text,
                    hover_text,
                )
            )

    def _set_main_button_hover(self, index: int | None) -> None:
        if index == self._main_hover_button:
            return
        canvas = self.main_rows_canvas
        if self._main_hover_button is not None and self._main_hover_button < len(self._main_button_items):
            button, text_item, rest_fill, _hover_fill, rest_outline, _hover_outline, rest_text, _hover_text = self._main_button_items[self._main_hover_button]
            canvas.itemconfigure(button, fill=rest_fill, outline=rest_outline)
            canvas.itemconfigure(text_item, fill=rest_text)
        self._main_hover_button = index
        if index is not None and index < len(self._main_button_items):
            button, text_item, _rest_fill, hover_fill, _rest_outline, hover_outline, _rest_text, hover_text = self._main_button_items[index]
            canvas.itemconfigure(button, fill=hover_fill, outline=hover_outline)
            canvas.itemconfigure(text_item, fill=hover_text)

    def _main_canvas_motion(self, event: tk.Event[tk.Misc]) -> None:
        canvas_y = self.main_rows_canvas.canvasy(event.y)
        index = int(canvas_y // 67)
        index = index if 0 <= index < len(self._main_canvas_rows) else None
        if index != self._main_hover_row:
            if self._main_hover_row is not None and self._main_hover_row < len(self._main_row_items):
                self._animate_main_canvas_row(
                    self._main_hover_row,
                    self._main_row_colors[self._main_hover_row],
                )
            self._main_hover_row = index
            if index is not None:
                self._animate_main_canvas_row(index, self._theme_palette["hover"])
        button_index = None
        if index is not None:
            left, top, right, bottom, _command = self._main_action_regions[index]
            if left <= event.x <= right and top <= canvas_y <= bottom:
                button_index = index
        self._set_main_button_hover(button_index)
        self.main_rows_canvas.configure(cursor="hand2" if button_index is not None else "")

    def _main_canvas_leave(self, _event: Any = None) -> None:
        if self._main_hover_row is not None and self._main_hover_row < len(self._main_row_items):
            self._animate_main_canvas_row(
                self._main_hover_row,
                self._main_row_colors[self._main_hover_row],
            )
        self._main_hover_row = None
        self._set_main_button_hover(None)
        self.main_rows_canvas.configure(cursor="")

    def _main_canvas_click(self, event: tk.Event[tk.Misc]) -> str | None:
        self.main_rows_canvas.focus_set()
        canvas_y = self.main_rows_canvas.canvasy(event.y)
        index = int(canvas_y // 67)
        if 0 <= index < len(self._main_action_regions):
            self._main_keyboard_row = index
            left, top, right, bottom, command = self._main_action_regions[index]
            if left <= event.x <= right and top <= canvas_y <= bottom:
                self.after_idle(command)
                return "break"
        return None

    def _move_main_keyboard_row(self, change: int) -> str:
        if not self._main_canvas_rows:
            return "break"
        self._main_keyboard_row = max(
            0,
            min(len(self._main_canvas_rows) - 1, self._main_keyboard_row + change),
        )
        canvas = self.main_rows_canvas
        height = max(1, canvas.winfo_height())
        total = max(height, len(self._main_canvas_rows) * 67)
        top = self._main_keyboard_row * 67
        visible_top = canvas.yview()[0] * total
        visible_bottom = visible_top + height
        if top < visible_top:
            canvas.yview_moveto(top / total)
        elif top + 67 > visible_bottom:
            canvas.yview_moveto((top + 67 - height) / total)
        return "break"

    def _activate_main_keyboard_row(self, _event: Any = None) -> str:
        if 0 <= self._main_keyboard_row < len(self._main_action_regions):
            self.after_idle(self._main_action_regions[self._main_keyboard_row][4])
        return "break"

    def _main_canvas_wheel(self, event: tk.Event[tk.Misc]) -> str:
        canvas = self.main_rows_canvas
        height = max(1, canvas.winfo_height())
        total = max(height, len(self._main_canvas_rows) * 67)
        if total <= height:
            return "break"
        current = canvas.yview()[0]
        base = self._main_scroll_target if self._main_scroll_job else current
        notches = event.delta / 120 if event.delta else 0
        target_pixels = max(0.0, min(total - height, base * total - notches * 148))
        target = target_pixels / total
        self._main_scroll_target = target
        if self._main_scroll_job:
            self.after_cancel(self._main_scroll_job)
        start = current
        started = time.perf_counter()
        duration = 0.24

        def advance() -> None:
            self._main_scroll_job = None
            progress = min(1.0, (time.perf_counter() - started) / duration)
            eased = 1 - (1 - progress) ** 3
            canvas.yview_moveto(start + (target - start) * eased)
            if progress < 1:
                self._main_scroll_job = self.after(5, advance)

        advance()
        return "break"

    def _refresh_main_rows(self) -> None:
        if not hasattr(self, "main_rows_canvas"):
            return
        if self._main_canvas_draw_job:
            try:
                self.after_cancel(self._main_canvas_draw_job)
            except (tk.TclError, ValueError):
                pass
            self._main_canvas_draw_job = None
        self._main_canvas_rows = self._main_rows_for_scope(self.main_scope_var.get())
        self._main_keyboard_row = 0
        self.main_rows_canvas.yview_moveto(0)
        self._main_scroll_target = 0.0
        self._draw_main_canvas()

    def _install_downloaded_zip(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        candidate = filedialog.askopenfilename(
            title="Choose a downloaded CK3 mod ZIP",
            initialdir=self.archive_var.get().strip() or None,
            filetypes=(("ZIP archives", "*.zip"),),
        )
        if not candidate:
            return
        candidate_path = Path(candidate)
        archive = inspect_mod_archive(candidate_path)
        if archive.status != "ck3_mod" or not archive.mod_id:
            messagebox.showerror(
                "Invalid CK3 mod archive",
                "The selected ZIP does not contain a valid CK3 Workshop mod descriptor.",
            )
            return
        matches = [
            item
            for item in self.results
            if item.row.archive.status == "ck3_mod"
            and item.row.archive.mod_id == archive.mod_id
        ]
        if len(matches) != 1:
            messagebox.showerror(
                "Matching archive unavailable",
                "The updater needs exactly one existing archive with the same Workshop ID. "
                "Scan the correct archive folder and try again.",
            )
            return
        self._pending_update_candidate = candidate_path
        self._action_result = matches[0]
        try:
            self.apply_selected_update()
        finally:
            self._action_result = None
            self._pending_update_candidate = None

    def _start_initial_scan(self) -> None:
        if self._initial_scan_started or self.worker and self.worker.is_alive():
            return
        self._initial_scan_started = True
        archive_directory = Path(self.archive_var.get().strip())
        if archive_directory.is_dir() and self.installation:
            self.start_scan()

    def _build_advanced_window(self) -> None:
        palette = self._theme_palette
        dark = self.settings.get("theme", "dark") == "dark"
        window = ctk.CTkToplevel(self, fg_color=palette["background"])
        window.withdraw()
        window.title(f"{APP_NAME} — Advanced")
        window.geometry("1320x820")
        window.minsize(1080, 720)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        self.advanced_window = window
        self._advanced_role_widgets: dict[str, list[Any]] = {}

        def remember(widget: Any, role: str) -> Any:
            self._advanced_role_widgets.setdefault(role, []).append(widget)
            return widget

        shell = remember(
            ctk.CTkFrame(
                window,
                fg_color=palette["panel"],
                border_width=1,
                border_color=palette["border"],
                corner_radius=3,
            ),
            "shell",
        )
        shell.pack(fill="both", expand=True, padx=16, pady=16)

        header = remember(
            ctk.CTkFrame(
                shell,
                height=68,
                fg_color=palette["header"],
                border_width=1,
                border_color=palette["bronze"],
                corner_radius=2,
            ),
            "header",
        )
        header.pack(fill="x")
        header.pack_propagate(False)
        self.advanced_header_pattern_photo = self._header_pattern(palette["header"], dark)
        self.advanced_header_pattern_label = ctk.CTkLabel(
            header,
            image=self.advanced_header_pattern_photo,
            text="",
            fg_color="transparent",
        )
        self.advanced_header_pattern_label.place(x=0, y=0, relwidth=1, relheight=1)
        if self.app_icon_photo:
            ctk.CTkLabel(
                header,
                image=self.app_icon_photo,
                text="",
                fg_color="transparent",
                width=40,
                height=40,
            ).pack(side="left", padx=(28, 12))
        identity = ctk.CTkFrame(header, fg_color="transparent")
        identity.pack(side="left")
        remember(
            ctk.CTkLabel(
                identity,
                text="Advanced tools",
                text_color=palette["text"],
                fg_color="transparent",
                font=ctk.CTkFont(family="Fondamento", size=21),
                height=25,
            ),
            "text",
        ).pack(anchor="w")
        remember(
            ctk.CTkLabel(
                identity,
                text="Setup protection, diagnostics, and archive management",
                text_color=palette["muted"],
                fg_color="transparent",
                font=ctk.CTkFont(family="Open Sans", size=11),
                height=18,
            ),
            "muted",
        ).pack(anchor="w")

        def advanced_button(
            master: tk.Misc,
            text: str,
            command: Callable[[], None],
            *,
            role: str = "button",
            width: int = 110,
            state: str = "normal",
        ) -> ctk.CTkButton:
            primary = role == "primary_button"
            quiet = role == "quiet_button"
            button = ctk.CTkButton(
                master,
                text=text,
                command=command,
                width=width,
                height=32,
                corner_radius=3,
                border_width=0 if quiet else 1,
                border_color=palette["bronze"] if primary else palette["border"],
                fg_color=palette["primary"] if primary else "transparent" if quiet else palette["field"],
                hover_color=palette["primary_hover"] if primary else palette["hover"],
                text_color="#F1E8D3" if primary else palette["muted"] if quiet else palette["text"],
                font=ctk.CTkFont(family="Open Sans", size=12),
                state=state,
            )
            remember(button, role)
            return button

        advanced_button(
            header,
            "Done",
            window.withdraw,
            role="button",
            width=78,
        ).pack(side="right", padx=(8, 28))
        self.advanced_theme_button = advanced_button(
            header,
            "Light theme" if dark else "Dark theme",
            self._toggle_theme,
            role="quiet_button",
            width=100,
        )
        self.advanced_theme_button.pack(side="right", padx=(0, 4))
        self.advanced_background_button = advanced_button(
            header,
            self._background_button_text(),
            lambda: None,
            role="quiet_button",
            width=178,
        )
        self.advanced_background_button.pack(side="right", padx=(0, 4))
        self.advanced_background_menu = tk.Menu(window, tearoff=False)
        self.advanced_background_menu.add_command(
            label="CK3 artwork",
            command=lambda: self._set_background_mode("ck3"),
        )
        self.advanced_background_menu.add_command(
            label="Choose custom image…",
            command=self._choose_custom_background,
        )
        self.advanced_background_menu.add_command(
            label="No artwork",
            command=lambda: self._set_background_mode("none"),
        )

        def show_background_menu() -> None:
            try:
                self.advanced_background_menu.tk_popup(
                    self.advanced_background_button.winfo_rootx(),
                    self.advanced_background_button.winfo_rooty()
                    + self.advanced_background_button.winfo_height(),
                )
            finally:
                self.advanced_background_menu.grab_release()

        self.advanced_background_button.configure(command=show_background_menu)

        body = ctk.CTkFrame(shell, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(16, 12))

        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        top.grid_columnconfigure(0, weight=3)
        top.grid_columnconfigure(1, weight=2)

        overview = remember(
            ctk.CTkFrame(
                top,
                height=158,
                fg_color=palette["field"],
                border_width=1,
                border_color=palette["border"],
                corner_radius=3,
            ),
            "panel",
        )
        overview.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        overview.grid_propagate(False)
        remember(
            ctk.CTkLabel(
                overview,
                text="Return overview",
                text_color=palette["text"],
                font=ctk.CTkFont(family="Open Sans", size=13, weight="bold"),
            ),
            "text",
        ).pack(anchor="w", padx=16, pady=(12, 3))
        summary_grid = ctk.CTkFrame(overview, fg_color="transparent")
        summary_grid.pack(fill="x", padx=16)
        for column in range(3):
            summary_grid.grid_columnconfigure(column, weight=1)
        for column, (label, variable) in enumerate(
            (
                ("Playset", self.playset_summary_var),
                ("Save", self.save_summary_var),
                ("Snapshot", self.snapshot_summary_var),
            )
        ):
            cell = ctk.CTkFrame(summary_grid, fg_color="transparent")
            cell.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
            remember(
                ctk.CTkLabel(
                    cell,
                    text=label.upper(),
                    text_color=palette["bronze"],
                    font=ctk.CTkFont(family="Open Sans", size=9, weight="bold"),
                    anchor="w",
                ),
                "bronze_text",
            ).pack(fill="x")
            remember(
                ctk.CTkLabel(
                    cell,
                    textvariable=variable,
                    text_color=palette["text"],
                    font=ctk.CTkFont(family="Open Sans", size=10),
                    anchor="w",
                ),
                "text",
            ).pack(fill="x")
        remember(
            ctk.CTkLabel(
                overview,
                textvariable=self.return_summary_var,
                text_color=palette["muted"],
                font=ctk.CTkFont(family="Open Sans", size=10),
                anchor="w",
                justify="left",
                wraplength=680,
            ),
            "muted",
        ).pack(fill="x", padx=16, pady=(6, 0))
        overview_actions = ctk.CTkFrame(overview, fg_color="transparent")
        overview_actions.pack(side="bottom", fill="x", padx=16, pady=(6, 12))
        self.snapshot_button = advanced_button(
            overview_actions,
            "Preserve setup",
            self.preserve_current_setup,
            role="primary_button",
            width=118,
            state="disabled",
        )
        self.snapshot_button.pack(side="left")
        self.preflight_button = advanced_button(
            overview_actions,
            "View preflight",
            self.show_preflight,
            width=110,
            state="disabled",
        )
        self.preflight_button.pack(side="left", padx=6)
        advanced_button(
            overview_actions,
            "Manage snapshots",
            self.manage_snapshots,
            width=130,
        ).pack(side="left")

        setup = remember(
            ctk.CTkFrame(
                top,
                height=158,
                fg_color=palette["field"],
                border_width=1,
                border_color=palette["border"],
                corner_radius=3,
            ),
            "panel",
        )
        setup.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        setup.grid_propagate(False)
        setup.grid_columnconfigure(1, weight=1)
        remember(
            ctk.CTkLabel(
                setup,
                text="Scan setup",
                text_color=palette["text"],
                font=ctk.CTkFont(family="Open Sans", size=13, weight="bold"),
            ),
            "text",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(10, 4))

        def setup_row(row: int, label: str, variable: tk.StringVar) -> None:
            remember(
                ctk.CTkLabel(
                    setup,
                    text=label,
                    text_color=palette["muted"],
                    font=ctk.CTkFont(family="Open Sans", size=10),
                    width=88,
                    anchor="w",
                ),
                "muted",
            ).grid(row=row, column=0, sticky="w", padx=(16, 6), pady=3)
            remember(
                ctk.CTkEntry(
                    setup,
                    textvariable=variable,
                    height=28,
                    corner_radius=2,
                    border_width=1,
                    border_color=palette["border"],
                    fg_color=palette["panel"],
                    text_color=palette["text"],
                    font=ctk.CTkFont(family="Open Sans", size=10),
                ),
                "entry",
            ).grid(row=row, column=1, sticky="ew", pady=3)
            advanced_button(
                setup,
                "Browse",
                lambda selected=variable: self._browse_directory(selected),
                width=64,
            ).grid(row=row, column=2, padx=(6, 16), pady=3)

        setup_row(1, "Archive folder", self.archive_var)
        setup_row(2, "Installed mods", self.mod_var)
        version_line = ctk.CTkFrame(setup, fg_color="transparent")
        version_line.grid(row=3, column=0, columnspan=3, sticky="ew", padx=16, pady=(5, 0))
        remember(
            ctk.CTkLabel(
                version_line,
                text="CK3",
                text_color=palette["muted"],
                font=ctk.CTkFont(family="Open Sans", size=10),
            ),
            "muted",
        ).pack(side="left")
        remember(
            ctk.CTkLabel(
                version_line,
                textvariable=self.version_var,
                text_color=palette["text"],
                font=ctk.CTkFont(family="Open Sans", size=10, weight="bold"),
            ),
            "text",
        ).pack(side="left", padx=(6, 8))
        advanced_button(
            version_line,
            "Detect",
            lambda: self._refresh_game_detection(show_error=True),
            width=64,
        ).pack(side="left")
        self.advanced_scan_button = advanced_button(
            version_line,
            "Scan archives",
            self.start_scan,
            role="primary_button",
            width=104,
        )
        self.advanced_scan_button.pack(side="right")
        self.advanced_progress = remember(
            ctk.CTkProgressBar(
                version_line,
                width=86,
                height=4,
                mode="indeterminate",
                fg_color=palette["border"],
                progress_color=palette["bronze"],
            ),
            "progress",
        )
        self.advanced_progress.set(0)
        self.advanced_progress.pack(side="right", padx=(0, 8))

        cards = ctk.CTkFrame(body, fg_color="transparent")
        cards.pack(fill="x", pady=(12, 0))
        for index, (key, label) in enumerate(
            (
                ("archives", "ZIP files"),
                ("ck3", "CK3 archives"),
                ("review", "Needs review"),
                ("installed", "Installed IDs"),
                ("enabled", "Enabled mods"),
            )
        ):
            cards.grid_columnconfigure(index, weight=1)
            card = remember(
                ctk.CTkFrame(
                    cards,
                    height=62,
                    fg_color=palette["panel"],
                    border_width=1,
                    border_color=palette["border"],
                    corner_radius=3,
                ),
                "card",
            )
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 5, 0))
            card.grid_propagate(False)
            remember(
                ctk.CTkLabel(
                    card,
                    text=label.upper(),
                    text_color=palette["muted"],
                    font=ctk.CTkFont(family="Open Sans", size=9, weight="bold"),
                    height=19,
                ),
                "muted",
            ).pack(pady=(5, 0))
            remember(
                ctk.CTkLabel(
                    card,
                    textvariable=self.summary_vars[key],
                    text_color=palette["text"],
                    font=ctk.CTkFont(family="Fondamento", size=20),
                    height=28,
                ),
                "text",
            ).pack()

        controls = ctk.CTkFrame(body, fg_color="transparent", height=42)
        controls.pack(fill="x", pady=(12, 8))
        controls.pack_propagate(False)
        self.advanced_search_entry = remember(
            ctk.CTkEntry(
                controls,
                textvariable=self.search_var,
                placeholder_text="Search mods and archives",
                width=260,
                height=32,
                corner_radius=3,
                border_width=1,
                border_color=palette["border"],
                fg_color=palette["field"],
                text_color=palette["text"],
                placeholder_text_color=palette["muted"],
                font=ctk.CTkFont(family="Open Sans", size=11),
            ),
            "entry",
        )
        self.advanced_search_entry.pack(side="left")
        filter_values = (
            "Enabled playset",
            "CK3 mods",
            "Recorded updates available",
            "All files",
            "Possible Workshop changes",
            "Declared for another CK3 version",
            "Installed",
            "Workshop unavailable/errors",
            "Duplicate archives",
        )
        self.advanced_filter_menu = remember(
            ctk.CTkOptionMenu(
                controls,
                variable=self.filter_var,
                values=list(filter_values),
                command=lambda _selection: self._apply_filter(),
                width=230,
                height=32,
                corner_radius=3,
                fg_color=palette["field"],
                button_color=palette["border"],
                button_hover_color=palette["bronze"],
                text_color=palette["text"],
                dropdown_fg_color=palette["field"],
                dropdown_text_color=palette["text"],
                dropdown_hover_color=palette["hover"],
                font=ctk.CTkFont(family="Open Sans", size=11),
                dropdown_font=ctk.CTkFont(family="Open Sans", size=11),
            ),
            "option",
        )
        self.advanced_filter_menu.pack(side="left", padx=8)
        self.export_button = advanced_button(
            controls,
            "Export report",
            self.export_report,
            width=108,
            state="disabled",
        )
        self.export_button.pack(side="right")
        self.batch_button = advanced_button(
            controls,
            "Batch update files…",
            self.queue_batch_updates,
            role="primary_button",
            width=142,
            state="disabled",
        )
        self.batch_button.pack(side="right", padx=(0, 8))

        content = ctk.CTkFrame(body, fg_color="transparent")
        content.pack(fill="both", expand=True)
        table_frame = remember(
            ctk.CTkFrame(
                content,
                fg_color=palette["panel"],
                border_width=1,
                border_color=palette["border"],
                corner_radius=3,
            ),
            "card",
        )
        table_frame.pack(fill="both", expand=True)
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
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=6)
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
            "name": 270,
            "id": 115,
            "installed": 78,
            "playset": 82,
            "version": 96,
            "compatibility": 160,
            "workshop": 160,
            "baseline": 200,
        }
        for column in columns:
            self.tree.heading(
                column,
                text=self.column_headings[column],
                command=lambda selected=column: self._sort_by_column(selected),
            )
            self.tree.column(column, width=widths[column], minwidth=70, anchor="w")
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_details)
        self.tree.bind("<Double-1>", lambda _event: self.open_workshop())
        tree_scrollbar = remember(
            ctk.CTkScrollbar(
                table_frame,
                command=self.tree.yview,
                width=12,
                fg_color=palette["panel"],
                button_color="#514A40" if dark else "#9D8D76",
                button_hover_color=palette["bronze"],
            ),
            "scrollbar",
        )
        self.tree.configure(yscrollcommand=tree_scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        tree_scrollbar.pack(side="right", fill="y", padx=(0, 2), pady=3)

        details_frame = remember(
            ctk.CTkFrame(
                content,
                height=136,
                fg_color=palette["field"],
                border_width=1,
                border_color=palette["border"],
                corner_radius=3,
            ),
            "panel",
        )
        details_frame.pack(fill="x", pady=(10, 0))
        details_frame.pack_propagate(False)
        remember(
            ctk.CTkLabel(
                details_frame,
                text="Selected archive evidence",
                text_color=palette["text"],
                font=ctk.CTkFont(family="Open Sans", size=12, weight="bold"),
            ),
            "text",
        ).pack(anchor="w", padx=12, pady=(8, 2))
        self.details = tk.Text(
            details_frame,
            height=2,
            wrap="word",
            state="disabled",
            relief="flat",
            borderwidth=0,
            font=("Open Sans", 9),
        )
        self.details.pack(fill="both", expand=True, padx=12)
        detail_buttons = ctk.CTkFrame(details_frame, fg_color="transparent")
        detail_buttons.pack(fill="x", padx=12, pady=(6, 9))
        for column in range(5):
            detail_buttons.grid_columnconfigure(column, weight=1)
        self.workshop_button = advanced_button(
            detail_buttons,
            "Open Workshop",
            self.open_workshop,
            role="primary_button",
            state="disabled",
        )
        self.folder_button = advanced_button(
            detail_buttons,
            "Show archive",
            self.show_archive,
            state="disabled",
        )
        self.record_button = advanced_button(
            detail_buttons,
            "Record baseline",
            self.record_selected_baseline,
            state="disabled",
        )
        self.update_button = advanced_button(
            detail_buttons,
            "Apply update file…",
            self.apply_selected_update,
            role="primary_button",
            state="disabled",
        )
        self.undo_button = advanced_button(
            detail_buttons,
            "Undo last update",
            self.undo_selected_update,
            state="disabled",
        )
        for column, button in enumerate(
            (
                self.workshop_button,
                self.folder_button,
                self.record_button,
                self.update_button,
                self.undo_button,
            )
        ):
            button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 3, 0))

        remember(
            ctk.CTkLabel(
                body,
                text=f"Version {APP_VERSION}  •  Baseline-aware scanning, verified backups, and recoverable updates",
                text_color=palette["muted"],
                font=ctk.CTkFont(family="Open Sans", size=9),
                anchor="w",
            ),
            "muted",
        ).pack(fill="x", pady=(7, 0))
        self._apply_theme(self.settings["theme"])

    def _apply_advanced_theme(self, dark: bool) -> None:
        if not hasattr(self, "_advanced_role_widgets"):
            return
        palette = self._theme_palette
        self.advanced_window.configure(fg_color=palette["background"])
        for role, widgets in self._advanced_role_widgets.items():
            for widget in widgets:
                if role == "shell":
                    widget.configure(fg_color=palette["panel"], border_color=palette["border"])
                elif role == "header":
                    widget.configure(fg_color=palette["header"], border_color=palette["bronze"])
                elif role == "panel":
                    widget.configure(fg_color=palette["field"], border_color=palette["border"])
                elif role == "card":
                    widget.configure(fg_color=palette["panel"], border_color=palette["border"])
                elif role == "text":
                    widget.configure(text_color=palette["text"])
                elif role == "muted":
                    widget.configure(text_color=palette["muted"])
                elif role == "bronze_text":
                    widget.configure(text_color=palette["bronze"])
                elif role == "button":
                    widget.configure(
                        fg_color=palette["field"],
                        hover_color=palette["hover"],
                        border_color=palette["border"],
                        text_color=palette["text"],
                    )
                elif role == "primary_button":
                    widget.configure(
                        fg_color=palette["primary"],
                        hover_color=palette["primary_hover"],
                        border_color=palette["bronze"],
                        text_color="#F1E8D3",
                    )
                elif role == "quiet_button":
                    widget.configure(
                        fg_color="transparent",
                        hover_color=palette["hover"],
                        text_color=palette["muted"],
                    )
                elif role == "entry":
                    widget.configure(
                        fg_color=palette["field"],
                        border_color=palette["border"],
                        text_color=palette["text"],
                        placeholder_text_color=palette["muted"],
                    )
                elif role == "option":
                    widget.configure(
                        fg_color=palette["field"],
                        button_color=palette["border"],
                        button_hover_color=palette["bronze"],
                        text_color=palette["text"],
                        dropdown_fg_color=palette["field"],
                        dropdown_text_color=palette["text"],
                        dropdown_hover_color=palette["hover"],
                    )
                elif role == "progress":
                    widget.configure(
                        fg_color=palette["border"],
                        progress_color=palette["bronze"],
                    )
                elif role == "scrollbar":
                    widget.configure(
                        fg_color=palette["panel"],
                        button_color="#514A40" if dark else "#9D8D76",
                        button_hover_color=palette["bronze"],
                    )
        self.advanced_header_pattern_photo = self._header_pattern(palette["header"], dark)
        self.advanced_header_pattern_label.configure(image=self.advanced_header_pattern_photo)
        self.advanced_background_menu.configure(
            background=palette["field"],
            foreground=palette["text"],
            activebackground=palette["hover"],
            activeforeground=palette["text"],
            borderwidth=1,
        )
        self._apply_windows_frame_theme(dark, self.advanced_window)

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
        self.theme_button.configure(text="Theme")
        if hasattr(self, "advanced_theme_button"):
            self.advanced_theme_button.configure(
                text="Light theme" if theme == "dark" else "Dark theme"
            )
        try:
            save_settings(self._current_settings())
        except OSError:
            pass

    def _background_button_text(self) -> str:
        return {
            "ck3": "Background: CK3 artwork",
            "custom": "Background: Custom",
            "none": "Background: Off",
        }.get(self.settings.get("background_mode", "ck3"), "Background: CK3 artwork")

    def _choose_custom_background(self) -> None:
        current = self.settings.get("custom_background", "").strip()
        chosen = filedialog.askopenfilename(
            title="Choose background artwork",
            initialdir=str(Path(current).parent) if current and Path(current).parent.is_dir() else None,
            filetypes=(
                ("Supported images", "*.bmp *.dds *.jpeg *.jpg *.png *.webp"),
                ("All files", "*.*"),
            ),
        )
        if not chosen:
            return
        self.settings["custom_background"] = chosen
        self._set_background_mode("custom")

    def _set_background_mode(self, mode: str) -> None:
        self.settings["background_mode"] = mode
        self._refresh_background_art()
        try:
            save_settings(self._current_settings())
        except OSError:
            pass

    def _refresh_background_art(self) -> None:
        mode = self.settings.get("background_mode", "ck3")
        custom = self.settings.get("custom_background", "").strip()
        game_root = self.installation.game_root if self.installation else None
        self._background_source = choose_background_art(
            mode,
            game_root,
            Path(custom) if custom else None,
        )
        if hasattr(self, "background_button"):
            self.background_button.configure(text=self._background_button_text())
        if hasattr(self, "advanced_background_button"):
            self.advanced_background_button.configure(text=self._background_button_text())
        self._schedule_background_render()

    def _schedule_background_render(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        if not hasattr(self, "outer"):
            return
        if self._background_render_job:
            self.after_cancel(self._background_render_job)
        self._background_render_job = self.after(120, self._render_background)

    def _render_background(self) -> None:
        self._background_render_job = None
        width = self.outer.winfo_width()
        height = self.outer.winfo_height()
        if width < 2 or height < 2:
            return
        dark = self.settings.get("theme", "dark") == "dark"
        solid = "#0F1214" if dark else "#E7E1D3"
        mode = self.settings.get("background_mode", "ck3")
        if mode == "none":
            self._background_photo = None
            self._background_rendered_image = None
            self.background_label.configure(image="", background=solid)
            self._update_translucent_surfaces(None, dark)
            return
        try:
            if self._background_source:
                image = render_background_art(
                    self._background_source,
                    (width, height),
                    dark=dark,
                )
            else:
                image = render_fallback_texture((width, height), dark=dark)
        except (OSError, ValueError):
            image = render_fallback_texture((width, height), dark=dark)
        self._background_rendered_image = image
        self._background_photo = ImageTk.PhotoImage(image, master=self)
        self.background_label.configure(image=self._background_photo, background=solid)
        self._update_translucent_surfaces(image, dark)

    def _update_translucent_surfaces(
        self,
        background: Image.Image | None,
        dark: bool,
    ) -> None:
        if not hasattr(self, "main_shell_background_label"):
            return
        panel = "#151719" if dark else "#F4EFE4"
        self._main_canvas_art_photo = None
        self._main_canvas_art_cache_key = None
        if background is None:
            self._shell_background_photo = None
            self.main_shell_background_label.configure(image="", background=panel)
        else:
            x = max(0, self.main_shell.winfo_x())
            y = max(0, self.main_shell.winfo_y())
            width = max(1, self.main_shell.winfo_width())
            height = max(1, self.main_shell.winfo_height())
            shell_art = background.crop((x, y, x + width, y + height))
            if shell_art.size != (width, height):
                shell_art = ImageOps.fit(
                    shell_art,
                    (width, height),
                    method=Image.Resampling.LANCZOS,
                )
            overlay = Image.new("RGB", shell_art.size, panel)
            shell_art = Image.blend(shell_art, overlay, 0.88 if dark else 0.91)
            self._shell_background_photo = ImageTk.PhotoImage(shell_art, master=self)
            self.main_shell_background_label.configure(
                image=self._shell_background_photo,
                background=panel,
            )
        if hasattr(self, "main_rows_canvas"):
            self._schedule_main_canvas_draw()

    def _main_canvas_background(self, width: int, height: int) -> ImageTk.PhotoImage | None:
        background = self._background_rendered_image
        if background is None or width < 2 or height < 2:
            return None
        x = max(0, self.main_rows_canvas.winfo_rootx() - self.outer.winfo_rootx())
        y = max(0, self.main_rows_canvas.winfo_rooty() - self.outer.winfo_rooty())
        dark = self.settings.get("theme", "dark") == "dark"
        cache_key = (id(background), x, y, width, height, dark)
        if cache_key == self._main_canvas_art_cache_key:
            return self._main_canvas_art_photo
        art = background.crop((x, y, x + width, y + height))
        if art.size != (width, height):
            art = ImageOps.fit(art, (width, height), method=Image.Resampling.LANCZOS)
        panel = "#151719" if dark else "#F4EFE4"
        overlay = Image.new("RGB", art.size, panel)
        art = Image.blend(art, overlay, 0.82 if dark else 0.90)
        self._main_canvas_art_photo = ImageTk.PhotoImage(art, master=self)
        self._main_canvas_art_cache_key = cache_key
        return self._main_canvas_art_photo

    def _current_settings(self) -> dict[str, str]:
        return {
            "archive_directory": self.archive_var.get().strip(),
            "mod_directory": self.mod_var.get().strip(),
            "game_root": str(self.installation.game_root) if self.installation else "",
            "theme": self.settings.get("theme", "dark"),
            "background_mode": self.settings.get("background_mode", "ck3"),
            "custom_background": self.settings.get("custom_background", ""),
        }

    def _refresh_game_detection(self, show_error: bool = False) -> CK3Installation | None:
        previous_root = self.installation.game_root if self.installation else None
        hints = [self.installation.game_root] if self.installation else []
        self.installation = detect_ck3_installation(hints)
        if self.installation:
            self.version_var.set(self.installation.version)
            self.version_source_var.set(str(self.installation.game_root))
            self.settings["game_root"] = str(self.installation.game_root)
            if (
                hasattr(self, "outer")
                and self.settings.get("background_mode") == "ck3"
                and (previous_root != self.installation.game_root or self._background_source is None)
            ):
                self._refresh_background_art()
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
        ctk.set_appearance_mode("Dark" if dark else "Light")
        background = "#0F1214" if dark else "#E7E1D3"
        panel = "#151719" if dark else "#F4EFE4"
        alternate = "#17191B" if dark else "#EDE6D8"
        field = "#1A1A21" if dark else "#FBF8F0"
        foreground = "#DED6BF" if dark else "#2D2923"
        muted = "#9C998F" if dark else "#6A6359"
        border = "#3B3833" if dark else "#9D8D76"
        bronze = "#AD8F69" if dark else "#725237"
        selected = "#3A2D22" if dark else "#D8C7AE"
        selected_foreground = "#F1E8D3" if dark else "#2D2923"
        header = "#211416" if dark else "#D8C7AE"
        primary = "#725237" if dark else "#725237"
        primary_active = "#866040" if dark else "#866040"
        attention = "#D1C47F" if dark else "#7A651F"
        success = "#669C4D" if dark else "#446C35"
        error = "#CC4D4D" if dark else "#9D3030"
        hover = "#1D2022" if dark else "#E5DDCF"
        self._theme_palette = {
            "background": background,
            "panel": panel,
            "alternate": alternate,
            "field": field,
            "text": foreground,
            "muted": muted,
            "border": border,
            "bronze": bronze,
            "success": success,
            "attention": attention,
            "error": error,
            "hover": hover,
            "primary": primary,
            "primary_hover": primary_active,
            "header": header,
        }
        self.configure(background=background)
        if hasattr(self, "outer"):
            self.outer.configure(background=background)
        self.style.configure("TFrame", background=background)
        self.style.configure("TLabel", background=background, foreground=foreground)
        self.style.configure("Secondary.TLabel", background=background, foreground=muted)
        self.style.configure("Warning.TLabel", background=background, foreground=attention)
        self.style.configure("TLabelframe", background=background, bordercolor=border)
        self.style.configure("TLabelframe.Label", background=background, foreground=foreground)
        self.style.configure("TEntry", fieldbackground=field, foreground=foreground, bordercolor=border)
        self.style.configure("TCombobox", fieldbackground=field, foreground=foreground, bordercolor=border)
        self.style.map("TCombobox", fieldbackground=[("readonly", field)], foreground=[("readonly", foreground)])
        self.style.configure("TButton", background=panel, foreground=foreground, bordercolor=border)
        self.style.configure("Secondary.TButton", background=panel, foreground=foreground, bordercolor=border)
        self.style.configure(
            "Secondary.TMenubutton",
            background=panel,
            foreground=foreground,
            bordercolor=border,
        )
        self.style.configure(
            "Header.TFrame",
            background=header,
            bordercolor=bronze,
            relief="solid",
        )
        self.style.configure("Header.TLabel", background=header, foreground=foreground)
        self.style.configure("Title.TLabel", background=header, foreground=foreground)
        self.style.configure("HeaderSecondary.TLabel", background=header, foreground=muted)
        self.style.configure("Panel.TFrame", background=panel)
        self.style.configure("Inset.TFrame", background=field, bordercolor=border, relief="sunken")
        self.style.configure("Inset.TLabel", background=field, foreground=foreground)
        self.style.configure("ListBorder.TFrame", background=bronze)
        self.style.configure("Footer.TFrame", background=panel, bordercolor=border, relief="solid")
        self.style.configure("Footer.TLabel", background=panel, foreground=muted)
        self.style.configure(
            "Outline.TButton",
            background=field,
            foreground=foreground,
            bordercolor="#514A40" if dark else border,
            padding=(10, 6),
        )
        self.style.map(
            "Outline.TButton",
            background=[("active", selected)],
            foreground=[("active", selected_foreground)],
            bordercolor=[("active", bronze)],
        )
        self.style.configure(
            "Quiet.TButton",
            background=header,
            foreground=muted,
            bordercolor=header,
            padding=(10, 6),
        )
        self.style.map("Quiet.TButton", foreground=[("active", foreground)])
        self.style.configure(
            "Quiet.TMenubutton",
            background=header,
            foreground=muted,
            bordercolor=header,
            padding=(10, 6),
        )
        self.style.configure(
            "Primary.TButton",
            background=primary,
            foreground="#F1E8D3",
            bordercolor="#B19068",
            padding=(10, 6),
        )
        self.style.map("Primary.TButton", background=[("active", primary_active)])
        self.style.configure(
            "Attention.TButton",
            background=field,
            foreground=attention,
            bordercolor=attention,
            padding=(10, 6),
        )
        self.style.map("Attention.TButton", background=[("active", selected)])
        self.style.configure(
            "Tab.TButton",
            background=panel,
            foreground=muted,
            bordercolor=panel,
            padding=(14, 8),
        )
        self.style.map("Tab.TButton", foreground=[("active", foreground)])
        self.style.configure(
            "SelectedTab.TButton",
            background=panel,
            foreground=foreground,
            bordercolor=bronze,
            padding=(14, 8),
        )
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
            self.tree.tag_configure("review", foreground=attention)
            self.tree.tag_configure("error", foreground=error)
            self.tree.tag_configure("nonmod", foreground=muted)
        if hasattr(self, "main_shell"):
            self.main_shell.configure(fg_color=panel, border_color=border)
            self.main_shell_background_label.configure(background=panel)
            self.main_header.configure(fg_color=header, border_color=bronze)
            self.header_pattern_photo = self._header_pattern(header, dark)
            self.header_pattern_label.configure(image=self.header_pattern_photo)
            self.main_title_label.configure(text_color=foreground)
            self.main_subtitle_label.configure(text_color=muted)
            header_hover = "#2A2020" if dark else "#CDB99D"
            control_hover = "#25231F" if dark else "#E4D7C5"
            self.advanced_button.configure(
                fg_color=field,
                hover_color=control_hover,
                border_color=border,
                text_color=foreground,
            )
            self.theme_button.configure(
                fg_color="transparent",
                hover_color=header_hover,
                text_color=muted,
            )
            self.main_summary.configure(fg_color=field, border_color=border)
            self.main_status_label.configure(text_color=foreground)
            self.scan_button.configure(
                fg_color=field,
                hover_color=control_hover,
                border_color=border,
                text_color=foreground,
            )
            self.progress.configure(
                fg_color=border,
                progress_color=bronze,
            )
            self.main_tabs.configure(fg_color=panel)
            self.tabs_separator.configure(fg_color=border)
            self.tab_indicator_canvas.configure(background=panel)
            self.tab_indicator_canvas.itemconfigure(self.active_tab_indicator, fill=bronze)
            for button in self.scope_buttons.values():
                button.configure(
                    fg_color="transparent",
                    hover_color=hover,
                )
            self.main_rows_inner.configure(
                fg_color=panel,
                border_color=border,
            )
            self.main_rows_canvas.configure(background=panel)
            self.main_rows_scrollbar.configure(
                fg_color=panel,
                button_color="#514A40" if dark else "#9D8D76",
                button_hover_color=bronze,
            )
            self.main_footer.configure(fg_color=panel, border_color=border)
            self.footer_separator.configure(fg_color=border)
            self.main_footer_label.configure(text_color=muted)
            self.install_zip_button.configure(
                fg_color=field,
                hover_color=control_hover,
                border_color=border,
                text_color=foreground,
            )
            self._reset_main_canvas()
            self._set_main_scope(self.main_scope_var.get())
        elif hasattr(self, "main_rows_inner"):
            self._refresh_main_rows()
        if hasattr(self, "outer"):
            self._schedule_background_render()
        if hasattr(self, "_advanced_role_widgets"):
            self._apply_advanced_theme(dark)
        self._apply_windows_frame_theme(dark)

    def _set_scan_button_state(self, state: str) -> None:
        self.scan_button.configure(state=state)
        if hasattr(self, "advanced_scan_button"):
            self.advanced_scan_button.configure(state=state)

    def _start_activity_progress(self) -> None:
        if not self.progress.winfo_manager():
            self.progress.pack(side="right", padx=(0, 8))
        self.progress.start()
        if hasattr(self, "advanced_progress"):
            self.advanced_progress.start()

    def _stop_activity_progress(self) -> None:
        self.progress.stop()
        self.progress.set(0)
        self.progress.pack_forget()
        if hasattr(self, "advanced_progress"):
            self.advanced_progress.stop()
            self.advanced_progress.set(0)

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

        self._set_scan_button_state("disabled")
        self.export_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self._start_activity_progress()
        self.status_var.set("Scanning your mods…")
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
        self._stop_activity_progress()
        self._set_scan_button_state("normal")
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
        self._stop_activity_progress()
        self._set_scan_button_state("normal")
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
            "Scan complete"
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
        confirmed_updates = sum(
            item.row.baseline_status == "changed_since_recorded_install"
            for item in ck3
            if not self.playset or item.enabled
        )
        update_word = "update" if confirmed_updates == 1 else "updates"
        self.status_var.set(
            f"{confirmed_updates} {update_word} available · Last checked just now"
            if confirmed_updates
            else "No confirmed updates · Last checked just now"
        )
        self._reset_main_canvas()
        self._apply_filter()
        self._refresh_main_rows()

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
        if self._action_result is not None:
            return self._action_result
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
            try:
                subprocess.Popen(_explorer_select_command(path))
            except OSError:
                os.startfile(str(path.parent))
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
        self._stop_activity_progress()
        self._set_scan_button_state("normal")
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
        self._stop_activity_progress()
        self._set_scan_button_state("normal")
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
        self._stop_activity_progress()
        self._set_scan_button_state("normal")
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
        self._stop_activity_progress()
        self._set_scan_button_state("normal")
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
        self._set_scan_button_state("disabled")
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
        self._set_scan_button_state("normal")
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
        if not errors:
            self.after(100, self._start_initial_scan)

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
        self._set_scan_button_state("disabled")
        self.export_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.record_button.configure(state="disabled")
        self.update_button.configure(state="disabled")
        self.undo_button.configure(state="disabled")
        self.snapshot_button.configure(state="disabled")
        self.preflight_button.configure(state="disabled")
        self._start_activity_progress()
        self.status_var.set(status)

    def apply_selected_update(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        item = self._selected_result()
        if not item or not item.row.archive.mod_id or not item.row.workshop_updated:
            return
        archive = item.row.archive
        candidate = str(self._pending_update_candidate) if self._pending_update_candidate else filedialog.askopenfilename(
            title="Choose the downloaded update archive",
            initialdir=str(archive.archive_path.parent),
            filetypes=(("ZIP archives", "*.zip"),),
        )
        if not candidate:
            return
        candidate_path = Path(candidate)
        expected_name = archive.name or item.row.workshop_title or archive.archive_path.name
        problem = validate_update_candidate(
            archive.archive_path,
            candidate_path,
            archive.mod_id,
            expected_name,
        )
        if problem:
            messagebox.showerror(problem.title, problem.message)
            return
        try:
            impact = compare_archives(archive.archive_path, candidate_path)
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
            expected_name,
            archive.archive_path,
            candidate_path,
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
        self._stop_activity_progress()
        self._set_scan_button_state("normal")
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
        self._stop_activity_progress()
        self._set_scan_button_state("normal")
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
        self._stop_activity_progress()
        self._set_scan_button_state("normal")
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
        if self._high_resolution_timer:
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except (AttributeError, OSError):
                pass
            self._high_resolution_timer = False
        self.destroy()


def main() -> None:
    app = CK3ModUpdaterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
