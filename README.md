# CK3 Mod Updater

CK3 Mod Updater is a Windows desktop application for inspecting local Crusader Kings III mod archives and comparing their metadata with the Steam Workshop.

The current version is read-only. It does not delete, replace, download, or install mods.

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

Exported reports contain archive filenames and scan metadata, not full local filesystem paths.

## Tests

```powershell
python -B -m unittest -v test_mod_scan_core.py test_ck3_game_detection.py test_app_helpers.py
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
- `test_mod_scan_core.py` — scanner regression tests
- `test_ck3_game_detection.py` — installation detection tests
- `test_app_helpers.py` — report formatting tests
- `CK3_Mod_Updater.spec` — PyInstaller configuration

## Disclaimer

This is an independent community project and is not affiliated with or endorsed by Paradox Interactive or Valve.
