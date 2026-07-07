from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_review.pools.manage_pool_item import PoolItemError, add_pool_item, list_pool_items


class PoolItemTest(unittest.TestCase):
    def test_watch_pool_item_can_be_added_and_listed(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"

            item = add_pool_item(
                "watch",
                code="000001",
                name="平安银行",
                start_date="2026-07-06",
                reason="样例关注",
                exchange="SZSE",
                sector="银行",
                database_path=database_path,
            )
            items = list_pool_items("watch", database_path=database_path)

            self.assertEqual(item.status, "active")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].code, "000001")
            self.assertEqual(items[0].sector, "银行")

    def test_hot_pool_requires_reason(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"

            with self.assertRaisesRegex(PoolItemError, "热点池必须填写进入原因"):
                add_pool_item(
                    "hot",
                    code="000001",
                    name="平安银行",
                    start_date="2026-07-06",
                    reason="",
                    database_path=database_path,
                )

    def test_duplicate_pool_item_reports_existing_record(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"
            add_pool_item(
                "watch",
                code="000001",
                name="平安银行",
                start_date="2026-07-06",
                reason="样例关注",
                database_path=database_path,
            )

            with self.assertRaisesRegex(PoolItemError, "关注池已存在该股票"):
                add_pool_item(
                    "watch",
                    code="000001",
                    name="平安银行",
                    start_date="2026-07-07",
                    reason="重复关注",
                    database_path=database_path,
                )

    def test_list_all_pool_items_keeps_pool_type(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"
            add_pool_item(
                "watch",
                code="000001",
                name="平安银行",
                start_date="2026-07-06",
                reason="样例关注",
                database_path=database_path,
            )
            add_pool_item(
                "hot",
                code="600519",
                name="贵州茅台",
                start_date="2026-07-06",
                reason="样例热点",
                database_path=database_path,
            )

            items = list_pool_items(database_path=database_path)

            self.assertEqual([item.pool_type for item in items], ["hot", "watch"])


if __name__ == "__main__":
    unittest.main()
