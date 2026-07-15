from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.scoring.score_market_state import create_scoring_report
from stock_review.scoring.verify_step9_patterns import verify_step9_patterns


class Step9PatternTest(unittest.TestCase):
    def test_all_current_step_nine_patterns_keep_missing_intraday_evidence_unavailable(self):
        snapshot = build_evidence_snapshot(
            "2026-07-15",
            {
                "trade_date": "2026-07-15",
                "sample_date": "2026-07-15",
                "market": {"indices": [{"name": "上证指数", "close": 3955.58}]},
                "sentiment": {"limit_up_count": 72},
                "sectors": [{"name": "创新药"}],
                "stocks": [{"code": "002829", "name": "星网宇达"}],
            },
        )

        checks = verify_step9_patterns(snapshot)

        self.assertEqual(len(checks), 12)
        self.assertTrue(all(check.status == "unavailable" for check in checks))
        self.assertIn("intraday", checks[0].missing_evidence)
        self.assertIn("regulatory_event", checks[8].missing_evidence)

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
