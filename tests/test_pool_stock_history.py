from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.collect_pool_stock_history import collect_real_pool_stock_history
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
            self.assertEqual(payload["records"][0]["return_5d_percent"], 3.4483)
            self.assertEqual(payload["records"][0]["return_20d_percent"], -0.8264)


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


if __name__ == "__main__":
    unittest.main()
