# CK3 Mod Updater

CK3 Mod Updater is a Windows desktop application for inspecting local Crusader Kings III mod archives, comparing their metadata with the Steam Workshop, and safely updating verified archives and enabled manual installations.

Scanning is read-only. An update is an explicit action that requires a user-selected ZIP, creates verified backups, records transaction journals, and can be undone. When the mod is enabled in the detected playset, its extracted installed folder and descriptor are updated together with the archive library.

## Project status

This project is in active development. The repository currently contains source code and development history; no stable executable or official release is available yet.

## Features

- Opens into a focused updater screen with automatic scanning, Enabled/Installed/Downloaded/All views, plain-language statuses, and one consistent action per mod.
- Keeps paths, evidence, reports, snapshots, preflight checks, batch tools, and recovery controls in a separate Advanced window.
- Reads mod descriptors directly from ZIP files without extracting them.
- Separates CK3 archives from unrelated files.
- Retrieves public Workshop metadata without requiring an API key.
- Detects the installed CK3 version automatically from the game's launcher settings.
- Shows descriptor-declared game compatibility separately from Workshop status.
- Identifies duplicate Workshop IDs and matches archives with installed mods.
- Provides search, filters, dark and light themes, Workshop links, and JSON or CSV reports.
- Can display a muted loading-screen background directly from the user's local CK3 installation, use a custom local image, or disable artwork entirely.
- Keeps network failures and unavailable Workshop items distinct from update signals.
- Records exact archive hashes and Workshop timestamps in a local install-baseline ledger.
- Validates staged update archives against the selected Workshop ID before replacement.
- Creates verified backups and uses same-folder atomic replacement.
- Rolls back automatically after a failed update and during recovery from an interrupted update.
- Provides a manual undo action for the latest committed update when the archive has not changed again.
- Reads the active Irony-applied or Paradox launcher mod set and load order without editing it.
- Compares the latest CK3 save's game version and mod set with the active playset.
- Preserves complete, hash-verified setup snapshots containing active installed mods, matching source archives, configuration, and an optional save.
- Tracks changes between scans for a return-from-hiatus summary.
- Sorts every results column and filters directly to the enabled playset.
- Shows added, removed, and changed files before an archive update.
- Rejects unsafe paths, symbolic links, encrypted entries, and duplicate archive paths.
- Plans explicitly selected batch updates, snapshots the active setup, prepares every candidate first, and rolls back earlier replacements if the batch fails.
- Safely extracts enabled mods into same-volume staging, normalizes wrapped archive layouts, and atomically swaps their installed folders and descriptors.
- Coordinates archive-library and installed-folder transactions across drives, rolling both sides back after failure or interrupted work.
- Blocks installed-file changes while CK3, Irony Mod Manager, or the Paradox launcher is running.
- Restores verified setup snapshots without editing launcher or Irony databases, and restores saved games under new filenames.
- Compares snapshot playset configuration with the current setup and reports missing, extra, or reordered mods for manual reconciliation.
- Keeps update backups, restore backups, and setup snapshots until the user removes them manually.
- Runs conservative structural and save/playset preflight checks without claiming that mods work in-game.

## Updating an archive

1. Scan the archive folder.
2. For an existing archive known to match the current Workshop revision, select it and choose **Record baseline** once.
3. When a later scan shows **Update available**, obtain the updated ZIP through Steam or another legitimate source.
4. Close CK3, Irony Mod Manager, and the Paradox launcher if the mod is enabled.
5. Select the archive, choose **Apply update file**, and select the downloaded ZIP.
6. Confirm that the selected ZIP was obtained for the displayed Workshop revision. The application verifies the Workshop ID, stages the ZIP privately, backs up the current archive, and replaces it atomically. For an enabled mod, it also stages, backs up, and replaces the installed folder and descriptor. The coordinated operation is verified before the confirmed revision is retained.

The archive descriptor proves the mod identity, not the exact Workshop revision. Revision baselines therefore depend on the user's explicit source confirmation. The public Workshop metadata endpoint does not guarantee a downloadable mod file, and the application does not request or store Steam credentials. File acquisition remains separate from the recoverable replacement workflow.

For a batch, choose **Batch update files**, select only the archives you intend to install, review the matched items and file-impact counts, then confirm the complete plan. A candidate mismatch blocks the whole batch instead of silently applying only part of it. Enabled installations are included automatically; inactive library entries remain archive-only updates.

## Setup preservation and preflight

When an active playset is detected, **Preserve setup** creates a complete local snapshot under `%LOCALAPPDATA%\CK3ModUpdater\snapshots`. The snapshot includes hash-verified copies of active installed mod content, matching source archives, launcher-compatible JSON state, and optionally the latest save. Snapshots are never deleted automatically.

**Manage snapshots** verifies saved hashes and can restore a selected setup. Before restoration, the current active setup is preserved as a new safety snapshot. Restoration can replace matching archive ZIPs, installed mod folders, and `.mod` descriptors; an included save is copied under a new filename. Existing targets receive recoverable backups.

Launcher and Irony configuration files are compared but not restored automatically. After a restore, the application reports whether the current playset has missing, extra, or reordered mods so the user can reconcile it in Irony or the launcher. The snapshot manager also exposes an **Undo last restore** action when the restored files have not changed afterward.

Preflight checks archive structure, enabled archive availability, duplicates, descriptor declarations, and whether the latest save's CK3 version and mod set differ from the active setup. Passing preflight means that no checked structural blocker was found; it is not proof that the mods function correctly in-game.

## Design boundaries

The application does not include a home-grown conflict solver, scrape Workshop pages, store Steam credentials, blindly update every archive, or automatically edit Irony or launcher databases. It does not treat descriptor compatibility as proof that a mod works. Backups and snapshots have no automatic expiration and remain visible in their storage folders until the user removes them manually.

## Accuracy

A file timestamp does not identify the exact Workshop revision stored inside an archive. The application therefore labels timestamp comparisons as review signals, not confirmed update verdicts.

Descriptor compatibility is also a declaration made by the mod author. It does not guarantee that every feature works with a particular game version.

## Running from source

Python 3.10 or newer is required.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python ck3_mod_updater_app.py
```

On Windows, double-click `CK3 Mod Updater.pyw` to launch the source build without a console window.

Application settings are stored in `%LOCALAPPDATA%\CK3ModUpdater\settings.json`.

CK3 background artwork is read from the detected game and DLC folders at runtime. The project does not include, copy, or redistribute those images. If local artwork is unavailable, the application uses its built-in neutral texture instead.

Baselines, scan history, transaction journals, archive backups, and setup snapshots are stored under `%LOCALAPPDATA%\CK3ModUpdater`. Same-volume installed-mod and restore backups are kept in private `.ck3-mod-updater` folders under the relevant CK3 data or archive-library root. These files should not be edited manually.

Exported reports contain archive filenames and scan metadata, not full local filesystem paths.

## Tests

```powershell
python -B -m unittest discover -v
```

## Building the Windows executable

```powershell
python -m pip install -r requirements-dev.txt
python -B -m PyInstaller --noconfirm CK3_Mod_Updater.spec
```

The resulting executable is written to `dist\CK3_Mod_Updater.exe`. Published binaries should be distributed through the repository's Releases page rather than committed to source control.

## Project structure

- `ck3_mod_updater_app.py` — desktop interface
- `mod_scan_core.py` — archive inspection and Workshop comparison
- `ck3_game_detection.py` — automatic CK3 installation and version detection
- `background_art.py` — local loading-screen discovery and non-destructive background rendering
- `install_ledger.py` — persistent, hash-bound Workshop install baselines
- `update_workflow.py` — staging, backups, atomic replacement, rollback, and recovery
- `batch_update.py` — explicit multi-archive update planning and batch rollback
- `installed_deployment.py` — safe extraction and recoverable installed-folder replacement
- `coordinated_update.py` — cross-drive coordination for library and installed updates
- `playset_state.py` — read-only active launcher and Irony-applied mod state
- `save_inspection.py` — lightweight CK3 save metadata inspection
- `setup_snapshots.py` — complete hash-verified setup preservation
- `snapshot_restore.py` — non-database snapshot planning, restoration, undo, and recovery
- `return_history.py` — comparisons between the current and previous scan
- `update_impact.py` — safe archive structure checks and file-impact previews
- `preflight.py` — conservative active-setup and save consistency checks
- `test_mod_scan_core.py` — scanner regression tests
- `test_ck3_game_detection.py` — installation detection tests
- `test_background_art.py` — local artwork discovery and rendering tests
- `test_app_helpers.py` — report formatting tests
- `test_update_workflow.py` — updater transaction and failure-recovery tests
- `test_batch_update.py` — batch planning and rollback tests
- `test_installed_deployment.py` — installed-folder extraction, replacement, and recovery tests
- `test_coordinated_update.py` — combined archive/installation rollback tests
- `test_playset_and_save.py` — active playset and save-awareness tests
- `test_setup_snapshots.py` — setup snapshot and verification tests
- `test_snapshot_restore.py` — verified snapshot restore and undo tests
- `test_update_impact.py` — impact preview and unsafe archive tests
- `test_preflight.py` — post-update preflight tests
- `test_return_history.py` — return-from-hiatus comparison tests
- `CK3_Mod_Updater.spec` — PyInstaller configuration

## Disclaimer

This is an independent community project and is not affiliated with or endorsed by Paradox Interactive or Valve.
