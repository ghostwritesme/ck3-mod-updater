"""Regression checks for scanner evidence and failure handling."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from mod_scan_core import (
    archive_timestamp_signal,
    classify_mod,
    declared_compatibility,
    discover_mod_ids,
    duplicate_archive_ids,
    fetch_workshop_batch,
    inspect_archive_directory,
    inspect_mod_archive,
    scan_archives,
    scan_mods,
)


def workshop_item(mod_id: str, updated: int = 200) -> dict:
    return {"publishedfileid": mod_id, "result": 1, "title": f"Mod {mod_id}", "time_updated": updated}


class FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"response": {"publishedfiledetails": [workshop_item("123")]}}


class FakeSession:
    def __init__(self) -> None:
        self.data = None

    def post(self, _url: str, *, data: dict, timeout: int) -> FakeResponse:
        self.data = data
        assert timeout == 20
        return FakeResponse()


class FakeEntry:
    def __init__(self, name: str, directory: bool = False) -> None:
        self.name = name
        self._directory = directory

    def is_dir(self) -> bool:
        return self._directory

    def is_file(self) -> bool:
        return not self._directory

    @property
    def suffix(self) -> str:
        return "." + self.name.rsplit(".", 1)[1] if "." in self.name else ""

    @property
    def stem(self) -> str:
        return self.name.rsplit(".", 1)[0]


class FakeRoot:
    def __init__(self, entries: list[FakeEntry] | None = None) -> None:
        self.entries = entries

    def is_dir(self) -> bool:
        return self.entries is not None

    def iterdir(self) -> list[FakeEntry]:
        return self.entries or []


class ScannerTests(unittest.TestCase):
    def test_discovery_accepts_only_exact_numeric_mod_names(self) -> None:
        root = FakeRoot([
            FakeEntry("123", directory=True),
            FakeEntry("234.mod"),
            FakeEntry("345.backup"),
            FakeEntry("456.txt"),
        ])
        self.assertEqual(discover_mod_ids(root), ["123", "234"])

    def test_missing_directory_is_an_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            discover_mod_ids(FakeRoot())

    def test_unknown_install_never_claims_current_or_outdated(self) -> None:
        row = classify_mod("123", workshop_item("123"))
        self.assertEqual(row.status, "unknown_local_version")

    def test_known_install_baseline_can_detect_a_change(self) -> None:
        self.assertEqual(classify_mod("123", workshop_item("123", 201), 200).status,
                         "changed_since_recorded_install")
        self.assertEqual(classify_mod("123", workshop_item("123", 200), 200).status,
                         "no_change_since_recorded_install")

    def test_unavailable_and_missing_timestamps_do_not_become_updates(self) -> None:
        self.assertEqual(classify_mod("123", {"publishedfileid": "123", "result": 9}).status,
                         "remote_unavailable")
        self.assertEqual(classify_mod("123", {"publishedfileid": "123", "result": 1}).status,
                         "scan_error")

    def test_failed_batch_does_not_fabricate_an_update(self) -> None:
        def failed_fetch(_ids: list[str]) -> list[dict]:
            raise ValueError("bad response")

        rows = scan_mods(FakeRoot([FakeEntry("123", directory=True)]), {}, failed_fetch)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "scan_error")
        self.assertIsNone(rows[0].workshop_updated)

    def test_workshop_request_omits_api_key(self) -> None:
        session = FakeSession()
        self.assertEqual(fetch_workshop_batch(session, ["123"]), [workshop_item("123")])
        self.assertEqual(session.data, {"itemcount": 1, "publishedfileids[0]": "123"})

    def test_archive_inspection_reads_descriptor_without_extracting(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            archive_path = Path(directory, "123_Test.zip")
            with ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "123/descriptor.mod",
                    'name="Test Mod"\nversion="2.0"\n'
                    'supported_version="1.18.*"\nremote_file_id="123"\n',
                )
                archive.writestr("123/common/test.txt", "test")
            record = inspect_mod_archive(archive_path)
            self.assertEqual(record.status, "ck3_mod")
            self.assertEqual(record.mod_id, "123")
            self.assertEqual(record.name, "Test Mod")
            self.assertEqual(record.supported_version, "1.18.*")
            self.assertFalse(Path(directory, "123").exists())

    def test_archive_inspection_supports_nonstandard_mod_descriptor(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            archive_path = Path(directory, "456_Legacy.zip")
            with ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "456/Legacy.mod",
                    'name="Legacy"\nsupported_version="1.12.*"\nremote_file_id="456"\n',
                )
                archive.writestr("456/events/test.txt", "test")
            self.assertEqual(inspect_mod_archive(archive_path).mod_id, "456")

    def test_archive_inventory_keeps_wallpapers_and_bad_zips_separate(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            wallpaper = Path(directory, "111_Wallpaper.zip")
            with ZipFile(wallpaper, "w") as archive:
                archive.writestr("project.json", "{}")
                archive.writestr("scene.pkg", "data")
            Path(directory, "222_Broken.zip").write_bytes(b"not a zip")
            statuses = {
                record.archive_path.name: record.status
                for record in inspect_archive_directory(Path(directory))
            }
            self.assertEqual(statuses["111_Wallpaper.zip"], "wallpaper_engine")
            self.assertEqual(statuses["222_Broken.zip"], "unreadable_archive")

    def test_duplicate_archive_ids_are_reported(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            for filename in ("123_Test.zip", "123_Test (1).zip"):
                with ZipFile(Path(directory, filename), "w") as archive:
                    archive.writestr(
                        "descriptor.mod",
                        'name="Test"\nsupported_version="1.18.*"\nremote_file_id="123"\n',
                    )
            records = inspect_archive_directory(Path(directory))
            self.assertEqual(set(duplicate_archive_ids(records)), {"123"})

    def test_declared_compatibility_handles_ck3_wildcards(self) -> None:
        self.assertEqual(declared_compatibility("1.18.*", "1.18.0"),
                         "declared_compatible")
        self.assertEqual(declared_compatibility("1.*", "1.18.0"),
                         "declared_compatible")
        self.assertEqual(declared_compatibility("1.12.*", "1.18.0"),
                         "declared_incompatible")
        self.assertEqual(declared_compatibility(None, "1.18.0"), "unknown")

    def test_archive_timestamp_is_only_an_estimate(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            archive_path = Path(directory, "123_Test.zip")
            with ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "descriptor.mod",
                    'name="Test"\nsupported_version="1.18.*"\nremote_file_id="123"\n',
                )
            record = inspect_mod_archive(archive_path)
            self.assertEqual(
                archive_timestamp_signal(record, record.archive_modified + 1),
                "remote_newer_than_archive_timestamp",
            )
            self.assertEqual(archive_timestamp_signal(record, None), "unknown")

    def test_archive_scan_keeps_each_file_and_separates_evidence(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            for filename in ("123_Test.zip", "123_Test Copy.zip"):
                with ZipFile(Path(directory, filename), "w") as archive:
                    archive.writestr(
                        "descriptor.mod",
                        'name="Test"\nsupported_version="1.18.*"\nremote_file_id="123"\n',
                    )
            with ZipFile(Path(directory, "999_Wallpaper.zip"), "w") as archive:
                archive.writestr("project.json", "{}")

            rows = scan_archives(
                Path(directory),
                "1.18.0",
                lambda ids: [workshop_item(ids[0], 300)],
            )

            mod_rows = [row for row in rows if row.archive.status == "ck3_mod"]
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(mod_rows), 2)
            self.assertTrue(all(row.workshop_status == "available" for row in mod_rows))
            self.assertTrue(all(row.compatibility == "declared_compatible" for row in mod_rows))
            self.assertTrue(all(row.duplicate_count == 2 for row in mod_rows))
            self.assertEqual(rows[-1].workshop_status, "not_applicable")

    def test_archive_scan_reports_lookup_failure_without_update_claim(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            with ZipFile(Path(directory, "123_Test.zip"), "w") as archive:
                archive.writestr(
                    "descriptor.mod",
                    'name="Test"\nsupported_version="1.*"\nremote_file_id="123"\n',
                )

            rows = scan_archives(
                Path(directory),
                "1.18.0",
                lambda _ids: (_ for _ in ()).throw(ValueError("offline")),
            )

            self.assertEqual(rows[0].workshop_status, "scan_error")
            self.assertEqual(rows[0].timestamp_signal, "not_applicable")
            self.assertIsNone(rows[0].workshop_updated)


if __name__ == "__main__":
    unittest.main()
