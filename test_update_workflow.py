"""Regression tests for persistent baselines and recoverable archive replacement."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from install_ledger import InstallLedger, LedgerError
from mod_scan_core import inspect_mod_archive, sha256_file
from update_workflow import UpdateError, UpdateManager


def create_mod_archive(path: Path, mod_id: str, version: str) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "descriptor.mod",
            f'name="Test {mod_id}"\nversion="{version}"\nremote_file_id="{mod_id}"\n',
        )
        archive.writestr("common/test.txt", version)


class FailingInstallLedger(InstallLedger):
    def record_archive(self, *args, **kwargs):
        raise LedgerError("simulated ledger failure")


class UpdateWorkflowTests(unittest.TestCase):
    def test_ledger_round_trip_records_an_exact_archive(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            archive_path = root / "123_Test.zip"
            create_mod_archive(archive_path, "123", "1")
            ledger = InstallLedger(root / "data" / "ledger.json")

            recorded = ledger.record_archive(
                inspect_mod_archive(archive_path),
                200,
                source="manual_confirmation",
            )

            loaded = ledger.load()["123"]
            self.assertEqual(loaded, recorded)
            self.assertEqual(loaded.archive_sha256, sha256_file(archive_path))
            self.assertEqual(loaded.archive_path, archive_path.resolve())

    def test_successful_update_is_backed_up_recorded_and_reversible(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "123_Test.zip"
            candidate = root / "downloaded.zip"
            create_mod_archive(current, "123", "1")
            create_mod_archive(candidate, "123", "2")
            old_hash = sha256_file(current)
            new_hash = sha256_file(candidate)
            ledger = InstallLedger(root / "data" / "ledger.json")
            previous = ledger.record_archive(
                inspect_mod_archive(current),
                100,
                source="manual_confirmation",
            )
            manager = UpdateManager(root / "data" / "updates", ledger)

            prepared = manager.prepare_update(
                current,
                candidate,
                expected_mod_id="123",
                workshop_updated=200,
            )
            self.assertEqual(sha256_file(current), old_hash)
            self.assertTrue(prepared.staged_archive.is_file())

            result = manager.apply_update(prepared.transaction_id)
            self.assertEqual(sha256_file(current), new_hash)
            self.assertEqual(sha256_file(result.backup_path), old_hash)
            self.assertEqual(ledger.load()["123"].workshop_updated, 200)
            self.assertEqual(
                manager.latest_recoverable("123", current),
                prepared.transaction_id,
            )

            manager.rollback(prepared.transaction_id)
            self.assertEqual(sha256_file(current), old_hash)
            self.assertEqual(ledger.load()["123"], previous)
            journal = json.loads(
                (manager.journal_directory / f"{prepared.transaction_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(journal["phase"], "rolled_back")

    def test_wrong_workshop_item_is_rejected_before_staging(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "123_Test.zip"
            candidate = root / "wrong.zip"
            create_mod_archive(current, "123", "1")
            create_mod_archive(candidate, "999", "2")
            original_hash = sha256_file(current)
            manager = UpdateManager(
                root / "data" / "updates",
                InstallLedger(root / "data" / "ledger.json"),
            )

            with self.assertRaises(UpdateError):
                manager.prepare_update(
                    current,
                    candidate,
                    expected_mod_id="123",
                    workshop_updated=200,
                )
            self.assertEqual(sha256_file(current), original_hash)

    def test_identical_candidate_is_rejected_before_journaling(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "123_Test.zip"
            candidate = root / "candidate.zip"
            create_mod_archive(current, "123", "1")
            shutil.copy2(current, candidate)
            manager = UpdateManager(
                root / "data" / "updates",
                InstallLedger(root / "data" / "ledger.json"),
            )

            with self.assertRaisesRegex(UpdateError, "identical"):
                manager.prepare_update(
                    current,
                    candidate,
                    expected_mod_id="123",
                    workshop_updated=200,
                )
            self.assertFalse(manager.journal_directory.exists())

    def test_target_change_after_preparation_is_never_overwritten(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "123_Test.zip"
            candidate = root / "candidate.zip"
            create_mod_archive(current, "123", "1")
            create_mod_archive(candidate, "123", "2")
            manager = UpdateManager(
                root / "data" / "updates",
                InstallLedger(root / "data" / "ledger.json"),
            )
            prepared = manager.prepare_update(
                current,
                candidate,
                expected_mod_id="123",
                workshop_updated=200,
            )
            create_mod_archive(current, "123", "manual-change")
            changed_hash = sha256_file(current)

            with self.assertRaises(UpdateError):
                manager.apply_update(prepared.transaction_id)
            self.assertEqual(sha256_file(current), changed_hash)
            self.assertFalse(prepared.backup_archive.exists())

    def test_ledger_failure_rolls_back_the_archive(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "123_Test.zip"
            candidate = root / "candidate.zip"
            create_mod_archive(current, "123", "1")
            create_mod_archive(candidate, "123", "2")
            original_hash = sha256_file(current)
            manager = UpdateManager(
                root / "data" / "updates",
                FailingInstallLedger(root / "data" / "ledger.json"),
            )
            prepared = manager.prepare_update(
                current,
                candidate,
                expected_mod_id="123",
                workshop_updated=200,
            )

            with self.assertRaisesRegex(UpdateError, "original archive was restored"):
                manager.apply_update(prepared.transaction_id)
            self.assertEqual(sha256_file(current), original_hash)

    def test_startup_recovery_cancels_an_unapplied_staged_update(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "123_Test.zip"
            candidate = root / "candidate.zip"
            create_mod_archive(current, "123", "1")
            create_mod_archive(candidate, "123", "2")
            original_hash = sha256_file(current)
            ledger = InstallLedger(root / "data" / "ledger.json")
            manager = UpdateManager(root / "data" / "updates", ledger)
            prepared = manager.prepare_update(
                current,
                candidate,
                expected_mod_id="123",
                workshop_updated=200,
            )

            recovered = UpdateManager(root / "data" / "updates", ledger).recover_pending()

            self.assertEqual(recovered[0].action, "cancelled")
            self.assertFalse(prepared.staged_archive.exists())
            self.assertEqual(sha256_file(current), original_hash)

    def test_startup_recovery_restores_an_interrupted_replacement(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "123_Test.zip"
            candidate = root / "candidate.zip"
            create_mod_archive(current, "123", "1")
            create_mod_archive(candidate, "123", "2")
            original_hash = sha256_file(current)
            ledger = InstallLedger(root / "data" / "ledger.json")
            previous = ledger.record_archive(
                inspect_mod_archive(current),
                100,
                source="manual_confirmation",
            )
            manager = UpdateManager(root / "data" / "updates", ledger)
            prepared = manager.prepare_update(
                current,
                candidate,
                expected_mod_id="123",
                workshop_updated=200,
            )

            prepared.backup_archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current, prepared.backup_archive)
            shutil.copy2(prepared.staged_archive, current)
            journal_path = manager.journal_directory / f"{prepared.transaction_id}.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["phase"] = "replaced"
            journal_path.write_text(json.dumps(journal), encoding="utf-8")

            recovered = UpdateManager(root / "data" / "updates", ledger).recover_pending()

            self.assertEqual(recovered[0].action, "rolled_back")
            self.assertEqual(sha256_file(current), original_hash)
            self.assertEqual(ledger.load()["123"], previous)
            self.assertFalse(prepared.staged_archive.exists())

    def test_startup_recovery_refuses_to_overwrite_an_independent_change(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "123_Test.zip"
            candidate = root / "candidate.zip"
            create_mod_archive(current, "123", "1")
            create_mod_archive(candidate, "123", "2")
            ledger = InstallLedger(root / "data" / "ledger.json")
            manager = UpdateManager(root / "data" / "updates", ledger)
            prepared = manager.prepare_update(
                current,
                candidate,
                expected_mod_id="123",
                workshop_updated=200,
            )
            prepared.backup_archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current, prepared.backup_archive)
            journal_path = manager.journal_directory / f"{prepared.transaction_id}.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["phase"] = "replaced"
            journal_path.write_text(json.dumps(journal), encoding="utf-8")
            create_mod_archive(current, "123", "independent-change")
            independent_hash = sha256_file(current)

            recovered = UpdateManager(root / "data" / "updates", ledger).recover_pending()

            self.assertEqual(recovered[0].action, "error")
            self.assertIn("automatic recovery was refused", recovered[0].detail)
            self.assertEqual(sha256_file(current), independent_hash)


if __name__ == "__main__":
    unittest.main()
