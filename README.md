# CK3 Mod Updater

CK3 Mod Updater is a Windows desktop application for inspecting local Crusader Kings III mod archives, comparing their metadata with the Steam Workshop, and safely replacing verified archives.

Scanning is read-only. Archive replacement is an explicit action that requires a user-selected ZIP, creates a verified backup, records a transaction journal, and can be undone.

## Project status

This project is in active development. The repository currently contains source code and development history; no stable executable or official release is available yet.

## Features

- Reads mod descriptors directly from ZIP files without extracting them.
- Separates CK3 archives from unrelated files.
- Retrieves public Workshop metadata without requiring an API key.
- Detects the installed CK3 version automatically from the game's launcher settings.
- Shows descriptor-declared game compatibility separately from Workshop status.
- Identifies duplicate Workshop IDs and matches archives with installed mods.
- Provides search, filters, dark and light themes, Workshop links, and JSON or CSV reports.
- Keeps network failures and unavailable Workshop items distinct from update signals.
- Records exact archive hashes and Workshop timestamps in a local install-baseline ledger.
- Validates staged update archives against the selected Workshop ID before replacement.
- Creates verified backups and uses same-folder atomic replacement.
- Rolls back automatically after a failed update and during recovery from an interrupted update.
- Provides a manual undo action for the latest committed update when the archive has not changed again.

## Updating an archive

1. Scan the archive folder.
2. For an existing archive known to match the current Workshop revision, select it and choose **Record baseline** once.
3. When a later scan shows **Update available**, obtain the updated ZIP through Steam or another legitimate source.
4. Select the archive, choose **Apply update file**, and select the downloaded ZIP.
5. Confirm that the selected ZIP was obtained for the displayed Workshop revision. The application verifies the Workshop ID, stages the ZIP privately, backs up the current archive, replaces it atomically, verifies the result, and only then records the confirmed revision.

The archive descriptor proves the mod identity, not the exact Workshop revision. Revision baselines therefore depend on the user's explicit source confirmation. The public Workshop metadata endpoint does not guarantee a downloadable mod file, and the application does not request or store Steam credentials. File acquisition remains separate from the recoverable replacement workflow.

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

Application settings are stored in `%LOCALAPPDATA%\CK3ModUpdater\settings.json`.

Baselines, transaction journals, staging files, and backups are stored under `%LOCALAPPDATA%\CK3ModUpdater`. These files should not be edited manually.

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
- `install_ledger.py` — persistent, hash-bound Workshop install baselines
- `update_workflow.py` — staging, backups, atomic replacement, rollback, and recovery
- `test_mod_scan_core.py` — scanner regression tests
- `test_ck3_game_detection.py` — installation detection tests
- `test_app_helpers.py` — report formatting tests
- `test_update_workflow.py` — updater transaction and failure-recovery tests
- `CK3_Mod_Updater.spec` — PyInstaller configuration

## Disclaimer

This is an independent community project and is not affiliated with or endorsed by Paradox Interactive or Valve.
