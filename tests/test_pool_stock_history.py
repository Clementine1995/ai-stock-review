from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.collect_pool_stock_history import (
    build_history_record,
    collect_real_pool_stock_history,
    collect_review_candidate_history,
    window_return,
)
from stock_review.pools.manage_pool_item import add_pool_item


class PoolStockHistoryTest(unittest.TestCase):
    def test_collects_only_real_pool_stock_and_calculates_windows(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            add_pool_item("hot", "002829", "星网宇达", "2026-07-10", "真实热点", record_kind="real", database_path=database_path)
            add_pool_item("hot", "600519", "贵州茅台", "2026-07-10", "样例", record_kind="sample", database_path=database_path)
            client = FakeAkshareClient()

            output_path = collect_real_pool_stock_history("2026-07-10", root, database_path, ak_client=client, log_path=root / "log.txt")
            payload = json.loads(output_path.read_text(encoding="utf-8"))

            self.assertEqual(client.symbols, ["002829"])
            self.assertEqual(payload["records"][0]["history_days"], 20)
            self.assertEqual(payload["records"][0]["return_5d_percent"], 4.3478)
            self.assertIsNone(payload["records"][0]["return_20d_percent"])
            self.assertIn("missing_20d_history", payload["records"][0]["missing_fields"])

    def test_falls_back_to_tencent_daily_history_without_relabeling_lot_volume_as_amount(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            add_pool_item("hot", "002829", "星网宇达", "2026-07-10", "真实热点", record_kind="real", database_path=database_path)
            client = FallbackAkshareClient()

            output_path = collect_real_pool_stock_history("2026-07-10", root, database_path, ak_client=client, log_path=root / "log.txt")
            record = json.loads(output_path.read_text(encoding="utf-8"))["records"][0]

            self.assertEqual(client.tx_symbols, ["sz002829"])
            self.assertEqual(record["source"], "akshare:stock_zh_a_hist_tx")
            self.assertEqual(record["volume"], 1000.0)
            self.assertIsNone(record["amount"])
            self.assertIn("missing_latest_amount", record["missing_fields"])
            self.assertEqual(record["return_5d_percent"], 4.3478)

    def test_return_windows_use_one_more_close_than_interval_count(self):
        rows = [{"close": 100 + index} for index in range(21)]

        self.assertEqual(window_return(rows, 5), 4.3478)
        self.assertEqual(window_return(rows, 20), 20.0)

    def test_return_windows_require_complete_interval_count(self):
        for window in (5, 20):
            with self.subTest(window=window):
                rows = [{"close": 100 + index} for index in range(window)]

                self.assertIsNone(window_return(rows, window))

    def test_history_record_normalizes_turnover_percent_to_ratio(self):
        record = build_history_record(
            "300862",
            "蓝盾光电",
            ("通用设备",),
            date(2026, 8, 14),
            [{"date": "2026-08-14", "close": 47.29, "换手率": 51.53}],
        )

        self.assertEqual(record["turnover"], 0.5153)
        self.assertEqual(record["turnover_unit"], "ratio")
        self.assertEqual(record["daily_bars"][0]["turnover"], 0.5153)

    def test_collects_snapshot_and_real_pool_candidates_with_capacity_facts(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            add_pool_item("hot", "002829", "星网宇达", "2026-07-10", "真实热点", record_kind="real", database_path=database_path)
            (root / "2026-07-10_snapshot.json").write_text(
                json.dumps(
                    {
                        "trade_date": "2026-07-10",
                        "sample_date": "2026-07-10",
                        "stocks": [
                            {"code": "603580", "name": "艾艾精工", "sector": "轻工制造"}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            client = DailyCapacityClient()

            output_path = collect_review_candidate_history(
                "2026-07-10",
                snapshot_dir=root,
                output_dir=root,
                database_path=database_path,
                ak_client=client,
                log_path=root / "log.txt",
            )
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            records = {record["code"]: record for record in payload["records"]}

            self.assertEqual(payload["turnover_unit"], "ratio")
            self.assertEqual(set(records), {"002829", "603580"})
            self.assertEqual(records["603580"]["source"], "akshare:stock_zh_a_daily")
            self.assertEqual(records["603580"]["turnover"], 0.1)
            self.assertEqual(records["603580"]["turnover_unit"], "ratio")
            self.assertEqual(records["603580"]["average_5d_amount"], 100000000.0)
            self.assertEqual(records["603580"]["circulating_market_value"], 1300000000.0)
            self.assertEqual(records["603580"]["daily_bar_count"], 10)
            self.assertEqual(records["603580"]["adjustment"], "none")
            self.assertEqual(
                set(records["603580"]["daily_bars"][0]),
                {"date", "open", "high", "low", "close", "volume", "amount", "turnover"},
            )

    def test_history_record_keeps_only_latest_twenty_one_daily_bars(self):
        rows = [
            {
                "date": f"2026-06-{index:02d}" if index <= 30 else f"2026-07-{index - 30:02d}",
                "open": 90 + index,
                "high": 92 + index,
                "low": 89 + index,
                "close": 91 + index,
                "volume": 1000 + index,
                "amount": 100000 + index,
                "turnover": 1 + index / 100,
            }
            for index in range(1, 41)
        ]

        record = build_history_record("603580", "艾艾精工", ("轻工制造",), date(2026, 7, 10), rows)

        self.assertEqual(record["daily_bar_count"], 21)
        self.assertTrue(record["daily_bars_complete"])
        self.assertEqual(record["daily_bars"][0]["date"], "2026-06-20")
        self.assertEqual(record["daily_bars"][-1]["date"], "2026-07-10")
        self.assertEqual(record["daily_bars"][-1]["close"], 131.0)

    def test_history_record_does_not_mark_stale_twenty_one_bars_complete(self):
        rows = [
            {
                "date": f"2026-06-{index:02d}" if index <= 21 else f"2026-07-{index - 21:02d}",
                "open": 90 + index,
                "high": 92 + index,
                "low": 89 + index,
                "close": 91 + index,
                "volume": 1000 + index,
                "amount": 100000 + index,
                "turnover": 1 + index / 100,
            }
            for index in range(1, 31)
        ]

        record = build_history_record(
            "603580",
            "艾艾精工",
            ("轻工制造",),
            date(2026, 7, 10),
            rows,
        )

        self.assertEqual(record["daily_bar_count"], 21)
        self.assertEqual(record["daily_bars"][-1]["date"], "2026-07-09")
        self.assertFalse(record["daily_bars_complete"])
        self.assertIn("missing_review_date_bar", record["missing_fields"])

    def test_review_history_skips_real_pool_that_requires_freshness_review(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            add_pool_item(
                "hot",
                "002829",
                "星网宇达",
                "2026-08-02",
                "真实热点",
                record_kind="real",
                database_path=database_path,
            )
            for trade_date in ("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"):
                (root / f"{trade_date}_snapshot.json").write_text("{}", encoding="utf-8")
            (root / "2026-08-07_snapshot.json").write_text(
                json.dumps(
                    {
                        "trade_date": "2026-08-07",
                        "sample_date": "2026-08-07",
                        "stocks": [{"code": "603580", "name": "艾艾精工", "sector": "轻工制造"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            client = DailyCapacityClient()

            output_path = collect_review_candidate_history(
                "2026-08-07",
                snapshot_dir=root,
                output_dir=root,
                database_path=database_path,
                ak_client=client,
                log_path=root / "log.txt",
            )
            records = json.loads(output_path.read_text(encoding="utf-8"))["records"]

            self.assertEqual([record["code"] for record in records], ["603580"])


class FakeAkshareClient:
    def __init__(self):
        self.symbols: list[str] = []

    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust):
        self.symbols.append(symbol)
        return [
            {"日期": f"2026-06-{index:02d}", "收盘": 100 + index, "涨跌幅": 1, "成交量": 10, "成交额": 1000}
            for index in range(21, 31)
        ] + [
            {"日期": f"2026-07-{index:02d}", "收盘": 110 + index, "涨跌幅": 1, "成交量": 10, "成交额": 1000}
            for index in range(1, 11)
        ]


class FallbackAkshareClient:
    def __init__(self):
        self.tx_symbols: list[str] = []

    def stock_zh_a_hist(self, **_kwargs):
        raise RuntimeError("主日线不可用")

    def stock_zh_a_hist_tx(self, symbol, start_date, end_date, adjust):
        self.tx_symbols.append(symbol)
        return [
            {"date": f"2026-06-{index:02d}", "close": 100 + index, "amount": 1000}
            for index in range(21, 31)
        ] + [
            {"date": f"2026-07-{index:02d}", "close": 110 + index, "amount": 1000}
            for index in range(1, 11)
        ]


class DailyCapacityClient:
    def stock_zh_a_daily(self, symbol, start_date, end_date, adjust):
        return [
            {
                "date": f"2026-06-{index:02d}",
                "close": 100 + index,
                "volume": 1000000,
                "amount": 100000000,
                "outstanding_share": 10000000,
                "turnover": 0.1,
            }
            for index in range(21, 31)
        ]


if __name__ == "__main__":
    unittest.main()
