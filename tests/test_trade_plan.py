from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_review.planning.build_trade_plan import TradePlanError, create_trade_plan
from stock_review.pools.manage_pool_item import add_pool_item


class TradePlanTest(unittest.TestCase):
    def test_trade_plan_contains_pool_items_and_condition_sections(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            review_path = root / "2026-07-06_review.md"
            review_path.write_text("# 2026-07-06 每日复盘\n", encoding="utf-8")
            evidence_path = root / "sample_snapshot.json"
            evidence_path.write_text(
                Path("data/evidence/2026-07-06_sample.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            database_path = root / "stock_review.sqlite"
            add_pool_item(
                "watch",
                code="000001",
                name="平安银行",
                start_date="2026-07-06",
                reason="样例关注",
                exchange="SZSE",
                sector="银行",
                database_path=database_path,
            )
            add_pool_item(
                "hot",
                code="600519",
                name="贵州茅台",
                start_date="2026-07-06",
                reason="系统验收样例",
                record_kind="sample",
                database_path=database_path,
            )

            output_path = create_trade_plan(
                "2026-07-06",
                review_path=review_path,
                evidence_path=evidence_path,
                database_path=database_path,
                output_dir=root,
                log_path=root / "logs" / "stock_review.log",
            )

            content = output_path.read_text(encoding="utf-8")

            self.assertEqual(output_path, root / "2026-07-06_plan.md")
            self.assertIn("计划边界：本文件只记录观察条件和应对框架，不是买卖指令。", content)
            self.assertIn("计划项 1: 000001 平安银行", content)
            self.assertNotIn("贵州茅台", content)
            self.assertIn("证据来源：manual_sample / 2026-07-06", content)
            self.assertIn("#### 符合预期", content)
            self.assertIn("#### 超预期", content)
            self.assertIn("#### 不及预期", content)
            self.assertIn("#### 放弃条件", content)

    def test_trade_plan_reports_empty_pool_without_failing(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            review_path = root / "2026-07-06_review.md"
            review_path.write_text("# 2026-07-06 每日复盘\n", encoding="utf-8")

            output_path = create_trade_plan(
                "2026-07-06",
                review_path=review_path,
                database_path=root / "stock_review.sqlite",
                output_dir=root,
                log_path=root / "logs" / "stock_review.log",
            )

            content = output_path.read_text(encoding="utf-8")

            self.assertIn("暂无池子记录", content)
            self.assertIn("未接入 Evidence Snapshot", content)

    def test_trade_plan_requires_existing_review_document(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)

            with self.assertRaisesRegex(TradePlanError, "未找到每日复盘文档"):
                create_trade_plan(
                    "2026-07-06",
                    review_path=root / "missing_review.md",
                    database_path=root / "stock_review.sqlite",
                    output_dir=root,
                    log_path=root / "logs" / "stock_review.log",
                )


if __name__ == "__main__":
    unittest.main()
