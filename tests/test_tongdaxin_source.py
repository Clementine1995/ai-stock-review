from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_review.cli import main
from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import (
    EvidenceSnapshotError,
    import_tongdaxin_index_history,
    import_tongdaxin_market_breadth_snapshot,
    import_tongdaxin_market_snapshot,
)
from stock_review.evidence.tongdaxin_source import (
    TONGDAXIN_INDEX_HISTORY_QUALITY,
    MARKET_BREADTH_QUOTE_QUALITY,
    MARKET_QUOTE_FALLBACK_QUALITY,
    TONGDAXIN_AMOUNT_SOURCE,
    TONGDAXIN_BREADTH_SOURCE,
    TONGDAXIN_SOURCE,
    TongdaxinSourceError,
    build_tongdaxin_index_history_evidence,
    build_tongdaxin_market_breadth_evidence,
    build_tongdaxin_market_evidence,
)
from stock_review.reports.render_markdown import render_market_lines
from stock_review.storage.sqlite_repository import EvidenceSnapshotRepository


class TongdaxinSourceTest(unittest.TestCase):
    def test_fixed_index_queries_build_five_daily_bars_ending_on_target(self):
        evidence = build_tongdaxin_index_history_evidence(
            "2026-08-07",
            build_index_history_payload(include_later_row=True),
        )

        self.assertEqual(evidence["source"], TONGDAXIN_SOURCE)
        self.assertEqual(evidence["quality"], TONGDAXIN_INDEX_HISTORY_QUALITY)
        self.assertEqual(
            evidence["sample_dates"],
            ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"],
        )
        self.assertEqual(len(evidence["records"]), 5)
        self.assertEqual(
            [item["name"] for item in evidence["records"][-1]["indices"]],
            ["上证指数", "深证成指", "创业板指"],
        )
        self.assertEqual(evidence["records"][-1]["indices"][0]["close"], 3940.040039)
        self.assertNotIn("Amount", json.dumps(evidence, ensure_ascii=False))

    def test_index_history_rejects_mismatched_dates_between_indices(self):
        payload = build_index_history_payload()
        payload["queries"][2]["Rows"].pop(-2)

        with self.assertRaisesRegex(TongdaxinSourceError, "日期序列必须完全一致"):
            build_tongdaxin_index_history_evidence("2026-08-07", payload)

    def test_index_history_requires_exact_fixed_queries_and_daily_parameters(self):
        scenarios = []

        missing_query = build_index_history_payload()
        missing_query["queries"].pop()
        scenarios.append((missing_query, "恰好包含 3 个固定代码"))

        duplicate_query = build_index_history_payload()
        duplicate_query["queries"][-1]["code"] = "000001"
        scenarios.append((duplicate_query, "查询代码重复"))

        wrong_setcode = build_index_history_payload()
        wrong_setcode["queries"][0]["setcode"] = 0
        scenarios.append((wrong_setcode, "000001.setcode 必须为 1"))

        wrong_period = build_index_history_payload()
        wrong_period["queries"][0]["period"] = 5
        scenarios.append((wrong_period, "000001.period 必须为 4"))

        wrong_adjustment = build_index_history_payload()
        wrong_adjustment["queries"][0]["tqFlag"] = 1
        scenarios.append((wrong_adjustment, "000001.tqFlag 必须为 0"))

        for payload, expected_message in scenarios:
            with self.subTest(expected_message=expected_message):
                with self.assertRaisesRegex(TongdaxinSourceError, expected_message):
                    build_tongdaxin_index_history_evidence("2026-08-07", payload)

    def test_index_history_requires_five_daily_rows_ending_on_target(self):
        payload = build_index_history_payload()
        payload["queries"][0]["Rows"] = payload["queries"][0]["Rows"][-4:]

        with self.assertRaisesRegex(TongdaxinSourceError, "目标交易日之前不足 4 根日 K"):
            build_tongdaxin_index_history_evidence("2026-08-07", payload)

    def test_confirmed_index_history_import_keeps_snapshot_and_database_unchanged(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            raw_path = evidence_dir / "2026-08-07_tdx_index_history_raw.json"
            raw_path.write_text(
                json.dumps(build_index_history_payload(), ensure_ascii=False),
                encoding="utf-8",
            )
            snapshot_path = evidence_dir / "2026-08-07_snapshot.json"
            snapshot_path.write_text(
                json.dumps(build_index_snapshot(), ensure_ascii=False),
                encoding="utf-8",
            )
            snapshot_before = snapshot_path.read_bytes()
            database_path = root / "stock_review.sqlite"
            database_path.write_bytes(b"database-sentinel")

            output_path = import_tongdaxin_index_history(
                "2026-08-07",
                raw_path,
                confirmed=True,
                output_dir=evidence_dir,
            )

            normalized = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(output_path.name, "2026-08-07_tdx_index_history.json")
            self.assertEqual(normalized["raw_reference"], str(raw_path))
            self.assertEqual(snapshot_path.read_bytes(), snapshot_before)
            self.assertEqual(database_path.read_bytes(), b"database-sentinel")

    def test_index_history_import_rejects_close_conflict_before_writing(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            raw_path = root / "raw.json"
            raw_path.write_text(
                json.dumps(build_index_history_payload(), ensure_ascii=False),
                encoding="utf-8",
            )
            snapshot = build_index_snapshot()
            snapshot["market"]["indices"][2]["close"] = 1
            (root / "2026-08-07_snapshot.json").write_text(
                json.dumps(snapshot, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(EvidenceSnapshotError, "创业板指 收盘与通达信"):
                import_tongdaxin_index_history(
                    "2026-08-07",
                    raw_path,
                    confirmed=True,
                    output_dir=root,
                )

            self.assertFalse((root / "2026-08-07_tdx_index_history.json").exists())

    def test_cli_imports_index_history_through_the_formal_entry(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            raw_path = root / "raw.json"
            raw_path.write_text(
                json.dumps(build_index_history_payload(), ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "2026-08-07_snapshot.json").write_text(
                json.dumps(build_index_snapshot(), ensure_ascii=False),
                encoding="utf-8",
            )

            with patch("builtins.print"):
                exit_code = main(
                    [
                        "evidence",
                        "import-tdx-index-history",
                        "--date",
                        "2026-08-07",
                        "--file",
                        str(raw_path),
                        "--output-dir",
                        str(root),
                        "--log-path",
                        str(root / "stock_review.log"),
                        "--confirmed",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue((root / "2026-08-07_tdx_index_history.json").exists())
            self.assertFalse((root / "stock_review.sqlite").exists())
            self.assertIn(
                "command=evidence import-tdx-index-history",
                (root / "stock_review.log").read_text(encoding="utf-8"),
            )

    def test_fixed_market_queries_build_traceable_turnover_fallback(self):
        evidence = build_tongdaxin_market_evidence("2026-08-07", build_payload())

        self.assertEqual(evidence["source"], TONGDAXIN_SOURCE)
        self.assertEqual(
            evidence["index_close_checks"],
            {"上证指数": 3940.040039, "深证成指": 14311.009766},
        )
        self.assertEqual(evidence["total_amount"], 2663679131648)
        self.assertEqual(evidence["total_amount_source"], TONGDAXIN_AMOUNT_SOURCE)
        self.assertEqual(evidence["total_amount_quality"], MARKET_QUOTE_FALLBACK_QUALITY)
        self.assertEqual(
            evidence["total_amount_calculation"],
            "000002.Rows.Amount+399106.Rows.Amount",
        )
        self.assertEqual(
            evidence["total_amount_breakdown"],
            {
                "shanghai_quote_amount": 1208803000320,
                "shenzhen_quote_amount": 1454876131328,
            },
        )
        self.assertNotIn("up_count", evidence)
        self.assertNotIn("down_count", evidence)

    def test_fixed_market_queries_build_traceable_advance_decline_breadth(self):
        evidence = build_tongdaxin_market_breadth_evidence("2026-08-07", build_payload())

        self.assertEqual(evidence["up_count"], 2687)
        self.assertEqual(evidence["down_count"], 2417)
        self.assertEqual(evidence["advance_decline_source"], TONGDAXIN_BREADTH_SOURCE)
        self.assertEqual(evidence["advance_decline_quality"], MARKET_BREADTH_QUOTE_QUALITY)
        self.assertEqual(
            evidence["advance_decline_breakdown"],
            {
                "shanghai_advance_count": 1195,
                "shanghai_decline_count": 1052,
                "shenzhen_advance_count": 1492,
                "shenzhen_decline_count": 1365,
            },
        )

    def test_market_breadth_requires_matching_shenzhen_quote_fields(self):
        payload = build_payload()
        payload["queries"][2]["Rows"][-1]["up"] = 1

        with self.assertRaisesRegex(TongdaxinSourceError, "up/down 不一致"):
            build_tongdaxin_market_breadth_evidence("2026-08-07", payload)

    def test_market_breadth_rejects_missing_or_non_integral_counts(self):
        for invalid_value in (None, "NaN", -1, "1.5"):
            with self.subTest(invalid_value=invalid_value):
                payload = build_payload()
                payload["queries"][1]["Rows"][-1]["up"] = invalid_value

                with self.assertRaises(TongdaxinSourceError):
                    build_tongdaxin_market_breadth_evidence("2026-08-07", payload)

    def test_market_queries_require_daily_unadjusted_parameters(self):
        for field_name, invalid_value, expected_message in (
            ("setcode", 0, "000001.setcode 必须为 1"),
            ("period", 5, "000001.period 必须为 4"),
            ("tqFlag", 1, "000001.tqFlag 必须为 0"),
        ):
            with self.subTest(field_name=field_name):
                payload = build_payload()
                payload["queries"][0][field_name] = invalid_value

                with self.assertRaisesRegex(TongdaxinSourceError, expected_message):
                    build_tongdaxin_market_evidence("2026-08-07", payload)

    def test_market_queries_require_all_fixed_codes_without_duplicates(self):
        payload = build_payload()
        payload["queries"][-1]["code"] = "000001"

        with self.assertRaisesRegex(TongdaxinSourceError, "查询代码重复"):
            build_tongdaxin_market_evidence("2026-08-07", payload)

    def test_market_queries_require_the_exact_target_daily_row(self):
        payload = build_payload()
        payload["queries"][0]["Rows"] = [{"Data": "20260806", "Close": "3900.35"}]

        with self.assertRaisesRegex(TongdaxinSourceError, "未精确返回目标交易日"):
            build_tongdaxin_market_evidence("2026-08-07", payload)

    def test_amount_queries_reject_non_integral_or_unusable_values(self):
        for invalid_value in ("NaN", -1, "120.5"):
            with self.subTest(invalid_value=invalid_value):
                payload = build_payload()
                payload["queries"][1]["Rows"][-1]["Amount"] = invalid_value

                with self.assertRaises(TongdaxinSourceError):
                    build_tongdaxin_market_evidence("2026-08-07", payload)

    def test_deep_market_amount_requires_matching_composite_and_component_rows(self):
        payload = build_payload()
        payload["queries"][2]["Rows"][-1]["Amount"] = 1

        with self.assertRaisesRegex(TongdaxinSourceError, "399001.Amount 与 399106.Amount 不一致"):
            build_tongdaxin_market_evidence("2026-08-07", payload)

    def test_detailed_report_marks_quote_fallback_scope(self):
        snapshot = build_evidence_snapshot(
            "2026-08-07",
            {
                "market": {
                    "indices": [{"name": "上证指数"}],
                    "total_amount": 2663679131648,
                    "total_amount_quality": MARKET_QUOTE_FALLBACK_QUALITY,
                }
            },
        )

        self.assertIn(
            "- 沪深成交额（通达信行情口径，非交易所官方汇总）：2663679131648。",
            render_market_lines(snapshot),
        )

    def test_report_marks_market_breadth_quote_scope(self):
        snapshot = build_evidence_snapshot(
            "2026-08-07",
            {
                "market": {
                    "indices": [{"name": "上证指数"}],
                    "up_count": 2687,
                    "down_count": 2417,
                    "advance_decline_quality": MARKET_BREADTH_QUOTE_QUALITY,
                }
            },
        )

        self.assertIn(
            "- 涨跌家数（通达信行情统计口径，非交易所官方全市场统计）：上涨 2687，下跌 2417。",
            render_market_lines(snapshot),
        )

    def test_confirmed_import_merges_market_without_overwriting_other_scopes(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            output_dir = root / "evidence"
            output_dir.mkdir()
            database_path = root / "stock_review.sqlite"
            input_path = root / "tongdaxin.json"
            input_path.write_text(json.dumps(build_payload(), ensure_ascii=False), encoding="utf-8")
            existing_snapshot = {
                "source": "hhxg",
                "sample_date": "2026-08-07",
                "market": {
                    "indices": [
                        {"name": "上证指数", "close": 3940.04, "change_percent": 1.02},
                        {"name": "深证成指", "close": 14311.01, "change_percent": 1.42},
                        {"name": "创业板指", "close": 3563.12, "change_percent": 1.35},
                    ]
                },
                "sentiment": {
                    "limit_up_count": 80,
                    "limit_down_count": 10,
                    "broken_board_rate": 0.2,
                    "highest_board": 5,
                },
                "sectors": [{"name": "机器人"}],
                "stocks": [{"code": "300024", "name": "机器人"}],
                "field_sources": {
                    "market": "akshare:tencent",
                    "sentiment": "hhxg",
                    "sectors": "hhxg",
                    "stocks": "akshare",
                },
                "events": [{"title": "既有事件", "source": "akshare"}],
            }
            (output_dir / "2026-08-07_snapshot.json").write_text(
                json.dumps(existing_snapshot, ensure_ascii=False),
                encoding="utf-8",
            )

            output_path = import_tongdaxin_market_snapshot(
                "2026-08-07",
                input_path,
                confirmed=True,
                output_dir=output_dir,
                database_path=database_path,
            )

            snapshot_data = json.loads(output_path.read_text(encoding="utf-8"))
            stored_data = EvidenceSnapshotRepository(database_path).load_snapshot("2026-08-07")
            self.assertEqual(snapshot_data["market"]["total_amount"], 2663679131648)
            self.assertEqual(snapshot_data["market"]["indices"], existing_snapshot["market"]["indices"])
            self.assertEqual(snapshot_data["sentiment"]["limit_up_count"], 80)
            self.assertEqual(snapshot_data["sectors"], [{"name": "机器人"}])
            self.assertEqual(snapshot_data["stocks"], [{"code": "300024", "name": "机器人"}])
            self.assertEqual(
                snapshot_data["field_sources"]["market"],
                f"akshare:tencent+{TONGDAXIN_SOURCE}",
            )
            self.assertEqual(
                [event["title"] for event in snapshot_data["events"]],
                ["既有事件", "通达信成交额行情备用证据"],
            )
            self.assertNotIn("missing_total_amount", snapshot_data["missing_fields"])
            self.assertEqual(stored_data["snapshot"]["market"]["total_amount"], 2663679131648)

    def test_import_requires_complete_matching_existing_indices(self):
        base_indices = [
            {"name": "上证指数", "close": 3940.04},
            {"name": "深证成指", "close": 14311.01},
            {"name": "创业板指", "close": 3563.12},
        ]
        scenarios = (
            (base_indices[:2], "缺少完整三大指数"),
            ([{"name": "上证指数", "close": 1}, *base_indices[1:]], "上证指数 收盘与通达信"),
        )
        for indices, expected_message in scenarios:
            with self.subTest(expected_message=expected_message), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                output_dir = root / "evidence"
                output_dir.mkdir()
                input_path = root / "tongdaxin.json"
                input_path.write_text(json.dumps(build_payload(), ensure_ascii=False), encoding="utf-8")
                snapshot_path = output_dir / "2026-08-07_snapshot.json"
                snapshot_path.write_text(
                    json.dumps(
                        {
                            "source": "akshare",
                            "sample_date": "2026-08-07",
                            "market": {"indices": indices},
                            "sentiment": {},
                            "sectors": [],
                            "stocks": [],
                            "events": [],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(EvidenceSnapshotError, expected_message):
                    import_tongdaxin_market_snapshot(
                        "2026-08-07",
                        input_path,
                        confirmed=True,
                        output_dir=output_dir,
                        database_path=root / "stock_review.sqlite",
                    )

    def test_confirmed_breadth_import_keeps_existing_market_facts(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            raw_path = root / "tongdaxin.json"
            raw_path.write_text(json.dumps(build_payload(), ensure_ascii=False), encoding="utf-8")
            snapshot = build_index_snapshot()
            snapshot["market"]["total_amount"] = 2145000000000
            snapshot["sentiment"] = {
                "limit_up_count": 63,
                "limit_down_count": 9,
                "broken_board_rate": 0.3077,
                "highest_board": 5,
            }
            snapshot_path = root / "2026-08-07_snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

            output_path = import_tongdaxin_market_breadth_snapshot(
                "2026-08-07",
                raw_path,
                confirmed=True,
                output_dir=root,
                database_path=root / "stock_review.sqlite",
            )

            stored = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["market"]["total_amount"], 2145000000000)
            self.assertEqual(stored["market"]["up_count"], 2687)
            self.assertEqual(stored["market"]["down_count"], 2417)
            self.assertEqual(stored["sentiment"]["limit_up_count"], 63)
            self.assertIn("通达信涨跌家数行情证据", [item["title"] for item in stored["events"]])

    def test_import_requires_confirmation_and_does_not_replace_primary_turnover(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            output_dir = root / "evidence"
            output_dir.mkdir()
            input_path = root / "tongdaxin.json"
            input_path.write_text(json.dumps(build_payload(), ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(EvidenceSnapshotError, "用户明确确认"):
                import_tongdaxin_market_snapshot(
                    "2026-08-07",
                    input_path,
                    confirmed=False,
                    output_dir=output_dir,
                    database_path=root / "stock_review.sqlite",
                )

            existing_snapshot = {
                "source": "akshare",
                "sample_date": "2026-08-07",
                "market": {
                    "indices": [{"name": "上证指数"}],
                    "total_amount": 2667015274973.73,
                    "total_amount_source": "akshare:stock_sse_deal_daily+akshare:stock_szse_summary",
                },
                "sentiment": {},
                "sectors": [],
                "stocks": [],
                "events": [],
            }
            snapshot_path = output_dir / "2026-08-07_snapshot.json"
            snapshot_path.write_text(json.dumps(existing_snapshot, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(EvidenceSnapshotError, "禁止用通达信行情口径覆盖"):
                import_tongdaxin_market_snapshot(
                    "2026-08-07",
                    input_path,
                    confirmed=True,
                    output_dir=output_dir,
                    database_path=root / "stock_review.sqlite",
                )
            self.assertEqual(json.loads(snapshot_path.read_text(encoding="utf-8")), existing_snapshot)

    def test_cli_imports_fallback_through_the_formal_entry(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            output_dir = root / "evidence"
            output_dir.mkdir()
            input_path = root / "tongdaxin.json"
            input_path.write_text(json.dumps(build_payload(), ensure_ascii=False), encoding="utf-8")
            snapshot_path = output_dir / "2026-08-07_snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "source": "akshare",
                        "sample_date": "2026-08-07",
                        "market": {
                            "indices": [
                                {"name": "上证指数", "close": 3940.04},
                                {"name": "深证成指", "close": 14311.01},
                                {"name": "创业板指", "close": 3563.12},
                            ]
                        },
                        "sentiment": {},
                        "sectors": [],
                        "stocks": [],
                        "events": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("stock_review.cli.write_evidence_log"), patch("builtins.print"):
                exit_code = main(
                    [
                        "evidence",
                        "import-tdx-market",
                        "--date",
                        "2026-08-07",
                        "--file",
                        str(input_path),
                        "--output-dir",
                        str(output_dir),
                        "--database",
                        str(root / "stock_review.sqlite"),
                        "--confirmed",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                json.loads(snapshot_path.read_text(encoding="utf-8"))["market"]["total_amount"],
                2663679131648,
            )

    def test_cli_imports_market_breadth_through_the_formal_entry(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            raw_path = root / "tongdaxin.json"
            raw_path.write_text(json.dumps(build_payload(), ensure_ascii=False), encoding="utf-8")
            snapshot_path = root / "2026-08-07_snapshot.json"
            snapshot_path.write_text(
                json.dumps(build_index_snapshot(), ensure_ascii=False),
                encoding="utf-8",
            )

            with patch("stock_review.cli.write_evidence_log"), patch("builtins.print"):
                exit_code = main(
                    [
                        "evidence",
                        "import-tdx-market-breadth",
                        "--date",
                        "2026-08-07",
                        "--file",
                        str(raw_path),
                        "--output-dir",
                        str(root),
                        "--database",
                        str(root / "stock_review.sqlite"),
                        "--confirmed",
                    ]
                )

            self.assertEqual(exit_code, 0)
            market = json.loads(snapshot_path.read_text(encoding="utf-8"))["market"]
            self.assertEqual(market["up_count"], 2687)
            self.assertEqual(market["down_count"], 2417)


def build_payload() -> dict:
    rows = {
        "000001": [
            {"Data": "20260806", "Close": "3900.35", "Amount": 1100000000000, "up": 1200, "down": 1000},
            {"Data": "20260807", "Close": "3940.040039", "Amount": 1209543557120, "up": 1216, "down": 1062},
        ],
        "000002": [
            {"Data": "20260807", "Close": "4131.379883", "Amount": 1208803000320, "up": 1195, "down": 1052},
        ],
        "399001": [
            {"Data": "20260806", "Close": "14110.12", "Amount": 1300000000000, "up": 1400, "down": 1300},
            {"Data": "20260807", "Close": "14311.009766", "Amount": 1454876131328, "up": 1492, "down": 1365},
        ],
        "399106": [
            {"Data": "20260807", "Close": "2574.649902", "Amount": 1454876131328, "up": 1492, "down": 1365},
        ],
    }
    setcodes = {"000001": 1, "000002": 1, "399001": 0, "399106": 0}
    return {
        "queries": [
            {
                "code": code,
                "setcode": setcodes[code],
                "period": 4,
                "tqFlag": 0,
                "Rows": deepcopy(rows[code]),
            }
            for code in ("000001", "000002", "399001", "399106")
        ]
    }


def build_index_history_payload(include_later_row: bool = False) -> dict:
    dates = ("20260802", "20260803", "20260804", "20260805", "20260806", "20260807")
    closes = {
        "000001": (3860.0, 3875.0, 3890.0, 3882.0, 3900.35, 3940.040039),
        "399001": (13900.0, 14010.0, 14080.0, 14040.0, 14110.12, 14311.009766),
        "399006": (3450.0, 3470.0, 3490.0, 3480.0, 3515.56, 3563.120117),
    }
    if include_later_row:
        dates = (*dates, "20260810")
        closes = {
            code: (*values, values[-1] + 1)
            for code, values in closes.items()
        }
    setcodes = {"000001": 1, "399001": 0, "399006": 0}
    return {
        "queries": [
            {
                "code": code,
                "setcode": setcodes[code],
                "period": 4,
                "tqFlag": 0,
                "Rows": [
                    {"Data": row_date, "Close": close, "Amount": 999}
                    for row_date, close in zip(dates, closes[code], strict=True)
                ],
            }
            for code in ("000001", "399001", "399006")
        ]
    }


def build_index_snapshot() -> dict:
    return {
        "trade_date": "2026-08-07",
        "source": "akshare",
        "sample_date": "2026-08-07",
        "market": {
            "indices": [
                {"name": "上证指数", "close": 3940.04},
                {"name": "深证成指", "close": 14311.01},
                {"name": "创业板指", "close": 3563.12},
            ]
        },
        "sentiment": {},
        "sectors": [],
        "stocks": [],
        "events": [],
    }
