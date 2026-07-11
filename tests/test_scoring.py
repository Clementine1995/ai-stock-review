from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import read_json_mapping
from stock_review.scoring.score_market_state import (
    create_scoring_report,
    score_market_state,
    score_sector_strength,
)
from stock_review.scoring.match_trade_patterns import match_stock_patterns


class ScoringTest(unittest.TestCase):
    def test_market_score_is_stable_and_explains_each_component(self):
        snapshot = build_evidence_snapshot(
            "2026-07-06",
            read_json_mapping(Path("data/evidence/2026-07-06_sample.json")),
        )

        first_result = score_market_state(snapshot)
        second_result = score_market_state(snapshot)

        self.assertEqual(first_result, second_result)
        self.assertEqual(first_result.score, 77.06)
        self.assertEqual(first_result.label, "偏强")
        self.assertEqual(len(first_result.evidence_lines), 4)

    def test_market_score_is_unavailable_when_core_evidence_is_missing(self):
        snapshot = build_evidence_snapshot(
            "2026-07-06",
            {
                "market": {"indices": []},
                "sentiment": {},
                "sectors": [],
                "stocks": [],
            },
        )

        result = score_market_state(snapshot)

        self.assertIsNone(result.score)
        self.assertEqual(result.label, "不可判定")
        self.assertIn("market.indices", result.missing_fields)
        self.assertIn("sentiment.limit_up_count", result.missing_fields)

    def test_sector_score_reports_coverage_and_missing_fields(self):
        result = score_sector_strength(
            {
                "name": "机器人",
                "change_percent": 3.8,
            }
        )

        self.assertEqual(result.score, 100)
        self.assertEqual(result.coverage, 40)
        self.assertEqual(result.label, "不可判定")
        self.assertIn("up_count/down_count", result.missing_fields)

    def test_scoring_report_contains_traceable_rules_and_boundary(self):
        with TemporaryDirectory() as temp_path:
            output_path = create_scoring_report(
                "2026-07-06",
                Path("data/evidence/2026-07-06_sample.json"),
                Path(temp_path),
            )
            content = output_path.read_text(encoding="utf-8")

            self.assertIn("市场状态", content)
            self.assertIn("指数正收益占比", content)
            self.assertIn("板块强度", content)
            self.assertIn("证据覆盖率", content)
            self.assertIn("不构成买卖建议", content)

    def test_stock_role_tags_and_pattern_matches_use_traceable_sources(self):
        snapshot = build_evidence_snapshot(
            "2026-07-06",
            {
                "sentiment": {"highest_board": 3},
                "sectors": [
                    {
                        "name": "机器人",
                        "change_percent": 3.0,
                        "up_count": 8,
                        "down_count": 2,
                        "net_inflow": 1.2,
                        "leading_stock_change_percent": 10.0,
                    }
                ],
                "stocks": [
                    {
                        "code": "300001",
                        "name": "样例股份",
                        "exchange": "SZSE",
                        "sector": "机器人",
                        "source": "akshare:sector_leading_stock",
                        "role_source": "industry_ths 板块领涨股",
                        "change_percent": 10.0,
                    },
                    {
                        "code": "600001",
                        "name": "连板样例",
                        "exchange": "SSE",
                        "sector": "机器人",
                        "source": "akshare:stock_zt_pool_em",
                        "role_source": "涨停池连板股（3板）",
                        "change_percent": 10.0,
                    },
                ],
            },
        )

        results = match_stock_patterns(snapshot)

        self.assertIn("板块领涨", results[0].role_tags)
        self.assertIn("板块领涨疑似观察模式", results[0].pattern_matches)
        self.assertIn("连板股", results[1].role_tags)
        self.assertIn("连板高度事实候选", results[1].role_tags)
        self.assertIn("连板接力疑似观察模式", results[1].pattern_matches)
        self.assertIn("intraday.evidence", results[1].missing_fields)

    def test_scoring_report_contains_stock_pattern_boundary(self):
        with TemporaryDirectory() as temp_path:
            output_path = create_scoring_report(
                "2026-07-06",
                Path("data/evidence/2026-07-06_snapshot.json"),
                Path(temp_path),
            )
            content = output_path.read_text(encoding="utf-8")

            self.assertIn("个股角色与疑似观察模式", content)
            self.assertIn("不是确认买点", content)
            self.assertIn("竞价", content)


if __name__ == "__main__":
    unittest.main()
