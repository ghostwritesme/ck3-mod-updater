"""Tests for report conversion helpers."""

from pathlib import Path
import unittest

from ck3_mod_updater_app import ResultItem, _application_asset, build_report_rows
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


if __name__ == "__main__":
    unittest.main()
