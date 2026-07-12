from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.summarize_hhxg_history import (
    create_hhxg_history_report,
    summarize_hhxg_history,
)


class HhxgHistoryTest(unittest.TestCase):
    def test_history_reports_facts_but_blocks_analysis_before_five_days(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-06", "军工")
            self.write_snapshot(root, "2026-07-07", "军工")

            output_path, summary = create_hhxg_history_report(
                "2026-07-06", "2026-07-07", root, root / "reports", root / "logs" / "stock_review.log"
            )

            self.assertEqual(len(summary.daily_facts), 2)
            self.assertFalse(summary.has_enough_history)
            self.assertIn("不得输出多日历史分析结论", output_path.read_text(encoding="utf-8"))

    def test_history_reports_repeated_rank_facts_after_five_days(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            for trade_date in ("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"):
                self.write_snapshot(root, trade_date, "军工")

            summary = summarize_hhxg_history("2026-07-06", "2026-07-10", root)

            self.assertTrue(summary.has_enough_history)
            self.assertEqual(len(summary.repeated_rank_facts), 1)
            self.assertEqual(summary.repeated_rank_facts[0].name, "军工")
            self.assertEqual(summary.repeated_rank_facts[0].occurrence_count, 5)

    @staticmethod
    def write_snapshot(root: Path, trade_date: str, theme_name: str) -> None:
        payload = {
            "trade_date": trade_date,
            "source": "hhxg",
            "sample_date": trade_date,
            "market": {},
            "sentiment": {"limit_up_count": 10, "limit_down_count": 1, "broken_board_rate": 0.1, "highest_board": 3},
            "sectors": [{"name": theme_name, "source_type": "hhxg_hot_theme", "rank": 1, "net_inflow_yi": 2}],
            "stocks": [],
            "field_sources": {"sentiment": "hhxg", "sectors": "hhxg"},
            "events": [{"title": "hhxg 最近交易日快照", "source": "hhxg"}],
        }
        (root / f"{trade_date}_snapshot.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
