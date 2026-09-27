"""Tests for report conversion helpers."""

from pathlib import Path
import unittest

from ck3_mod_updater_app import (
    ResultItem,
    _application_asset,
    build_report_rows,
    sort_result_items,
)
from mod_scan_core import ArchiveRecord, ArchiveScanRow


class ReportHelperTests(unittest.TestCase):
    def test_application_asset_resolves_from_source_directory(self) -> None:
        self.assertEqual(_application_asset("icon.ico"), Path(__file__).parent / "icon.ico")

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


if __name__ == "__main__":
    unittest.main()
