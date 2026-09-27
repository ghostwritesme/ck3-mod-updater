"""Regression tests for archive impact preview and safety validation."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from update_impact import ImpactError, compare_archives, inspect_archive_structure


class UpdateImpactTests(unittest.TestCase):
    def test_preview_reports_added_removed_changed_and_replace_paths(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "current.zip"
            candidate = root / "candidate.zip"
            with ZipFile(current, "w") as archive:
                archive.writestr("descriptor.mod", 'name="Test"\nreplace_path="old/path"')
                archive.writestr("common/changed.txt", "before")
                archive.writestr("common/removed.txt", "removed")
                archive.writestr("common/same.txt", "same")
            with ZipFile(candidate, "w") as archive:
                archive.writestr("descriptor.mod", 'name="Test"\nreplace_path="new/path"')
                archive.writestr("common/changed.txt", "after")
                archive.writestr("common/added.txt", "added")
                archive.writestr("common/same.txt", "same")

            impact = compare_archives(current, candidate)

            self.assertIn("common/added.txt", impact.added_files)
            self.assertIn("common/removed.txt", impact.removed_files)
            self.assertIn("common/changed.txt", impact.changed_files)
            self.assertEqual(impact.unchanged_count, 1)
            self.assertEqual(impact.added_replace_paths, ("new/path",))
            self.assertEqual(impact.removed_replace_paths, ("old/path",))

    def test_preview_flags_executable_content(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "current.zip"
            candidate = root / "candidate.zip"
            with ZipFile(current, "w") as archive:
                archive.writestr("descriptor.mod", 'name="Test"')
            with ZipFile(candidate, "w") as archive:
                archive.writestr("descriptor.mod", 'name="Test"')
                archive.writestr("tools/install.exe", "payload")

            impact = compare_archives(current, candidate)

            self.assertEqual(impact.suspicious_files, ("tools/install.exe",))

    def test_preview_refuses_parent_directory_paths(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "current.zip"
            candidate = root / "candidate.zip"
            with ZipFile(current, "w") as archive:
                archive.writestr("descriptor.mod", 'name="Test"')
            with ZipFile(candidate, "w") as archive:
                archive.writestr("descriptor.mod", 'name="Test"')
                archive.writestr("../outside.txt", "unsafe")

            with self.assertRaisesRegex(ImpactError, "unsafe path"):
                compare_archives(current, candidate)

    def test_preview_refuses_windows_reserved_and_stream_paths(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            reserved = root / "reserved.zip"
            with ZipFile(reserved, "w") as archive:
                archive.writestr("common/CON.txt", "unsafe")
            with self.assertRaisesRegex(ImpactError, "reserved Windows path"):
                inspect_archive_structure(reserved)

            stream = root / "stream.zip"
            with ZipFile(stream, "w") as archive:
                archive.writestr("common/file.txt:payload", "unsafe")
            with self.assertRaisesRegex(ImpactError, "Windows-unsafe path"):
                inspect_archive_structure(stream)


if __name__ == "__main__":
    unittest.main()
