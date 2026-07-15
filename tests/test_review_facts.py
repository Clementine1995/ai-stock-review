from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.manage_evidence_snapshot import EvidenceSnapshotError
from stock_review.evidence.summarize_review_facts import build_step_evidence, create_review_facts_report, summarize_review_facts


class ReviewFactsTest(unittest.TestCase):
    def test_report_renders_sources_facts_and_non_deterministic_boundaries(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-13")
            self.write_pool_history(root, "2026-07-13")

            output_path, summary = create_review_facts_report(
                "2026-07-13", snapshot_dir=root, pool_history_dir=root, output_dir=root / "reports"
            )
            content = output_path.read_text(encoding="utf-8")

            self.assertEqual(summary.field_sources["market"], "akshare")
            self.assertEqual(len(summary.pool_history_records), 1)
            self.assertIn("市场字段来源：akshare", content)
            self.assertIn("002829 星网宇达：5 日 4.2%，20 日 12.5%", content)
            self.assertIn("炸板率比例 0.3", content)
            self.assertIn("本地允许事实字段：指数事实、成交额事实", content)
            self.assertIn("真实池 5 日涨跌幅", content)
            self.assertIn("归纳结果：不可判定", content)
            self.assertIn("不能认定核心票", content)
            self.assertIn("不自动加入、暂停、移出或修改任何池子", content)

    def test_summary_keeps_missing_pool_history_as_gap(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-13")

            summary = summarize_review_facts("2026-07-13", root, root)

            self.assertTrue(summary.pool_history_missing)
            self.assertEqual(summary.pool_history_records, ())

    def test_step_mapping_limits_pool_history_to_step_six_and_step_seven(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-13")
            self.write_pool_history(root, "2026-07-13")

            step_evidence = build_step_evidence(summarize_review_facts("2026-07-13", root, root))

            self.assertNotIn("pool_history.return_5d_percent", step_evidence[5].allowed_field_keys)
            self.assertIn("pool_history.return_5d_percent", step_evidence[6].allowed_field_keys)
            self.assertIn("pool_history.return_20d_percent", step_evidence[7].allowed_field_keys)
            self.assertIn("missing_stock_identity", step_evidence[6].hard_missing_fields)

    def test_summary_rejects_snapshot_with_different_sample_date(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-13", sample_date="2026-07-10")

            with self.assertRaisesRegex(EvidenceSnapshotError, "样本日期与交易日期不一致"):
                summarize_review_facts("2026-07-13", root, root)

    @staticmethod
    def write_snapshot(root: Path, trade_date: str, sample_date: str | None = None) -> None:
        payload = {
            "trade_date": trade_date,
            "sample_date": sample_date or trade_date,
            "source": "akshare",
            "market": {"indices": [{"name": "上证指数", "close": 3900, "change_percent": -1.2}], "total_amount": 10},
            "sentiment": {"limit_up_count": 90, "limit_down_count": 4, "broken_board_rate": 0.3, "highest_board": 2},
            "sectors": [{"name": "军民融合", "rank": 1, "limit_up_count": 15, "source_type": "hhxg_hot_theme"}],
            "stocks": [{"code": "002829", "name": "星网宇达", "role": "个股事实候选", "role_source": "测试来源"}],
            "field_sources": {"market": "akshare", "sentiment": "hhxg", "sectors": "hhxg", "stocks": "akshare"},
            "events": [],
        }
        (root / f"{trade_date}_snapshot.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def write_pool_history(root: Path, trade_date: str) -> None:
        payload = {
            "trade_date": trade_date,
            "sample_date": trade_date,
            "records": [{"code": "002829", "name": "星网宇达", "return_5d_percent": 4.2, "return_20d_percent": 12.5, "missing_fields": []}],
        }
        (root / f"{trade_date}_real_pool_stock_history.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
