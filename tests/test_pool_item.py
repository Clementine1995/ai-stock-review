from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from stock_review.pools.manage_pool_item import (
    PoolItemError,
    add_pool_item,
    list_pool_item_status_history,
    list_pool_items,
    update_pool_item_record_kind,
    update_pool_item_status,
)


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
            self.assertEqual(item.record_kind, "real")
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

    def test_pool_status_update_keeps_item_and_history(self):
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

            item = update_pool_item_status(
                "watch",
                code="000001",
                status="paused",
                reason="等待板块确认",
                note="验收样例",
                database_path=database_path,
            )
            history = list_pool_item_status_history("watch", "000001", database_path=database_path)

            self.assertEqual(item.status, "paused")
            self.assertEqual(len(list_pool_items("watch", database_path=database_path)), 1)
            self.assertEqual([event.status for event in history], ["active", "paused"])
            self.assertEqual(history[-1].reason, "等待板块确认")

    def test_pool_status_update_requires_reason_and_existing_item(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"

            with self.assertRaisesRegex(PoolItemError, "更新池子状态必须填写原因"):
                update_pool_item_status(
                    "watch",
                    code="000001",
                    status="removed",
                    reason="",
                    database_path=database_path,
                )

    def test_pool_record_kind_can_be_updated_and_filtered(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"
            add_pool_item("watch", "000001", "平安银行", "2026-07-06", "样例关注", database_path=database_path)
            update_pool_item_record_kind("watch", "000001", "sample", "系统验收样例", database_path=database_path)

            self.assertEqual(list_pool_items(record_kind="real", database_path=database_path), [])
            self.assertEqual(list_pool_items(record_kind="sample", database_path=database_path)[0].code, "000001")

    def test_pool_history_query_does_not_create_tables(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"

            history = list_pool_item_status_history("watch", "000001", database_path=database_path)

            connection = sqlite3.connect(database_path)
            try:
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(history, [])
            self.assertEqual(tables, [])
            with self.assertRaisesRegex(PoolItemError, "未找到关注池记录"):
                update_pool_item_status(
                    "watch",
                    code="000001",
                    status="removed",
                    reason="不再关注",
                    database_path=database_path,
                )


if __name__ == "__main__":
    unittest.main()
