from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.manage_evidence_snapshot import EvidenceSnapshotError
from stock_review.evidence.summarize_market_history import (
    create_market_history_report,
    summarize_market_history,
)


class MarketHistoryTest(unittest.TestCase):
    def test_history_reports_daily_market_facts_and_insufficient_history(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-06", close=100, change_percent=1, total_amount=10)
            self.write_snapshot(root, "2026-07-07", close=101, change_percent=1, total_amount=11)

            output_path, summary = create_market_history_report(
                "2026-07-06",
                "2026-07-07",
                snapshot_dir=root,
                output_dir=root / "reports",
                log_path=root / "logs" / "stock_review.log",
            )

            self.assertEqual(len(summary.market_facts), 2)
            self.assertFalse(summary.has_enough_history)
            self.assertIn("不得判断市场阶段", output_path.read_text(encoding="utf-8"))

    def test_history_builds_index_range_facts_after_enough_market_days(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            for offset, trade_date in enumerate(
                ("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10")
            ):
                self.write_snapshot(root, trade_date, close=100 + offset, change_percent=1, total_amount=10 + offset)

            summary = summarize_market_history("2026-07-06", "2026-07-10", root)

            self.assertTrue(summary.has_enough_history)
            self.assertEqual(len(summary.index_range_facts), 1)
            self.assertEqual(summary.index_range_facts[0].name, "上证指数")
            self.assertEqual(summary.index_range_facts[0].cumulative_change_percent, 4.0)
            self.assertFalse(summary.has_verified_daily_sequence)

    def test_sparse_snapshots_are_labeled_as_samples_not_contiguous_days(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            dates = ("2026-07-21", "2026-07-22", "2026-07-23", "2026-07-28", "2026-08-07")
            for offset, trade_date in enumerate(dates):
                self.write_snapshot(root, trade_date, close=100 + offset, change_percent=1, total_amount=10)

            output_path, summary = create_market_history_report(
                dates[0],
                dates[-1],
                snapshot_dir=root,
                output_dir=root / "reports",
                log_path=root / "review.log",
            )
            content = output_path.read_text(encoding="utf-8")

            self.assertFalse(summary.has_verified_daily_sequence)
            self.assertIn("5 个本地有效快照样本", content)
            self.assertIn("连续性未验证", content)
            self.assertNotIn("样本：5 日", content)

    def test_history_rejects_reversed_date_range(self):
        with TemporaryDirectory() as temp_path:
            with self.assertRaisesRegex(EvidenceSnapshotError, "开始日期不能晚于结束日期"):
                summarize_market_history("2026-07-10", "2026-07-06", Path(temp_path))

    @staticmethod
    def write_snapshot(root: Path, trade_date: str, close: float, change_percent: float, total_amount: float) -> None:
        payload = {
            "trade_date": trade_date,
            "source": "manual",
            "sample_date": trade_date,
            "market": {
                "indices": [{"name": "上证指数", "close": close, "change_percent": change_percent}],
                "total_amount": total_amount,
                "total_amount_source": "test",
            },
            "sentiment": {},
            "sectors": [],
            "stocks": [],
            "events": [],
        }
        (root / f"{trade_date}_snapshot.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
