from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import (
    check_evidence_snapshot,
    import_evidence_snapshot,
)
from stock_review.storage.sqlite_repository import EvidenceSnapshotRepository


class EvidenceSnapshotTest(unittest.TestCase):
    def test_missing_fields_identify_required_evidence_gaps(self):
        snapshot = build_evidence_snapshot("2026-07-06", {"source": "manual"})

        self.assertEqual(
            list(snapshot.missing_fields),
            [
                "missing_indices",
                "missing_total_amount",
                "missing_sentiment",
                "missing_sectors",
                "missing_stocks",
            ],
        )

    def test_sample_evidence_import_writes_snapshot_and_sqlite_record(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            output_dir = root / "evidence"
            database_path = root / "stock_review.sqlite"

            output_path = import_evidence_snapshot(
                "2026-07-06",
                Path("data/evidence/2026-07-06_sample.json"),
                output_dir=output_dir,
                database_path=database_path,
            )

            snapshot_data = json.loads(output_path.read_text(encoding="utf-8"))
            stored_data = EvidenceSnapshotRepository(database_path).load_snapshot("2026-07-06")

            self.assertEqual(output_path, output_dir / "2026-07-06_snapshot.json")
            self.assertEqual(snapshot_data["missing_fields"], [])
            self.assertIsNotNone(stored_data)
            self.assertEqual(stored_data["missing_fields"], [])

    def test_check_evidence_snapshot_reads_existing_snapshot(self):
        with TemporaryDirectory() as temp_path:
            snapshot_dir = Path(temp_path)
            import_evidence_snapshot(
                "2026-07-06",
                Path("data/evidence/2026-07-06_sample.json"),
                output_dir=snapshot_dir,
                database_path=snapshot_dir / "stock_review.sqlite",
            )

            snapshot = check_evidence_snapshot("2026-07-06", snapshot_dir=snapshot_dir)

            self.assertEqual(snapshot.trade_date, "2026-07-06")
            self.assertEqual(snapshot.missing_fields, ())


if __name__ == "__main__":
    unittest.main()
