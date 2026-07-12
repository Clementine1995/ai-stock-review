from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.check_history_readiness import (
    check_hhxg_history_readiness,
    render_hhxg_history_readiness,
)
from stock_review.evidence.manage_evidence_snapshot import EvidenceSnapshotError


class HistoryReadinessTest(unittest.TestCase):
    def test_readiness_counts_only_date_verified_hhxg_snapshots(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-06", "2026-07-06", has_hhxg_event=True, missing_fields=[])
            self.write_snapshot(root, "2026-07-07", "2026-07-07", has_hhxg_event=False, missing_fields=[])
            self.write_snapshot(root, "2026-07-08", "2026-07-07", has_hhxg_event=True, missing_fields=["missing_sectors"])

            readiness = check_hhxg_history_readiness("2026-07-06", "2026-07-08", root)
            content = render_hhxg_history_readiness(readiness)

            self.assertEqual([day.trade_date for day in readiness.valid_days], ["2026-07-06"])
            self.assertEqual(readiness.snapshot_dates_without_valid_hhxg, ("2026-07-07", "2026-07-08"))
            self.assertFalse(readiness.is_ready)
            self.assertIn("还需连续采集 4 个有效交易日", content)

    def test_readiness_enables_history_after_five_date_verified_snapshots(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            for trade_date in ("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"):
                self.write_snapshot(root, trade_date, trade_date, has_hhxg_event=True, missing_fields=[])

            readiness = check_hhxg_history_readiness("2026-07-06", "2026-07-10", root)

            self.assertTrue(readiness.is_ready)
            self.assertIn("可按其它历史报告", render_hhxg_history_readiness(readiness))

    def test_readiness_rejects_reversed_date_range(self):
        with TemporaryDirectory() as temp_path:
            with self.assertRaisesRegex(EvidenceSnapshotError, "开始日期不能晚于结束日期"):
                check_hhxg_history_readiness("2026-07-10", "2026-07-06", Path(temp_path))

    @staticmethod
    def write_snapshot(
        root: Path,
        trade_date: str,
        sample_date: str,
        has_hhxg_event: bool,
        missing_fields: list[str],
    ) -> None:
        payload = {
            "trade_date": trade_date,
            "source": "akshare",
            "sample_date": sample_date,
            "market": {},
            "sentiment": {},
            "sectors": [],
            "stocks": [],
            "events": [{"title": "hhxg 最近交易日快照", "source": "hhxg"}] if has_hhxg_event else [],
            "missing_fields": missing_fields,
        }
        (root / f"{trade_date}_snapshot.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
