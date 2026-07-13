from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.pools.manage_pool_item import add_pool_item, update_pool_item_record_kind
from stock_review.review_documents.build_review_context import build_review_context, render_review_context
from stock_review.review_documents.manage_manual_review import add_manual_preview, add_manual_step_record


class ReviewContextTest(unittest.TestCase):
    def test_context_keeps_empty_real_pool_as_normal_input_state(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-13")

            context = build_review_context("2026-07-13", snapshot_dir=root, database_path=root / "stock_review.sqlite")
            content = render_review_context(context)

            self.assertEqual(context.active_real_pool_items, ())
            self.assertEqual(context.missing_step_numbers, (1, 2, 3, 4, 5, 6, 7))
            self.assertIn("对象数：0", content)
            self.assertIn("不形成重点关注候选", content)

    def test_context_reads_only_active_real_pool_and_existing_manual_records(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            self.write_snapshot(root, "2026-07-13")
            add_pool_item("hot", "002829", "星网宇达", "2026-07-13", "人工确认", record_kind="real", database_path=database_path)
            add_pool_item("watch", "000001", "平安银行", "2026-07-13", "样例", record_kind="sample", database_path=database_path)
            add_manual_step_record("2026-07-13", 7, "仅保留人工确认对象。", "snapshot", database_path=database_path)
            add_manual_preview(
                "2026-07-13", "星网宇达", "符合", "超预期", "不及预期", "放弃", "snapshot", True, database_path
            )

            context = build_review_context("2026-07-13", snapshot_dir=root, database_path=database_path)

            self.assertEqual([item.code for item in context.active_real_pool_items], ["002829"])
            self.assertNotIn(7, context.missing_step_numbers)
            self.assertEqual(context.focus_input_status, "输入已齐备，仍需用户确认最终重点关注。")

    @staticmethod
    def write_snapshot(root: Path, trade_date: str) -> None:
        payload = {
            "trade_date": trade_date,
            "sample_date": trade_date,
            "source": "manual",
            "market": {},
            "sentiment": {},
            "sectors": [],
            "stocks": [],
            "events": [],
        }
        (root / f"{trade_date}_snapshot.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
