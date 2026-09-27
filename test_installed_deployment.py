"""Regression tests for recoverable deployment into CK3's installed mod folder."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from zipfile import ZipFile

from installed_deployment import DeploymentError, InstalledDeploymentManager
from playset_state import PlaysetMod


def create_candidate(path: Path, mod_id: str, value: str) -> None:
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


class InstalledDeploymentTests(unittest.TestCase):
    def make_fixture(self, root: Path):
        data = root / "game-data"
        target = data / "mod" / "123"
        target.mkdir(parents=True)
        (target / "old.txt").write_text("old", encoding="utf-8")
        descriptor = data / "mod" / "123.mod"
        descriptor.write_text('name="Old"\npath="mod/123"\n', encoding="utf-8")
        candidate = root / "candidate.zip"
        create_candidate(candidate, "123", "new")
        mod = PlaysetMod(
            registry_id="registry",
            game_registry_id="mod/123.mod",
            display_name="Test",
            mod_id="123",
            directory=target,
            descriptor_path=descriptor,
            position=1,
        )
        manager = InstalledDeploymentManager(root / "journals", process_check=lambda: ())
        return data, target, descriptor, candidate, mod, manager

    def test_apply_and_rollback_preserve_verified_installed_backup(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            data, target, descriptor, candidate, mod, manager = self.make_fixture(Path(directory))

            prepared = manager.prepare(candidate, mod, data)
            result = manager.apply(prepared.transaction_id)

            self.assertEqual((target / "common" / "value.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse((target / "old.txt").exists())
            self.assertIn('path="mod/123"', descriptor.read_text(encoding="utf-8"))
            self.assertTrue(result.backup_directory and (result.backup_directory / "old.txt").is_file())

            manager.rollback(prepared.transaction_id)

            self.assertEqual((target / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual(descriptor.read_text(encoding="utf-8"), 'name="Old"\npath="mod/123"\n')
            self.assertTrue(result.backup_directory and result.backup_directory.is_dir())

    def test_apply_refuses_installed_content_changed_after_prepare(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            data, target, _descriptor, candidate, mod, manager = self.make_fixture(Path(directory))
            prepared = manager.prepare(candidate, mod, data)
            (target / "old.txt").write_text("changed outside", encoding="utf-8")

            with self.assertRaisesRegex(DeploymentError, "changed after"):
                manager.apply(prepared.transaction_id)

            self.assertEqual((target / "old.txt").read_text(encoding="utf-8"), "changed outside")
            self.assertEqual(manager.transaction_phase(prepared.transaction_id), "cancelled")

    def test_startup_recovery_cancels_untouched_staging(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            data, _target, _descriptor, candidate, mod, manager = self.make_fixture(Path(directory))
            prepared = manager.prepare(candidate, mod, data)

            results = manager.recover_pending()

            self.assertEqual(results[0].action, "cancelled")
            self.assertEqual(manager.transaction_phase(prepared.transaction_id), "cancelled")

    def test_recovery_restores_content_when_descriptor_was_not_yet_touched(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            data, target, descriptor, candidate, mod, manager = self.make_fixture(Path(directory))
            prepared = manager.prepare(candidate, mod, data)
            payload = manager._load_journal(prepared.transaction_id)
            paths = manager._paths(payload)
            paths["backup_directory"].parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, paths["backup_directory"])
            os.replace(paths["staged_content"], target)
            payload["phase"] = "content_replaced"
            manager._write_journal(payload)

            results = manager.recover_pending()

            self.assertEqual(results[0].action, "rolled_back")
            self.assertEqual((target / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual(descriptor.read_text(encoding="utf-8"), 'name="Old"\npath="mod/123"\n')

    def test_running_game_or_manager_blocks_preparation(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            data, _target, _descriptor, candidate, mod, _manager = self.make_fixture(Path(directory))
            manager = InstalledDeploymentManager(
                Path(directory) / "journals-blocked",
                process_check=lambda: ("ck3.exe",),
            )

            with self.assertRaisesRegex(DeploymentError, "Still running"):
                manager.prepare(candidate, mod, data)


if __name__ == "__main__":
    unittest.main()
