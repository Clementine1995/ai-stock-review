from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.pools.manage_pool_item import add_pool_item
from stock_review.review_documents.create_brief_review import create_brief_review_report


class BriefReviewTest(unittest.TestCase):
    def test_brief_review_keeps_system_candidates_out_of_decision_area(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            self.write_snapshot(root, "2026-07-28")
            self.write_candidate_history(root, "2026-07-28")
            add_pool_item(
                "hot",
                "002829",
                "星网宇达",
                "2026-07-28",
                "人工确认",
                sectors=("航天装备",),
                database_path=database_path,
            )

            output_path = create_brief_review_report(
                "2026-07-28",
                root,
                database_path=database_path,
                output_dir=root / "reports",
                log_path=root / "stock_review.log",
            )
            content = output_path.read_text(encoding="utf-8")
            decision_area, appendix = content.split("## 附录：未确认系统候选", maxsplit=1)

            self.assertIn("002829 星网宇达", decision_area)
            self.assertNotIn("603580 艾艾精工", decision_area)
            self.assertIn("603580 艾艾精工", appendix)
            self.assertIn("另有 1 个对象缺少可确认的六位股票代码", appendix)
            self.assertIn("未经用户确认，不生成买卖方向、仓位或正式计划", content)
            self.assertNotIn("条件买入", content)
            self.assertTrue((root / "stock_review.log").exists())

    def test_brief_review_is_created_when_standard_evidence_is_missing(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            payload = {
                "trade_date": "2026-07-29",
                "sample_date": "2026-07-29",
                "source": "manual",
                "market": {},
                "sentiment": {},
                "sectors": [],
                "stocks": [],
                "events": [],
            }
            (root / "2026-07-29_snapshot.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

            output_path = create_brief_review_report(
                "2026-07-29",
                root,
                database_path=root / "stock_review.sqlite",
                output_dir=root / "reports",
                log_path=root / "stock_review.log",
            )
            content = output_path.read_text(encoding="utf-8")

            self.assertIn("缺少指数事实", content)
            self.assertIn("缺少成交额事实", content)
            self.assertIn("当前没有有效真实池对象", content)
            self.assertIn("允许明确填写“不参与”", content)

    @staticmethod
    def write_snapshot(root: Path, trade_date: str) -> None:
        payload = {
            "trade_date": trade_date,
            "sample_date": trade_date,
            "source": "manual",
            "market": {
                "indices": [
                    {"name": "上证指数", "close": 3813.31, "change_percent": -1.16}
                ],
                "total_amount": 1450000000000,
            },
            "sentiment": {
                "limit_up_count": 61,
                "limit_down_count": 49,
                "broken_board_rate": 0.2824,
                "highest_board": 6,
                "profit_effect_percent": 49.3,
            },
            "sectors": [
                {"name": "电网设备", "rank": 1, "limit_up_count": 8, "source_type": "manual"}
            ],
            "stocks": [
                {"name": "板块领涨对象", "sector": "电网设备", "role": "板块领涨事实候选"},
                {"code": "002829", "name": "星网宇达", "sector": "航天装备", "role": "用户池事实"},
                {"code": "603580", "name": "艾艾精工", "sector": "轻工制造", "role": "连板事实候选"},
            ],
            "field_sources": {
                "market": "manual",
                "sentiment": "manual",
                "sectors": "manual",
                "stocks": "manual",
            },
            "events": [],
        }
        (root / f"{trade_date}_snapshot.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def write_candidate_history(root: Path, trade_date: str) -> None:
        payload = {
            "trade_date": trade_date,
            "sample_date": trade_date,
            "records": [
                {"code": "002829", "name": "星网宇达", "return_5d_percent": -6.3, "return_20d_percent": 15.2},
                {"code": "603580", "name": "艾艾精工", "return_5d_percent": 8.5, "return_20d_percent": 12.1},
            ],
        }
        (root / f"{trade_date}_review_candidate_history.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
