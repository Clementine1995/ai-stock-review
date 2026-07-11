from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.review_documents.create_daily_review import create_daily_review


class ReviewDocumentTest(unittest.TestCase):
    def test_daily_review_contains_all_framework_steps(self):
        with TemporaryDirectory() as temp_path:
            output_dir = Path(temp_path) / "daily"
            log_path = Path(temp_path) / "logs" / "stock_review.log"
            output_path = create_daily_review(
                "2026-07-06",
                Path("stock-review.md"),
                output_dir=output_dir,
                log_path=log_path,
            )

            content = output_path.read_text(encoding="utf-8")

            self.assertEqual(output_path, output_dir / "2026-07-06_review.md")
            for number in range(1, 11):
                self.assertIn(f"## STEP {number}:", content)
            self.assertEqual(content.count("### 原始规则"), 10)
            self.assertEqual(content.count("### 自动证据"), 10)
            self.assertEqual(content.count("### 人工判断"), 10)
            self.assertEqual(content.count("### 待验证假设"), 10)
            self.assertEqual(content.count("### 风险缺口"), 10)
            self.assertIn("missing_evidence_snapshot", content)

    def test_daily_review_renders_snapshot_evidence_when_provided(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            evidence_path = root / "sample_snapshot.json"
            evidence_path.write_text(
                Path("data/evidence/2026-07-06_sample.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            output_dir = root / "daily"
            log_path = root / "logs" / "stock_review.log"
            output_path = create_daily_review(
                "2026-07-06",
                Path("stock-review.md"),
                evidence_path=evidence_path,
                output_dir=output_dir,
                log_path=log_path,
            )

            content = output_path.read_text(encoding="utf-8")

            self.assertIn("来源：manual_sample", content)
            self.assertIn("样本日期：2026-07-06", content)
            self.assertIn("指数：上证指数", content)
            self.assertIn("两市成交额：860000000000", content)
            self.assertIn("涨停数：58", content)
            self.assertIn("情绪温度：62", content)
            self.assertIn("板块：机器人", content)
            self.assertIn("涨跌幅：3.8%", content)
            self.assertIn("成交额：120000000000", content)
            self.assertIn("核心票：300024、002747", content)
            self.assertIn("个股：000001 平安银行", content)
            self.assertIn("角色来源：待确认", content)
            self.assertIn("来源：待确认", content)
            self.assertIn("原因：样例关注票，仅用于验证个股证据渲染。", content)
            self.assertIn("暂无当前 STEP 直接相关的证据缺口。", content)

    def test_daily_review_places_missing_fields_on_related_steps(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            evidence_path = root / "missing_snapshot.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "source": "manual_missing",
                        "sample_date": "2026-07-06",
                        "market": {},
                        "sentiment": {},
                        "sectors": [],
                        "stocks": [],
                        "events": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            output_path = create_daily_review(
                "2026-07-06",
                Path("stock-review.md"),
                evidence_path=evidence_path,
                output_dir=root / "daily",
                log_path=root / "logs" / "stock_review.log",
            )

            content = output_path.read_text(encoding="utf-8")

            self.assertIn("- missing_indices：当前 STEP 需要对应证据，请人工补齐或确认。", content)
            self.assertIn("- missing_total_amount：当前 STEP 需要对应证据，请人工补齐或确认。", content)
            self.assertEqual(content.count("- missing_sentiment：当前 STEP 需要对应证据，请人工补齐或确认。"), 2)
            self.assertEqual(content.count("- missing_sectors：当前 STEP 需要对应证据，请人工补齐或确认。"), 2)
            self.assertEqual(content.count("- missing_stocks：当前 STEP 需要对应证据，请人工补齐或确认。"), 2)


if __name__ == "__main__":
    unittest.main()
