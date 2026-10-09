# 本文件验证通达信个股身份导入、连续比较算法与正式 CLI 写入边界。

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_review.cli import main
from stock_review.evidence.enrich_stock_facts import (
    StockFactEnrichmentError,
    compute_continuous_comparison,
    load_stock_identity_input,
)
from stock_review.evidence.manage_evidence_snapshot import (
    EvidenceSnapshotError,
    import_tongdaxin_stock_facts,
)


TRADE_DATE = "2026-08-17"


class StockFactEnrichmentTest(unittest.TestCase):
    def test_comparison_counts_terminal_rises_from_latest_bar(self):
        bars = build_daily_bars([10, 9, 10, 11, 12, 13])
        bars.reverse()

        comparison = compute_continuous_comparison(bars)

        self.assertEqual(comparison["consecutive_up_days"], 4)
        self.assertEqual(comparison["recent_up_ratio"], 0.8)
        self.assertEqual(comparison["direction"], "上行延续")

    def test_recent_up_ratio_is_independent_of_terminal_streak(self):
        comparison = compute_continuous_comparison(
            build_daily_bars([10, 11, 12, 13, 14, 13])
        )

        self.assertEqual(comparison["consecutive_up_days"], 0)
        self.assertEqual(comparison["recent_up_ratio"], 0.8)
        self.assertEqual(comparison["direction"], "上行延续")

    def test_identity_input_accepts_only_the_fixed_envelope_and_identity_contract(self):
        with TemporaryDirectory() as temp_path:
            input_path = Path(temp_path) / "identity.json"
            write_json(input_path, build_identity_input())

            mapping = load_stock_identity_input(input_path, TRADE_DATE)

            self.assertEqual(
                mapping["目标股"],
                {"code": "000002", "setcode": 0, "exchange": "SZ"},
            )

            scenarios = []
            wrong_trade_date = build_identity_input()
            wrong_trade_date["trade_date"] = "2026-08-14"
            scenarios.append((wrong_trade_date, "trade_date 必须为"))

            wrong_source = build_identity_input()
            wrong_source["source"] = "manual"
            scenarios.append((wrong_source, "source 必须为"))

            wrong_scope = build_identity_input()
            wrong_scope["scope"] = "all_market"
            scenarios.append((wrong_scope, "scope 必须为"))

            wrong_code = build_identity_input()
            wrong_code["identities"][0]["code"] = "00002"
            scenarios.append((wrong_code, "code 必须是六位数字字符串"))

            wrong_setcode = build_identity_input()
            wrong_setcode["identities"][0]["setcode"] = 2
            scenarios.append((wrong_setcode, "setcode 必须是整数 0 或 1"))

            string_setcode = build_identity_input()
            string_setcode["identities"][0]["setcode"] = "0"
            scenarios.append((string_setcode, "setcode 必须是整数 0 或 1"))

            duplicate_name = build_identity_input()
            duplicate_name["identities"].append(
                {"name": "目 标 股", "code": "600001", "setcode": 1}
            )
            scenarios.append((duplicate_name, "重复规范化名称：目标股"))

            for payload, expected_message in scenarios:
                with self.subTest(expected_message=expected_message):
                    write_json(input_path, payload)
                    with self.assertRaisesRegex(
                        StockFactEnrichmentError,
                        expected_message,
                    ):
                        load_stock_identity_input(input_path, TRADE_DATE)

    def test_stock_fact_import_preserves_existing_profit_effect(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            snapshot_path = root / f"{TRADE_DATE}_snapshot.json"
            input_path = root / "identity.json"
            database_path = root / "stock_review.sqlite"
            write_json(snapshot_path, build_snapshot())
            write_json(input_path, build_identity_input())

            import_tongdaxin_stock_facts(
                TRADE_DATE,
                input_path,
                confirmed=True,
                output_dir=root,
                database_path=database_path,
            )

            sentiment = json.loads(snapshot_path.read_text(encoding="utf-8"))["sentiment"]
            self.assertEqual(sentiment["profit_effect_percent"], 37.5)
            self.assertEqual(sentiment["profit_effect_source"], "hhxg:market.sentiment_index")
            self.assertEqual(
                sentiment["profit_effect_calculation"],
                "source_provided_formula_unverified",
            )

    def test_stock_fact_import_rejects_mismatched_comparison_dates(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            snapshot_path = root / f"{TRADE_DATE}_snapshot.json"
            input_path = root / "identity.json"
            database_path = root / "stock_review.sqlite"
            candidate_path = root / f"{TRADE_DATE}_review_candidate_history.json"
            index_path = root / f"{TRADE_DATE}_tdx_index_history.json"
            write_json(snapshot_path, build_snapshot())
            write_json(input_path, build_identity_input())
            candidate_history = build_candidate_history()
            candidate_history["trade_date"] = "2026-08-14"
            write_json(candidate_path, candidate_history)
            write_json(index_path, build_index_history())
            snapshot_before = snapshot_path.read_bytes()

            with self.assertRaisesRegex(
                EvidenceSnapshotError,
                "候选历史交易日期不是目标交易日",
            ):
                import_tongdaxin_stock_facts(
                    TRADE_DATE,
                    input_path,
                    confirmed=True,
                    output_dir=root,
                    database_path=database_path,
                )

            self.assertEqual(snapshot_path.read_bytes(), snapshot_before)
            self.assertFalse(database_path.exists())

    def test_cli_imports_stock_facts_and_writes_audit_log(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            snapshot_path = root / f"{TRADE_DATE}_snapshot.json"
            input_path = root / "identity.json"
            database_path = root / "stock_review.sqlite"
            log_path = root / "stock_review.log"
            write_json(snapshot_path, build_snapshot())
            write_json(input_path, build_identity_input())
            write_json(
                root / f"{TRADE_DATE}_review_candidate_history.json",
                build_candidate_history(),
            )
            write_json(
                root / f"{TRADE_DATE}_tdx_index_history.json",
                build_index_history(),
            )

            with patch("builtins.print"):
                exit_code = main(
                    [
                        "evidence",
                        "import-tdx-stock-facts",
                        "--date",
                        TRADE_DATE,
                        "--file",
                        str(input_path),
                        "--output-dir",
                        str(root),
                        "--database",
                        str(database_path),
                        "--log-path",
                        str(log_path),
                        "--confirmed",
                    ]
                )

            self.assertEqual(exit_code, 0)
            saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
            stock = saved["stocks"][0]
            self.assertTrue(stock["identity_verified"])
            self.assertEqual(stock["code"], "000002")
            self.assertEqual(stock["continuous_comparison"]["consecutive_up_days"], 4)
            self.assertEqual(stock["continuous_comparison"]["recent_up_ratio"], 0.8)
            self.assertTrue(database_path.exists())
            self.assertIn(
                "command=evidence import-tdx-stock-facts",
                log_path.read_text(encoding="utf-8"),
            )

    def test_cli_requires_confirmation_before_writing_stock_facts(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            snapshot_path = root / f"{TRADE_DATE}_snapshot.json"
            input_path = root / "identity.json"
            database_path = root / "stock_review.sqlite"
            log_path = root / "stock_review.log"
            write_json(snapshot_path, build_snapshot())
            write_json(input_path, build_identity_input())
            snapshot_before = snapshot_path.read_bytes()

            with patch("builtins.print"):
                exit_code = main(
                    [
                        "evidence",
                        "import-tdx-stock-facts",
                        "--date",
                        TRADE_DATE,
                        "--file",
                        str(input_path),
                        "--output-dir",
                        str(root),
                        "--database",
                        str(database_path),
                        "--log-path",
                        str(log_path),
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertEqual(snapshot_path.read_bytes(), snapshot_before)
            self.assertFalse(database_path.exists())
            self.assertFalse(log_path.exists())


def build_identity_input() -> dict:
    return {
        "trade_date": TRADE_DATE,
        "source": "workbuddy:tongdaxin.tdx_lookup_stock",
        "scope": "snapshot_stock_identity",
        "identities": [
            {"name": "目标股", "code": "000002", "setcode": 0},
        ],
    }


def build_snapshot() -> dict:
    return {
        "trade_date": TRADE_DATE,
        "source": "akshare+hhxg",
        "sample_date": TRADE_DATE,
        "market": {"indices": [{"name": "上证指数", "close": 100}], "total_amount": 1},
        "sentiment": {
            "limit_up_count": 80,
            "limit_down_count": 20,
            "broken_board_rate": 12.5,
            "highest_board": 5,
            "profit_effect_percent": 37.5,
            "profit_effect_source": "hhxg:market.sentiment_index",
            "profit_effect_calculation": "source_provided_formula_unverified",
        },
        "sectors": [],
        "stocks": [{"name": "目标股", "role": "有限候选"}],
        "field_sources": {"sentiment": "hhxg", "stocks": "akshare"},
        "events": [],
    }


def build_daily_bars(closes: list[float]) -> list[dict]:
    dates = (
        "2026-08-10",
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
        "2026-08-14",
        "2026-08-17",
    )
    return [
        {"date": date, "close": close}
        for date, close in zip(dates, closes)
    ]


def build_candidate_history() -> dict:
    return {
        "trade_date": TRADE_DATE,
        "records": [
            {
                "name": "目标股",
                "code": "000002",
                "daily_bars": build_daily_bars([10, 9, 10, 11, 12, 13]),
            }
        ],
    }


def build_index_history() -> dict:
    return {
        "trade_date": TRADE_DATE,
        "records": [
            {
                "sample_date": bar["date"],
                "indices": [{"code": "000001", "close": 100 + position}],
            }
            for position, bar in enumerate(build_daily_bars([10, 9, 10, 11, 12, 13]))
        ],
    }


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
