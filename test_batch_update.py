"""Regression tests for explicit batch planning and transactional recovery."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from batch_update import BatchPlanItem, BatchUpdateError, BatchUpdateManager, build_batch_plan
from install_ledger import InstallLedger
from mod_scan_core import ArchiveRecord, ArchiveScanRow, sha256_file
from update_impact import compare_archives
from update_workflow import UpdateError, UpdateManager


def create_archive(path: Path, mod_id: str, value: str) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "descriptor.mod",
            f'name="Mod {mod_id}"\nremote_file_id="{mod_id}"\nversion="{value}"',
        )
        archive.writestr("common/value.txt", value)


class FailSecondApplyManager(UpdateManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_calls = 0

    def apply_update(self, transaction_id: str):
        self.apply_calls += 1
        if self.apply_calls == 2:
            raise UpdateError("simulated second update failure")
        return super().apply_update(transaction_id)


class BatchUpdateTests(unittest.TestCase):
    def test_batch_plan_matches_candidates_by_descriptor_id(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            current = root / "123_current.zip"
            candidate = root / "downloaded.zip"
            create_archive(current, "123", "old")
            create_archive(candidate, "123", "new")
            row = ArchiveScanRow(
                ArchiveRecord(current, "ck3_mod", mod_id="123", name="Test"),
                "available",
                workshop_updated=200,
            )

            plan = build_batch_plan([row], [candidate])

            self.assertEqual(len(plan.items), 1)
            self.assertEqual(plan.items[0].mod_id, "123")
            self.assertEqual(plan.issues, ())

    def test_successful_batch_replaces_every_validated_archive(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            items: list[BatchPlanItem] = []
            expected: dict[str, str] = {}
            for mod_id in ("100", "200"):
                current = root / f"{mod_id}_current.zip"
                candidate = root / f"{mod_id}_candidate.zip"
                create_archive(current, mod_id, "old")
                create_archive(candidate, mod_id, "new")
                expected[mod_id] = sha256_file(candidate)
                items.append(
                    BatchPlanItem(
                        mod_id,
                        f"Mod {mod_id}",
                        current,
                        candidate,
                        200,
                        compare_archives(current, candidate),
                    )
                )
            manager = UpdateManager(
                root / "data" / "updates",
                InstallLedger(root / "data" / "ledger.json"),
            )

            result = BatchUpdateManager(manager).apply(items)

            self.assertEqual(len(result.updates), 2)
            for item in items:
                self.assertEqual(sha256_file(item.current_archive), expected[item.mod_id])

    def test_batch_failure_rolls_back_earlier_commits(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            items: list[BatchPlanItem] = []
            originals: dict[str, str] = {}
            for mod_id in ("100", "200"):
                current = root / f"{mod_id}_current.zip"
                candidate = root / f"{mod_id}_candidate.zip"
                create_archive(current, mod_id, "old")
                create_archive(candidate, mod_id, "new")
                originals[mod_id] = sha256_file(current)
                items.append(
                    BatchPlanItem(
                        mod_id,
                        f"Mod {mod_id}",
                        current,
                        candidate,
                        200,
                        compare_archives(current, candidate),
                    )
                )
            manager = FailSecondApplyManager(
                root / "data" / "updates",
                InstallLedger(root / "data" / "ledger.json"),
            )

            with self.assertRaisesRegex(BatchUpdateError, "rolled back"):
                BatchUpdateManager(manager).apply(items)

            for item in items:
                self.assertEqual(sha256_file(item.current_archive), originals[item.mod_id])


if __name__ == "__main__":
    unittest.main()
