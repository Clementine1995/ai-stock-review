from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_review.observations.manage_observation import add_observation
from stock_review.pools.manage_pool_item import add_pool_item
from stock_review.review_documents.manage_manual_review import (
    ManualReviewError,
    add_manual_final_response,
    add_manual_preview,
    list_manual_final_responses,
)


class ManualFinalResponseTest(unittest.TestCase):
    def test_confirmed_final_response_saves_all_explicit_links(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            plan_path = self.write_plan(root)
            add_pool_item("hot", "002829", "星网宇达", "2026-07-14", "人工确认", database_path=database_path)
            preview = add_manual_preview(
                "2026-07-14", "星网宇达", "符合", "超预期", "不及预期", "放弃", "snapshot", True, database_path
            )
            observation = add_observation(
                "2026-07-14", "次日反馈", "仅观察", "符合", "放弃", "snapshot",
                related_target="星网宇达", plan_item="计划项 1: 002829 星网宇达", database_path=database_path,
            )

            final_response = add_manual_final_response(
                "2026-07-14", "不开新仓，只观察反馈。", "证据不足时放弃。", "snapshot", plan_path,
                "计划项 1: 002829 星网宇达", ("002829",), (preview.preview_id,),
                (observation.observation_id,), True, database_path,
            )

            self.assertEqual(final_response.final_id, "FINAL-20260714-001")
            saved = list_manual_final_responses("2026-07-14", database_path)
            self.assertEqual(saved[0].pool_codes, ("002829",))
            self.assertEqual(saved[0].preview_ids, (preview.preview_id,))
            self.assertEqual(saved[0].observation_ids, (observation.observation_id,))

    def test_final_response_requires_user_confirmation(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            with self.assertRaisesRegex(ManualReviewError, "用户显式确认"):
                add_manual_final_response(
                    "2026-07-14", "只观察", "证据不足", "snapshot", self.write_plan(root),
                    "计划项 1: 002829 星网宇达", ("002829",), ("PREVIEW-20260714-001",),
                    ("OBS-20260714-001",), False, root / "stock_review.sqlite",
                )

    @staticmethod
    def write_plan(root: Path) -> Path:
        plan_path = root / "2026-07-14_plan.md"
        plan_path.write_text("### 计划项 1: 002829 星网宇达\n", encoding="utf-8")
        return plan_path
