"""Regression tests for combined archive and installed-folder updates."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from batch_update import BatchPlanItem
from coordinated_update import CoordinatedUpdateError, CoordinatedUpdateManager
from install_ledger import InstallLedger
from installed_deployment import DeploymentError, InstalledDeploymentManager
from mod_scan_core import sha256_file
from playset_state import PlaysetMod
from update_impact import compare_archives
from update_workflow import UpdateManager


def create_archive(path: Path, mod_id: str, value: str) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            f"{mod_id}.mod",
            f'name="Test"\nremote_file_id="{mod_id}"\npath="mod/{mod_id}"\n',
        )
        archive.writestr(
            f"{mod_id}/descriptor.mod",
            f'name="Test"\nremote_file_id="{mod_id}"\nversion="{value}"\n',
        )
        archive.writestr(f"{mod_id}/common/value.txt", value)


class FailingInstalledManager(InstalledDeploymentManager):
    def apply(self, transaction_id: str):
        raise DeploymentError("simulated installed deployment failure")


class CoordinatedUpdateTests(unittest.TestCase):
    def make_fixture(self, root: Path, installed_class=InstalledDeploymentManager):
        current = root / "library" / "123_current.zip"
        candidate = root / "downloads" / "123_new.zip"
        current.parent.mkdir()
        candidate.parent.mkdir()
        create_archive(current, "123", "old")
        create_archive(candidate, "123", "new")
        data = root / "game-data"
        target = data / "mod" / "123"
        target.mkdir(parents=True)
        (target / "common").mkdir()
        (target / "common" / "value.txt").write_text("old", encoding="utf-8")
        descriptor = data / "mod" / "123.mod"
        descriptor.write_text('name="Old"\npath="mod/123"\n', encoding="utf-8")
        playset_mod = PlaysetMod(
            "registry", "mod/123.mod", "Test", "123", target, descriptor, 1
        )
        item = BatchPlanItem(
            "123", "Test", current, candidate, 200, compare_archives(current, candidate)
        )
        archive_manager = UpdateManager(
            root / "state" / "updates",
            InstallLedger(root / "state" / "ledger.json"),
        )
        installed_manager = installed_class(
            root / "state" / "installed",
            process_check=lambda: (),
        )
        coordinator = CoordinatedUpdateManager(
            root / "state" / "coordinated",
            archive_manager,
            installed_manager,
        )
        return current, candidate, data, target, playset_mod, item, coordinator

    def test_combined_update_changes_library_and_enabled_installation(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            current, candidate, data, target, mod, item, coordinator = self.make_fixture(Path(directory))

            result = coordinator.apply([item], {"123": mod}, data)

            self.assertEqual(sha256_file(current), sha256_file(candidate))
            self.assertEqual((target / "common" / "value.txt").read_text(encoding="utf-8"), "new")
            self.assertEqual(len(result.archive_updates), 1)
            self.assertEqual(len(result.installed_updates), 1)

    def test_installed_failure_rolls_archive_back(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            current, _candidate, data, target, mod, item, coordinator = self.make_fixture(
                Path(directory), FailingInstalledManager
            )
            original_hash = sha256_file(current)

            with self.assertRaisesRegex(CoordinatedUpdateError, "rolled back"):
                coordinator.apply([item], {"123": mod}, data)

            self.assertEqual(sha256_file(current), original_hash)
            self.assertEqual((target / "common" / "value.txt").read_text(encoding="utf-8"), "old")

    def test_manual_undo_restores_archive_and_enabled_installation_together(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            current, _candidate, data, target, mod, item, coordinator = self.make_fixture(Path(directory))
            original_hash = sha256_file(current)
            result = coordinator.apply([item], {"123": mod}, data)

            self.assertEqual(coordinator.latest_recoverable("123"), result.transaction_id)
            rollback = coordinator.rollback(result.transaction_id)

            self.assertEqual(rollback.archive_count, 1)
            self.assertEqual(rollback.installed_count, 1)
            self.assertEqual(sha256_file(current), original_hash)
            self.assertEqual((target / "common" / "value.txt").read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
