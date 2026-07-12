from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.manage_evidence_snapshot import EvidenceSnapshotError
from stock_review.evidence.summarize_sector_history import (
    create_sector_history_report,
    summarize_sector_history,
)


class SectorHistoryTest(unittest.TestCase):
    def test_history_reports_daily_strongest_and_insufficient_history(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(
                root,
                "2026-07-06",
                [
                    {"name": "机器人", "change_percent": 2.5, "source_type": "concept", "leading_stock": "甲"},
                    {"name": "算力", "change_percent": 4.0, "source_type": "concept", "leading_stock": "乙"},
                ],
            )
            self.write_snapshot(root, "2026-07-07", [])

            output_path, summary = create_sector_history_report(
                "2026-07-06",
                "2026-07-07",
                snapshot_dir=root,
                output_dir=root / "reports",
                log_path=root / "logs" / "stock_review.log",
            )
            content = output_path.read_text(encoding="utf-8")

            self.assertEqual(summary.daily_strongest[0].name, "算力")
            self.assertEqual(summary.missing_sector_dates, ("2026-07-07",))
            self.assertIn("数据不足", content)
            self.assertIn("不得认定近期核心板块", content)

    def test_history_builds_repeated_candidates_from_enough_sector_days(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            for index, trade_date in enumerate(
                ("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10")
            ):
                sectors = [
                    {"name": "机器人", "change_percent": 1.0 + index},
                    {"name": f"单日板块{index}", "change_percent": 0.5},
                ]
                self.write_snapshot(root, trade_date, sectors)

            summary = summarize_sector_history("2026-07-06", "2026-07-10", root)

            self.assertTrue(summary.has_enough_history)
            self.assertEqual(len(summary.repeated_candidates), 1)
            self.assertEqual(summary.repeated_candidates[0].name, "机器人")
            self.assertEqual(summary.repeated_candidates[0].occurrence_count, 5)
            self.assertEqual(summary.repeated_candidates[0].positive_count, 5)
            self.assertEqual(summary.repeated_candidates[0].cumulative_change_percent, 15.0)

    def test_history_rejects_reversed_date_range(self):
        with TemporaryDirectory() as temp_path:
            with self.assertRaisesRegex(EvidenceSnapshotError, "开始日期不能晚于结束日期"):
                summarize_sector_history("2026-07-10", "2026-07-06", Path(temp_path))

    def test_history_rejects_invalid_date_format(self):
        with TemporaryDirectory() as temp_path:
            with self.assertRaisesRegex(EvidenceSnapshotError, "日期格式必须为 YYYY-MM-DD"):
                summarize_sector_history("invalid", "2026-07-06", Path(temp_path))

    @staticmethod
    def write_snapshot(root: Path, trade_date: str, sectors: list[dict[str, object]]) -> None:
        payload = {
            "trade_date": trade_date,
            "source": "manual",
            "sample_date": trade_date,
            "market": {},
            "sentiment": {},
            "sectors": sectors,
            "stocks": [],
            "events": [],
        }
        (root / f"{trade_date}_snapshot.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
