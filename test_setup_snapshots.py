"""Regression tests for complete, verified setup snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from playset_state import PlaysetMod, PlaysetState
from save_inspection import SaveMetadata
from setup_snapshots import SnapshotManager


class SetupSnapshotTests(unittest.TestCase):
    def test_snapshot_copies_active_content_archives_configuration_and_save(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            game_data = root / "game-data"
            installed = game_data / "mod" / "123"
            installed.mkdir(parents=True)
            (installed / "content.txt").write_text("installed", encoding="utf-8")
            descriptor = game_data / "mod" / "123.mod"
            descriptor.write_text('name="Test"', encoding="utf-8")
            for filename in ("dlc_load.json", "game_data.json", "mods_registry.json"):
                (game_data / filename).write_text("{}", encoding="utf-8")
            archive = root / "123_Test.zip"
            archive.write_bytes(b"archive")
            save_path = root / "campaign.ck3"
            save_path.write_bytes(b"save")
            save = SaveMetadata(
                path=save_path,
                game_version="1.18.0",
                game_date="1100.1.1",
                player_name="Player",
                title_name="Title",
                mod_references=("mod/123.mod",),
                mod_ids=("123",),
                dlcs=(),
            )
            playset = PlaysetState(
                data_directory=game_data,
                provider="Test",
                mods=(
                    PlaysetMod(
                        registry_id="entry",
                        game_registry_id="mod/123.mod",
                        display_name="Test",
                        mod_id="123",
                        directory=installed,
                        descriptor_path=descriptor,
                        position=1,
                    ),
                ),
                disabled_dlcs=(),
            )
            manager = SnapshotManager(root / "snapshots")

            estimate = manager.estimate(playset, {"123": [archive]}, save)
            result = manager.create("Working setup", "1.18.0", playset, {"123": [archive]}, save)

            self.assertEqual(result.file_count, estimate.file_count)
            self.assertTrue((result.directory / "archives" / archive.name).is_file())
            self.assertTrue((result.directory / "installed" / "mod" / "123" / "content.txt").is_file())
            self.assertTrue((result.directory / "save" / save_path.name).is_file())
            self.assertEqual(manager.verify(result.directory), ())
            manifest = json.loads((result.directory / "snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["enabled_mods"][0]["mod_id"], "123")

    def test_snapshot_verification_detects_changed_content(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            game_data = root / "game-data"
            installed = game_data / "mod" / "123"
            installed.mkdir(parents=True)
            content = installed / "content.txt"
            content.write_text("before", encoding="utf-8")
            descriptor = game_data / "mod" / "123.mod"
            descriptor.write_text('name="Test"', encoding="utf-8")
            playset = PlaysetState(
                data_directory=game_data,
                provider="Test",
                mods=(
                    PlaysetMod(
                        registry_id="entry",
                        game_registry_id="mod/123.mod",
                        display_name="Test",
                        mod_id="123",
                        directory=installed,
                        descriptor_path=descriptor,
                        position=1,
                    ),
                ),
                disabled_dlcs=(),
            )
            manager = SnapshotManager(root / "snapshots")
            result = manager.create("Working setup", "1.18.0", playset, {})
            copied = result.directory / "installed" / "mod" / "123" / "content.txt"
            copied.write_text("after", encoding="utf-8")

            problems = manager.verify(result.directory)

            self.assertEqual(len(problems), 1)
            self.assertIn("Hash mismatch", problems[0])


if __name__ == "__main__":
    unittest.main()
