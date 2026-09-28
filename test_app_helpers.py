"""Tests for report conversion helpers."""

from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from ck3_mod_updater_app import (
    CK3ModUpdaterApp,
    ResultItem,
    _application_asset,
    _explorer_select_command,
    build_report_rows,
    sort_result_items,
    validate_update_candidate,
)
from mod_scan_core import ArchiveRecord, ArchiveScanRow


class ReportHelperTests(unittest.TestCase):
    @staticmethod
    def _create_archive(path: Path, mod_id: str, value: str) -> None:
        with ZipFile(path, "w") as archive:
            archive.writestr(
                "descriptor.mod",
                f'name="Mod {mod_id}"\nremote_file_id="{mod_id}"\nversion="{value}"',
            )
            archive.writestr("common/value.txt", value)

    def test_application_asset_resolves_from_source_directory(self) -> None:
        self.assertEqual(_application_asset("icon.ico"), Path(__file__).parent / "icon.ico")

    def test_explorer_command_quotes_archive_paths_with_spaces(self) -> None:
        archive = Path(r"H:\Games\CK3 Mods\123_Test.zip")

        command = _explorer_select_command(archive)

        self.assertEqual(command, f'explorer.exe /select,"{archive.resolve()}"')

    def test_update_candidate_errors_distinguish_common_selection_mistakes(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "123_current.zip"
            identical_copy = root / "copied.zip"
            different_mod = root / "456_other.zip"
            self._create_archive(current, "123", "old")
            shutil.copy2(current, identical_copy)
            self._create_archive(different_mod, "456", "other")

            same_path = validate_update_candidate(current, current, "123", "Mod 123")
            identical = validate_update_candidate(current, identical_copy, "123", "Mod 123")
            wrong_mod = validate_update_candidate(current, different_mod, "123", "Mod 123")

            self.assertEqual(same_path.title, "Current archive selected")
            self.assertEqual(identical.title, "Identical archive selected")
            self.assertEqual(wrong_mod.title, "Different mod selected")
            self.assertIn("Workshop ID 456", wrong_mod.message)

    def test_report_contains_only_ck3_mod_rows(self) -> None:
        ck3 = ArchiveScanRow(
            archive=ArchiveRecord(
                Path("123_Mod.zip"),
                "ck3_mod",
                mod_id="123",
                name="Test Mod",
            ),
            workshop_status="available",
        )
        unrelated = ArchiveScanRow(
            archive=ArchiveRecord(Path("wallpaper.zip"), "wallpaper_engine"),
            workshop_status="not_applicable",
        )

        rows = build_report_rows(
            [ResultItem(ck3, installed=True), ResultItem(unrelated, installed=False)]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workshop_id"], "123")
        self.assertEqual(rows[0]["archive_file"], "123_Mod.zip")
        self.assertNotIn("archive", rows[0])
        self.assertTrue(rows[0]["installed"])

    def test_column_sorting_uses_numeric_ids_and_keeps_missing_values_last(self) -> None:
        items = [
            ResultItem(
                ArchiveScanRow(
                    ArchiveRecord(Path("missing.zip"), "ck3_mod", name="Missing"),
                    "remote_unavailable",
                ),
                installed=False,
            ),
            ResultItem(
                ArchiveScanRow(
                    ArchiveRecord(Path("10.zip"), "ck3_mod", mod_id="10", name="Ten"),
                    "available",
                    workshop_updated=100,
                ),
                installed=True,
            ),
            ResultItem(
                ArchiveScanRow(
                    ArchiveRecord(Path("2.zip"), "ck3_mod", mod_id="2", name="Two"),
                    "available",
                    workshop_updated=200,
                ),
                installed=False,
            ),
        ]

        ascending = sort_result_items(items, "id", descending=False)
        descending = sort_result_items(items, "id", descending=True)

        self.assertEqual([item.row.archive.mod_id for item in ascending], ["2", "10", None])
        self.assertEqual([item.row.archive.mod_id for item in descending], ["10", "2", None])

    def test_version_and_workshop_columns_sort_naturally(self) -> None:
        items = [
            ResultItem(
                ArchiveScanRow(
                    ArchiveRecord(Path("new.zip"), "ck3_mod", version="1.10"),
                    "available",
                    workshop_updated=300,
                ),
                installed=False,
            ),
            ResultItem(
                ArchiveScanRow(
                    ArchiveRecord(Path("old.zip"), "ck3_mod", version="1.9"),
                    "available",
                    workshop_updated=100,
                ),
                installed=False,
            ),
        ]

        by_version = sort_result_items(items, "version", descending=False)
        by_workshop = sort_result_items(items, "workshop", descending=True)

        self.assertEqual([item.row.archive.version for item in by_version], ["1.9", "1.10"])
        self.assertEqual([item.row.workshop_updated for item in by_workshop], [300, 100])

    def test_simple_rows_use_truthful_status_and_action_pairs(self) -> None:
        archive = ArchiveRecord(
            Path("123_Mod.zip"),
            "ck3_mod",
            mod_id="123",
            name="Test Mod",
        )
        current = ResultItem(
            ArchiveScanRow(
                archive,
                "available",
                workshop_updated=100,
                baseline_status="no_change_since_recorded_install",
            ),
            installed=True,
        )
        update = ResultItem(
            ArchiveScanRow(
                archive,
                "available",
                workshop_updated=200,
                baseline_status="changed_since_recorded_install",
            ),
            installed=True,
        )
        unknown = ResultItem(
            ArchiveScanRow(
                archive,
                "available",
                workshop_updated=200,
                baseline_status="unknown_local_version",
            ),
            installed=True,
        )

        self.assertEqual(
            CK3ModUpdaterApp._main_status(current),
            ("✓", "Up to date", "Open source", "source"),
        )
        self.assertEqual(
            CK3ModUpdaterApp._main_status(update),
            ("↑", "Update available", "Get update", "source"),
        )
        self.assertEqual(
            CK3ModUpdaterApp._main_status(unknown),
            ("!", "Version not recorded", "Record installed version", "record"),
        )


if __name__ == "__main__":
    unittest.main()
