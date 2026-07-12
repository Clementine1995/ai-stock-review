from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import (
    EvidenceSnapshotError,
    check_evidence_snapshot,
    collect_akshare_evidence_scopes,
    import_evidence_snapshot,
    merge_evidence_data,
)
from stock_review.reports.render_markdown import render_sentiment_lines
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

    def test_missing_emotion_temperature_is_reported_separately(self):
        snapshot = build_evidence_snapshot(
            "2026-07-06",
            {
                "source": "manual",
                "market": {"indices": [{"name": "上证指数"}], "total_amount": 1},
                "sentiment": {
                    "limit_up_count": 58,
                    "limit_down_count": 7,
                    "broken_board_rate": 0.31,
                    "highest_board": 5,
                },
                "sectors": [{"name": "机器人"}],
                "stocks": [{"code": "300024", "name": "机器人"}],
            },
        )

        self.assertNotIn("missing_sentiment", snapshot.missing_fields)
        self.assertIn("missing_emotion_temperature", snapshot.missing_fields)

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

    def test_daily_scope_collection_keeps_successful_scopes_after_partial_failure(self):
        calls: list[str] = []

        def fake_collector(trade_date, scope, output_dir, database_path, refresh):
            calls.append(scope)
            if scope == "sentiment":
                raise EvidenceSnapshotError("情绪源不可用")
            return output_dir / f"{trade_date}_{scope}.json"

        results = collect_akshare_evidence_scopes(
            "2026-07-06",
            scopes=["market", "sentiment", "sectors"],
            output_dir=Path("data/evidence"),
            collector=fake_collector,
        )

        self.assertEqual(calls, ["market", "sentiment", "sectors"])
        self.assertTrue(results[0].is_successful)
        self.assertEqual(results[1].error_message, "情绪源不可用")
        self.assertTrue(results[2].is_successful)

    def test_daily_scope_collection_requires_explicit_scope(self):
        with self.assertRaisesRegex(EvidenceSnapshotError, "至少需要显式传入一个 scope"):
            collect_akshare_evidence_scopes("2026-07-06", scopes=[])

    def test_daily_scope_collection_rejects_duplicate_scope(self):
        with self.assertRaisesRegex(EvidenceSnapshotError, "scope 不能重复传入"):
            collect_akshare_evidence_scopes("2026-07-06", scopes=["market", "market"])

    def test_merge_records_each_replaced_field_source(self):
        merged = merge_evidence_data(
            {
                "source": "akshare",
                "market": {"indices": [{"name": "上证指数"}]},
                "field_sources": {"market": "akshare", "stocks": "akshare"},
            },
            {
                "source": "hhxg",
                "sentiment": {"limit_up_count": 92},
                "sectors": [{"name": "军工", "source_type": "hhxg_hot_theme"}],
            },
        )

        self.assertEqual(
            merged["field_sources"],
            {"market": "akshare", "stocks": "akshare", "sentiment": "hhxg", "sectors": "hhxg"},
        )

    def test_render_marks_old_snapshot_field_source_as_unconfirmed(self):
        snapshot = build_evidence_snapshot(
            "2026-07-06",
            {"source": "hhxg", "sentiment": {"limit_up_count": 92}},
        )

        self.assertIn("待确认（旧快照未记录字段来源）", render_sentiment_lines(snapshot)[0])


if __name__ == "__main__":
    unittest.main()
