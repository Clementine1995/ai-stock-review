from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.scoring.score_market_state import create_scoring_report
from stock_review.scoring.verify_step9_patterns import verify_step9_patterns


class Step9PatternTest(unittest.TestCase):
    def test_step_nine_separates_background_match_from_pending_intraday_trigger(self):
        snapshot = build_evidence_snapshot(
            "2026-07-15",
            {
                "trade_date": "2026-07-15",
                "sample_date": "2026-07-15",
                "market": {"indices": [{"name": "上证指数", "close": 3955.58}]},
                "sentiment": {"limit_up_count": 20, "limit_down_count": 50},
                "sectors": [{"name": "创新药"}],
                "stocks": [{"code": "002829", "name": "星网宇达"}],
            },
        )

        checks = verify_step9_patterns(snapshot)

        self.assertEqual(len(checks), 12)
        self.assertEqual(checks[0].status, "not_matched")
        self.assertEqual(checks[1].status, "background_matched_trigger_pending")
        self.assertEqual(checks[5].status, "unavailable")
        self.assertEqual(checks[0].candidate_codes, ("002829",))
        self.assertIn("intraday", checks[0].missing_evidence)
        self.assertIn("regulatory_event", checks[8].missing_evidence)

    def test_candidate_and_sector_backgrounds_bind_priority_patterns_before_triggers(self):
        snapshot = build_evidence_snapshot(
            "2026-07-15",
            {
                "trade_date": "2026-07-15",
                "sample_date": "2026-07-15",
                "market": {
                    "indices": [
                        {
                            "name": "上证指数",
                            "close": 3955.58,
                            "change_percent": -0.29,
                        }
                    ]
                },
                "sentiment": {"limit_up_count": 20, "limit_down_count": 50},
                "sectors": [{"name": "创新药"}],
                "stocks": [
                    {
                        "code": "002829",
                        "name": "星网宇达",
                        "sector": "创新药",
                        "role": "板块领涨事实",
                        "change_percent": 9.98,
                    }
                ],
            },
        )
        candidate_history_records = (
            {
                "code": "002829",
                "return_5d_percent": 12.5,
                "average_5d_amount": 100000000,
                "circulating_market_value": 5000000000,
                "daily_bar_count": 21,
                "daily_bars_complete": True,
                "adjustment": "none",
                "daily_bars": [
                    {
                        "date": (date(2026, 6, 25) + timedelta(days=day - 1)).isoformat(),
                        "open": 10 + day / 10,
                        "high": 10.3 + day / 10,
                        "low": 9.9 + day / 10,
                        "close": 10.2 + day / 10,
                        "volume": 100000 + day,
                        "amount": 1000000 + day,
                        "turnover": 2.0 + day / 100,
                    }
                    for day in range(1, 22)
                ],
            },
        )

        checks = verify_step9_patterns(
            snapshot,
            candidate_history_records,
            ("002829",),
        )

        for pattern_number in (1, 2, 6, 11, 12):
            with self.subTest(pattern_number=pattern_number):
                self.assertEqual(
                    checks[pattern_number - 1].status,
                    "background_matched_trigger_pending",
                )
                self.assertEqual(
                    checks[pattern_number - 1].candidate_codes,
                    ("002829",),
                )
        self.assertEqual(
            checks[6].status,
            "base_evidence_available_trigger_pending",
        )
        for pattern_number in (3, 4, 8, 9, 10):
            with self.subTest(pattern_number=pattern_number):
                self.assertEqual(
                    checks[pattern_number - 1].status,
                    "base_evidence_available_trigger_pending",
                )
        self.assertEqual(checks[4].status, "not_matched")
        self.assertIn("daily_kline", checks[0].available_evidence)
        self.assertIn("call_auction", checks[0].missing_evidence)
        self.assertIn("intraday", checks[0].missing_evidence)

    def test_capacity_return_and_complete_daily_kline_do_not_prove_pattern_seven_background(self):
        snapshot = build_evidence_snapshot(
            "2026-07-15",
            {
                "trade_date": "2026-07-15",
                "sample_date": "2026-07-15",
                "market": {},
                "sentiment": {},
                "sectors": [],
                "stocks": [{"code": "002829", "name": "星网宇达"}],
            },
        )
        history = (
            {
                "code": "002829",
                "return_5d_percent": 12.5,
                "average_5d_amount": 100000000,
                "circulating_market_value": 5000000000,
                "daily_bar_count": 21,
                "daily_bars_complete": True,
                "adjustment": "none",
                "daily_bars": [
                    {
                        "date": (date(2026, 6, 25) + timedelta(days=day - 1)).isoformat(),
                        "open": 10 + day / 10,
                        "high": 10.3 + day / 10,
                        "low": 9.9 + day / 10,
                        "close": 10.2 + day / 10,
                        "volume": 100000 + day,
                        "amount": 1000000 + day,
                        "turnover": 2.0 + day / 100,
                    }
                    for day in range(1, 22)
                ],
            },
        )

        pattern_seven = verify_step9_patterns(snapshot, history, ("002829",))[6]

        self.assertEqual(
            pattern_seven.status,
            "base_evidence_available_trigger_pending",
        )
        self.assertEqual(pattern_seven.candidate_codes, ("002829",))
        self.assertIn("candidate_history", pattern_seven.available_evidence)
        self.assertIn("daily_kline", pattern_seven.available_evidence)
        self.assertIn("intraday", pattern_seven.missing_evidence)
        self.assertIn("next_day_feedback", pattern_seven.missing_evidence)

    def test_incomplete_candidate_bars_do_not_unlock_daily_kline(self):
        snapshot = build_evidence_snapshot(
            "2026-07-15",
            {
                "trade_date": "2026-07-15",
                "sample_date": "2026-07-15",
                "market": {"indices": [{"name": "上证指数", "close": 3955.58}]},
                "sentiment": {"limit_up_count": 30, "limit_down_count": 10},
                "sectors": [{"name": "创新药"}],
                "stocks": [
                    {
                        "code": "300001",
                        "name": "候选甲",
                        "sector": "创新药",
                        "role": "板块领涨事实",
                    }
                ],
            },
        )
        history = (
            {
                "code": "300001",
                "return_5d_percent": 8.0,
                "average_5d_amount": 100000000,
                "circulating_market_value": 5000000000,
                "daily_bar_count": 20,
                "daily_bars_complete": False,
                "adjustment": "none",
                "daily_bars": [
                    {
                        "date": (date(2026, 6, 26) + timedelta(days=day - 1)).isoformat(),
                        "open": 10 + day / 10,
                        "high": 10.3 + day / 10,
                        "low": 9.9 + day / 10,
                        "close": 10.2 + day / 10,
                        "volume": 100000 + day,
                        "amount": 1000000 + day,
                        "turnover": 2.0 + day / 100,
                    }
                    for day in range(1, 21)
                ],
            },
        )

        checks = verify_step9_patterns(snapshot, history, ("300001",))

        self.assertEqual(checks[0].status, "background_matched_trigger_pending")
        self.assertNotIn("daily_kline", checks[0].available_evidence)
        self.assertIn("daily_kline", checks[0].missing_evidence)

    def test_candidate_history_background_cannot_be_borrowed_from_unbound_stock(self):
        snapshot = build_evidence_snapshot(
            "2026-07-15",
            {
                "trade_date": "2026-07-15",
                "sample_date": "2026-07-15",
                "market": {"indices": [{"name": "上证指数", "close": 3955.58}]},
                "sentiment": {"limit_up_count": 20, "limit_down_count": 50},
                "sectors": [{"name": "创新药"}],
                "stocks": [
                    {"code": "300001", "name": "候选甲", "sector": "创新药"},
                    {"code": "300002", "name": "候选乙", "sector": "创新药"},
                ],
            },
        )
        history = (
            {"code": "300001", "return_5d_percent": 8.0},
            {
                "code": "300002",
                "return_5d_percent": 6.0,
                "average_5d_amount": 100000000,
                "circulating_market_value": 5000000000,
            },
        )

        checks = verify_step9_patterns(snapshot, history, ("300001",))

        self.assertEqual(checks[5].status, "unavailable")
        self.assertEqual(checks[5].candidate_codes, ())
        self.assertIn("candidate_history", checks[5].missing_evidence)

    def test_daily_kline_requires_current_complete_ohlcv_amount_and_turnover(self):
        snapshot = build_evidence_snapshot(
            "2026-07-15",
            {
                "trade_date": "2026-07-15",
                "sample_date": "2026-07-15",
                "market": {"indices": [{"name": "上证指数", "close": 3955.58}]},
                "sentiment": {"limit_up_count": 30, "limit_down_count": 10},
                "sectors": [{"name": "创新药"}],
                "stocks": [{"code": "300001", "name": "候选甲", "sector": "创新药"}],
            },
        )
        bars = [
            {
                "date": (date(2026, 6, 25) + timedelta(days=day - 1)).isoformat(),
                "open": 10 + day / 10,
                "high": 10.3 + day / 10,
                "low": 9.9 + day / 10,
                "close": 10.2 + day / 10,
                "volume": 100000 + day,
                "amount": 1000000 + day,
                "turnover": 2.0 + day / 100,
            }
            for day in range(1, 22)
        ]
        bars[-1].pop("turnover")
        history = (
            {
                "code": "300001",
                "return_5d_percent": 8.0,
                "average_5d_amount": 100000000,
                "circulating_market_value": 5000000000,
                "daily_bar_count": 21,
                "daily_bars_complete": True,
                "adjustment": "none",
                "daily_bars": bars,
            },
        )

        checks = verify_step9_patterns(snapshot, history, ("300001",))

        self.assertNotIn("daily_kline", checks[0].available_evidence)
        self.assertIn("daily_kline", checks[0].missing_evidence)

        bars[-1]["turnover"] = 2.21
        bars[-1]["date"] = "2026-07-14"
        stale_checks = verify_step9_patterns(snapshot, history, ("300001",))

        self.assertNotIn("daily_kline", stale_checks[0].available_evidence)
        self.assertIn("daily_kline", stale_checks[0].missing_evidence)

    def test_generic_pattern_is_unavailable_without_required_base_evidence(self):
        snapshot = build_evidence_snapshot(
            "2026-07-15",
            {
                "trade_date": "2026-07-15",
                "sample_date": "2026-07-15",
                "market": {"indices": [{"name": "上证指数", "close": 3955.58}]},
                "sentiment": {"limit_up_count": 20, "limit_down_count": 50},
                "sectors": [],
                "stocks": [],
            },
        )

        checks = verify_step9_patterns(snapshot)

        self.assertEqual(checks[0].status, "unavailable")
        self.assertEqual(checks[7].status, "unavailable")

    def test_scoring_report_renders_each_step_nine_pattern_with_boundary(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            evidence_path = root / "snapshot.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "trade_date": "2026-07-15",
                        "sample_date": "2026-07-15",
                        "market": {"indices": [{"name": "上证指数", "close": 3955.58, "change_percent": -0.29}]},
                        "sentiment": {"limit_up_count": 72, "limit_down_count": 31, "broken_board_rate": 0.3143, "highest_board": 4},
                        "sectors": [{"name": "创新药"}],
                        "stocks": [{"code": "002829", "name": "星网宇达"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            output_path = create_scoring_report("2026-07-15", evidence_path, root / "reports")
            content = output_path.read_text(encoding="utf-8")

            self.assertIn("## STEP 9 模式逐项核验", content)
            self.assertIn("### 模式 1：题材上升初期和发酵阶段的弹性标", content)
            self.assertIn("### 模式 12：情绪指数双低迷尾盘炸板", content)
            self.assertIn("不确认买点、不生成交易指令", content)


if __name__ == "__main__":
    unittest.main()
