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
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

import requests
from ck3_game_detection import CK3Installation, detect_ck3_installation
from mod_scan_core import (
    ArchiveScanRow,
    discover_mod_ids,
    fetch_workshop_batch,
    scan_archives,
)


APP_NAME = "CK3 Mod Updater"
APP_VERSION = "0.2.1"
WORKSHOP_ITEM_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"


def _app_data_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return base / "CK3ModUpdater"


SETTINGS_FILE = _app_data_directory() / "settings.json"


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


@dataclass(frozen=True)
class ResultItem:
    row: ArchiveScanRow
    installed: bool


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
                "archive_timestamp_signal": row.timestamp_signal,
                "duplicate_count": row.duplicate_count,
                "installed": item.installed,
                "detail": row.detail,
            }
        )
    return rows


class CK3ModUpdaterApp(tk.Tk):
    def __init__(self) -> None:
        self.settings = load_settings()
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
        self.worker: threading.Thread | None = None
        self.last_scan_utc: str | None = None
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
        self.summary_vars = {
            "archives": tk.StringVar(value="0"),
            "ck3": tk.StringVar(value="0"),
            "review": tk.StringVar(value="0"),
            "installed": tk.StringVar(value="0"),
        }

        self._build_ui()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_ui_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, font=("Segoe UI", 22, "bold")).pack(side="left")
        ttk.Label(
            header,
            text="Workshop archive checker",
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
                "Workshop dates and descriptor compatibility are evidence, not proof that an "
                "archive is current or that a mod works in-game."
            ),
            style="Warning.TLabel",
        ).pack(fill="x", pady=(6, 14))

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
                "CK3 mods",
                "All files",
                "Possible Workshop changes",
                "Declared for another CK3 version",
                "Installed",
                "Workshop unavailable/errors",
                "Duplicate archives",
                "Non-CK3 files",
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
            "version",
            "compatibility",
            "workshop",
            "signal",
        )
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=19)
        headings = {
            "name": "Mod / archive",
            "id": "Workshop ID",
            "installed": "Installed",
            "version": "Mod version",
            "compatibility": "Descriptor vs CK3",
            "workshop": "Workshop updated",
            "signal": "Date evidence",
        }
        widths = {
            "name": 280,
            "id": 115,
            "installed": 80,
            "version": 100,
            "compatibility": 170,
            "workshop": 165,
            "signal": 210,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
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

        ttk.Label(
            outer,
            text=f"{APP_VERSION}  •  Read-only scanning; deletion and automatic replacement are disabled.",
            style="Secondary.TLabel",
        ).pack(fill="x", pady=(9, 0))

        self._apply_theme(self.settings["theme"])

    def _browse_directory(self, variable: tk.StringVar) -> None:
        initial = variable.get().strip()
        chosen = filedialog.askdirectory(initialdir=initial if Path(initial).is_dir() else None)
        if chosen:
            variable.set(chosen)

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
        self.progress.start(12)
        self.status_var.set("Reading ZIP descriptors and checking Workshop…")
        self.worker = threading.Thread(
            target=self._scan_worker,
            args=(archive_directory, mod_directory, game_version),
            daemon=True,
        )
        self.worker.start()

    def _scan_worker(self, archive_directory: Path, mod_directory: Path, game_version: str) -> None:
        try:
            with requests.Session() as session:
                rows = scan_archives(
                    archive_directory,
                    game_version,
                    lambda ids: fetch_workshop_batch(session, ids),
                )
            installed_ids = set(discover_mod_ids(mod_directory)) if mod_directory.is_dir() else set()
            results = [
                ResultItem(row=row, installed=bool(row.archive.mod_id in installed_ids))
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
        self.progress.stop()
        self.scan_button.configure(state="normal")
        self.status_var.set("Scan failed")
        messagebox.showerror("Scan failed", detail)

    def _scan_finished(self, results: list[ResultItem]) -> None:
        self.results = results
        self.last_scan_utc = datetime.now(timezone.utc).isoformat()
        self.progress.stop()
        self.scan_button.configure(state="normal")
        ck3 = [item for item in results if item.row.archive.status == "ck3_mod"]
        self.export_button.configure(state="normal" if ck3 else "disabled")
        review = [
            item
            for item in ck3
            if item.row.timestamp_signal == "remote_newer_than_archive_timestamp"
            or item.row.compatibility == "declared_incompatible"
            or item.row.workshop_status != "available"
            or item.row.duplicate_count > 1
        ]
        self.summary_vars["archives"].set(str(len(results)))
        self.summary_vars["ck3"].set(str(len(ck3)))
        self.summary_vars["review"].set(str(len(review)))
        self.summary_vars["installed"].set(str(sum(item.installed for item in results)))
        ignored = len(results) - len(ck3)
        self.status_var.set(
            f"Finished: {len(ck3)} CK3 archives, {len(review)} needing review, {ignored} non-mod ZIPs ignored"
        )
        self._apply_filter()

    def _matches_filter(self, item: ResultItem) -> bool:
        row = item.row
        selected = self.filter_var.get()
        if selected == "CK3 mods":
            return row.archive.status == "ck3_mod"
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
        if selected == "Non-CK3 files":
            return row.archive.status != "ck3_mod"
        return True

    def _apply_filter(self) -> None:
        search = self.search_var.get().strip().casefold()
        self.visible_results = []
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
            self.visible_results.append(item)
            display_name = archive.name or row.workshop_title or archive.archive_path.name
            if row.duplicate_count > 1:
                display_name = f"{display_name}  ({row.duplicate_count} copies)"
            tags: tuple[str, ...] = ()
            if archive.status != "ck3_mod":
                tags = ("nonmod",)
            elif row.workshop_status in {"remote_unavailable", "scan_error"}:
                tags = ("error",)
            elif (
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
                    archive.version or "—",
                    _compatibility_label(row.compatibility),
                    _workshop_label(row),
                    _signal_label(row.timestamp_signal),
                ),
                tags=tags,
            )
            self.result_by_item[tree_item] = item
        self._set_details("Select an archive to see exactly what each status means.")
        self.workshop_button.configure(state="disabled")
        self.folder_button.configure(state="disabled")

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
            f"Archive-date signal: {_signal_label(row.timestamp_signal)}",
            f"Installed in selected mod folder: {'Yes' if item.installed else 'No'}",
        ]
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
