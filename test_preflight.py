"""Regression tests for conservative post-update preflight checks."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from mod_scan_core import ArchiveRecord, ArchiveScanRow
from playset_state import PlaysetMod, PlaysetState
from preflight import run_preflight
from save_inspection import SaveMetadata


def create_archive(path: Path, mod_id: str) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "descriptor.mod",
            f'name="Test"\nremote_file_id="{mod_id}"\nsupported_version="1.18.*"',
        )


class PreflightTests(unittest.TestCase):
    def test_matching_setup_has_no_structural_blockers(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            archive = root / "123.zip"
            create_archive(archive, "123")
            row = ArchiveScanRow(
                ArchiveRecord(archive, "ck3_mod", mod_id="123", name="Test"),
                "available",
                compatibility="declared_compatible",
            )
            installed = root / "mod" / "123"
            installed.mkdir(parents=True)
            (installed / "descriptor.mod").write_text('name="Test"', encoding="utf-8")
            descriptor = root / "mod" / "123.mod"
            descriptor.write_text('name="Test"\npath="mod/123"', encoding="utf-8")
            playset = PlaysetState(
                root,
                "Test",
                (PlaysetMod("entry", "mod/123.mod", "Test", "123", installed, descriptor, 1),),
                (),
            )
            save = SaveMetadata(root / "save.ck3", "1.18.0", None, None, None, (), ("123",), ())

            report = run_preflight([row], playset, save, "1.18.0")

            self.assertTrue(report.structurally_ready)
            self.assertEqual(report.issues, ())

    def test_missing_archive_and_save_change_are_reported_without_working_claim(self) -> None:
        root = Path("data")
        playset = PlaysetState(
            root,
            "Test",
            (PlaysetMod("entry", "mod/123.mod", "Test", "123", None, None, 1),),
            (),
        )
        save = SaveMetadata(root / "save.ck3", "1.17.0", None, None, None, (), ("999",), ())

        report = run_preflight([], playset, save, "1.18.0")

        self.assertFalse(report.structurally_ready)
        self.assertEqual(report.errors[0].code, "missing_archive")
        self.assertEqual({issue.code for issue in report.warnings}, {
            "save_game_version_changed",
            "save_playset_changed",
        })


if __name__ == "__main__":
    unittest.main()
