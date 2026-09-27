"""Regression tests for verified non-database snapshot restoration."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest

from playset_state import PlaysetMod, PlaysetState
from save_inspection import SaveMetadata
from setup_snapshots import SnapshotManager
from snapshot_restore import SnapshotRestoreManager, build_restore_plan


class SnapshotRestoreTests(unittest.TestCase):
    def make_snapshot(self, root: Path):
        data = root / "game-data"
        installed = data / "mod" / "123"
        installed.mkdir(parents=True)
        (installed / "content.txt").write_text("snapshot installed", encoding="utf-8")
        descriptor = data / "mod" / "123.mod"
        descriptor.write_text("snapshot descriptor", encoding="utf-8")
        (data / "dlc_load.json").write_text('{"enabled_mods": ["mod/123.mod"]}', encoding="utf-8")
        archive_root = root / "archives"
        archive_root.mkdir()
        archive = archive_root / "123.zip"
        archive.write_text("snapshot archive", encoding="utf-8")
        save_root = data / "save games"
        save_root.mkdir()
        save_path = save_root / "campaign.ck3"
        save_path.write_text("snapshot save", encoding="utf-8")
        save = SaveMetadata(save_path, "1.18.0", "1100.1.1", None, None, (), ("123",), ())
        mod = PlaysetMod("registry", "mod/123.mod", "Test", "123", installed, descriptor, 1)
        playset = PlaysetState(data, "Test", (mod,), ())
        snapshot_manager = SnapshotManager(root / "snapshots")
        snapshot = snapshot_manager.create("Working", "1.18.0", playset, {"123": [archive]}, save)
        return data, installed, descriptor, archive_root, archive, playset, snapshot_manager, snapshot

    def test_restore_and_undo_preserve_current_files_and_do_not_edit_configuration(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            data, installed, descriptor, archive_root, archive, playset, snapshots, snapshot = self.make_snapshot(root)
            (installed / "content.txt").write_text("current installed", encoding="utf-8")
            descriptor.write_text("current descriptor", encoding="utf-8")
            archive.write_text("current archive", encoding="utf-8")
            (data / "dlc_load.json").write_text("current configuration", encoding="utf-8")
            plan = build_restore_plan(snapshots, snapshot.directory, archive_root, data, playset)
            manager = SnapshotRestoreManager(root / "restore-journals", process_check=lambda: ())

            transaction_id = manager.prepare(plan)
            result = manager.apply(transaction_id)

            self.assertEqual((installed / "content.txt").read_text(encoding="utf-8"), "snapshot installed")
            self.assertEqual(descriptor.read_text(encoding="utf-8"), "snapshot descriptor")
            self.assertEqual(archive.read_text(encoding="utf-8"), "snapshot archive")
            self.assertEqual((data / "dlc_load.json").read_text(encoding="utf-8"), "current configuration")
            self.assertTrue(result.restored_save and result.restored_save.is_file())
            self.assertIn("dlc_load.json", plan.configuration_differences)

            manager.rollback(transaction_id)

            self.assertEqual((installed / "content.txt").read_text(encoding="utf-8"), "current installed")
            self.assertEqual(descriptor.read_text(encoding="utf-8"), "current descriptor")
            self.assertEqual(archive.read_text(encoding="utf-8"), "current archive")
            self.assertFalse(result.restored_save.exists())

    def test_startup_recovery_cancels_prepared_restore(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            data, _installed, _descriptor, archive_root, _archive, playset, snapshots, snapshot = self.make_snapshot(root)
            plan = build_restore_plan(snapshots, snapshot.directory, archive_root, data, playset)
            manager = SnapshotRestoreManager(root / "restore-journals", process_check=lambda: ())
            manager.prepare(plan)

            results = manager.recover_pending()

            self.assertEqual(results[-1].action, "cancelled")

    def test_restore_and_undo_accept_content_already_matching_snapshot(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            data, installed, _descriptor, archive_root, _archive, playset, snapshots, snapshot = self.make_snapshot(root)
            plan = build_restore_plan(snapshots, snapshot.directory, archive_root, data, playset)
            manager = SnapshotRestoreManager(root / "restore-journals", process_check=lambda: ())

            transaction_id = manager.prepare(plan)
            manager.apply(transaction_id)
            manager.rollback(transaction_id)

            self.assertEqual((installed / "content.txt").read_text(encoding="utf-8"), "snapshot installed")

    def test_recovery_rolls_back_one_replaced_path_with_later_paths_untouched(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            data, _installed, _descriptor, archive_root, archive, playset, snapshots, snapshot = self.make_snapshot(root)
            archive.write_text("current archive", encoding="utf-8")
            plan = build_restore_plan(snapshots, snapshot.directory, archive_root, data, playset)
            manager = SnapshotRestoreManager(root / "restore-journals", process_check=lambda: ())
            transaction_id = manager.prepare(plan)
            payload = manager._load(transaction_id)
            first = next(
                value for value in payload["operations"]
                if value["category"] == "source_archive"
            )
            paths = manager._validate_operation(first)
            paths["backup"].parent.mkdir(parents=True, exist_ok=True)
            os.replace(paths["destination"], paths["backup"])
            os.replace(paths["stage"], paths["destination"])
            first["state"] = "replaced"
            payload["phase"] = "replaced"
            manager._write(payload)

            results = manager.recover_pending()

            self.assertEqual(results[-1].action, "rolled_back", results[-1].detail)
            self.assertEqual(archive.read_text(encoding="utf-8"), "current archive")


if __name__ == "__main__":
    unittest.main()
