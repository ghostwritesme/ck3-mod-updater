"""Regression tests for read-only playset and save awareness."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from playset_state import read_active_playset
from save_inspection import compare_save_to_playset, inspect_save_metadata


class PlaysetAndSaveTests(unittest.TestCase):
    def test_active_playset_uses_launcher_order_and_enabled_set(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            (root / "mod").mkdir()
            (root / "IronyModManager_IntegrityCheck.txt").write_text("", encoding="utf-8")
            (root / "dlc_load.json").write_text(
                json.dumps(
                    {
                        "disabled_dlcs": ["Example DLC"],
                        "enabled_mods": ["mod/200.mod", "mod/100.mod"],
                    }
                ),
                encoding="utf-8",
            )
            (root / "mods_registry.json").write_text(
                json.dumps(
                    {
                        "first": {
                            "gameRegistryId": "mod/100.mod",
                            "displayName": "One Hundred",
                            "steamId": "100",
                            "dirPath": "mod/100/",
                            "source": "local",
                        },
                        "second": {
                            "gameRegistryId": "mod/200.mod",
                            "displayName": "Two Hundred",
                            "steamId": "200",
                            "dirPath": "mod/200/",
                            "source": "local",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / "game_data.json").write_text(
                json.dumps({"modsOrder": ["first", "second"]}),
                encoding="utf-8",
            )

            state = read_active_playset(root)

            self.assertEqual(state.provider, "Irony / Paradox launcher")
            self.assertEqual(state.ordered_mod_ids, ("100", "200"))
            self.assertEqual(state.mods[0].position, 1)
            self.assertEqual(state.disabled_dlcs, ("Example DLC",))

    def test_playset_rejects_registry_paths_outside_ck3_data(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            (root / "dlc_load.json").write_text(
                json.dumps({"enabled_mods": ["mod/123.mod"]}),
                encoding="utf-8",
            )
            (root / "mods_registry.json").write_text(
                json.dumps(
                    {
                        "entry": {
                            "gameRegistryId": "mod/123.mod",
                            "displayName": "Unsafe",
                            "steamId": "123",
                            "dirPath": "../../outside",
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "game_data.json").write_text(
                json.dumps({"modsOrder": ["entry"]}),
                encoding="utf-8",
            )

            state = read_active_playset(root)

            self.assertIsNone(state.mods[0].directory)
            self.assertIn("Unsafe mod directory ignored", state.warnings[0])

    def test_save_metadata_reports_version_mods_and_dlcs(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            save = Path(directory) / "campaign.ck3"
            save.write_bytes(
                b"SAV0100abcd\nmeta_data={\n"
                b' version="1.18.0"\n meta_date=1178.11.3\n'
                b' meta_player_name="Motofusa"\n meta_title_name="Oki"\n'
                b' dlcs={ "DLC One" "DLC Two" }\n'
                b' mods={ "mod/200.mod" "mod/100.mod" }\n}\nBINARY'
            )

            metadata = inspect_save_metadata(save)
            comparison = compare_save_to_playset(metadata, ("100", "300"))

            self.assertEqual(metadata.game_version, "1.18.0")
            self.assertEqual(metadata.mod_ids, ("200", "100"))
            self.assertEqual(metadata.dlcs, ("DLC One", "DLC Two"))
            self.assertFalse(comparison.matching_mods)
            self.assertEqual(comparison.missing_from_playset, ("200",))
            self.assertEqual(comparison.added_since_save, ("300",))


if __name__ == "__main__":
    unittest.main()
