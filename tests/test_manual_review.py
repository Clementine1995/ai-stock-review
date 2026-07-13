from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from stock_review.review_documents.manage_manual_review import (
    ManualReviewError,
    add_manual_preview,
    add_manual_step_record,
    list_manual_review_records,
)


class ManualReviewTest(unittest.TestCase):
    def test_step_judgment_and_preview_can_be_saved_and_listed(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"
            step_record = add_manual_step_record(
                "2026-07-13",
                1,
                "市场阶段待确认，等待连续样本。",
                "data/evidence/2026-07-13_snapshot.json",
                hypothesis="后续五日补齐后再回看。",
                database_path=database_path,
            )
            preview = add_manual_preview(
                "2026-07-13",
                "军民融合板块",
                "次日保持强势且有证据支持。",
                "强于板块排行事实。",
                "弱于当日排行事实。",
                "证据缺失时放弃观察。",
                "data/evidence/2026-07-13_snapshot.json",
                user_confirmed=True,
                database_path=database_path,
            )
            records, previews = list_manual_review_records("2026-07-13", database_path)

            self.assertEqual(step_record.record_id, "STEP-20260713-01-001")
            self.assertEqual(preview.preview_id, "PREVIEW-20260713-001")
            self.assertEqual(records[0].judgment, "市场阶段待确认，等待连续样本。")
            self.assertEqual(previews[0].target, "军民融合板块")

    def test_preview_requires_explicit_user_confirmation_and_all_conditions(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"
            values = {
                "target": "军民融合板块",
                "expectation": "符合预期",
                "over_expectation": "超预期",
                "under_expectation": "不及预期",
                "abandon_condition": "放弃",
                "evidence_reference": "data/evidence/2026-07-13_snapshot.json",
                "database_path": database_path,
            }

            with self.assertRaisesRegex(ManualReviewError, "用户显式确认"):
                add_manual_preview("2026-07-13", user_confirmed=False, **values)
            values["abandon_condition"] = ""
            with self.assertRaisesRegex(ManualReviewError, "放弃条件不能为空"):
                add_manual_preview("2026-07-13", user_confirmed=True, **values)

    def test_manual_review_tables_do_not_create_observation_records(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"
            add_manual_step_record(
                "2026-07-13", 7, "候选待人工确认。", "data/evidence/2026-07-13_snapshot.json", database_path=database_path
            )

            connection = sqlite3.connect(database_path)
            try:
                table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            finally:
                connection.close()
            self.assertIn("manual_step_records", table_names)
            self.assertNotIn("observations", table_names)


if __name__ == "__main__":
    unittest.main()
