"""Regression tests for return-from-hiatus scan comparisons."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from return_history import ReturnHistory, compare_history, load_history, save_history


class ReturnHistoryTests(unittest.TestCase):
    def test_history_round_trip_and_hiatus_comparison(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "history.json"
            previous = ReturnHistory(
                "2026-01-01T00:00:00+00:00",
                "1.18.0",
                ("100", "200"),
                {"100": 10, "200": 20},
            )
            save_history(path, previous)

            loaded = load_history(path)
            comparison = compare_history(
                loaded,
                game_version="1.19.0",
                enabled_mod_ids=("100", "300"),
                workshop_updated={"100": 11, "300": 30},
                now=datetime(2026, 1, 11, tzinfo=timezone.utc),
            )

            self.assertEqual(loaded, previous)
            self.assertEqual(comparison.elapsed_days, 10)
            self.assertTrue(comparison.game_version_changed)
            self.assertEqual(comparison.changed_workshop_ids, ("100",))
            self.assertEqual(comparison.added_mod_ids, ("300",))
            self.assertEqual(comparison.removed_mod_ids, ("200",))


if __name__ == "__main__":
    unittest.main()
