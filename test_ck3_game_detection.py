"""Tests for automatic CK3 installation and version detection."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ck3_game_detection import detect_ck3_installation, read_launcher_version


class CK3GameDetectionTests(unittest.TestCase):
    def test_reads_raw_ck3_version_and_resolves_game_root(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "Crusader Kings III"
            settings = root / "launcher" / "launcher-settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                json.dumps(
                    {
                        "gameId": "ck3",
                        "displayName": "Crusader Kings III",
                        "version": "1.18.0 (Crane)",
                        "rawVersion": "1.18.0",
                    }
                ),
                encoding="utf-8",
            )
            result = read_launcher_version(settings)
            self.assertIsNotNone(result)
            self.assertEqual(result.version, "1.18.0")
            self.assertEqual(result.game_root, root)

    def test_rejects_another_paradox_game(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            settings = Path(directory) / "launcher-settings.json"
            settings.write_text(
                json.dumps({"gameId": "stellaris", "rawVersion": "4.0.0"}),
                encoding="utf-8",
            )
            self.assertIsNone(read_launcher_version(settings))

    def test_detection_accepts_an_explicit_candidate_root(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory) / "CK3"
            settings = root / "launcher" / "launcher-settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                json.dumps({"gameId": "ck3", "version": "1.19.2 (Example)"}),
                encoding="utf-8",
            )
            result = detect_ck3_installation([root])
            self.assertIsNotNone(result)
            self.assertEqual(result.version, "1.19.2")


if __name__ == "__main__":
    unittest.main()
