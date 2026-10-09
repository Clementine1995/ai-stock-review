from pathlib import Path
import hashlib
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_review.learning.create_weekly_review_run import create_weekly_review_run
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.storage.review_run_repository import (
    OptimisticConcurrencyError,
    ReviewRunRepository,
    ReviewRunRepositoryError,
)


class CreateWeeklyReviewRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repository = ReviewRunRepository(self.root / "review.sqlite")
        self.repository.initialize_schema()

    def test_weekly_review_pins_current_range_and_replays_same_input(self):
        outside = self.seed_daily("outside", "2026-08-14", result_status="hit")
        first = self.seed_daily("first", "2026-08-17", result_status="hit")
        second = self.seed_daily("second", "2026-08-18", result_status=None)

        result = create_weekly_review_run(
            "2026-08-17",
            "2026-08-18",
            self.repository,
            output_root=self.root / "weekly-output",
        )
        replayed = create_weekly_review_run(
            "2026-08-17",
            "2026-08-18",
            self.repository,
            output_root=self.root / "weekly-output",
        )

        report_path = Path(result["report_path"])
        self.assertTrue(report_path.is_file())
        self.assertEqual(report_path.parent.name, "blobs")
        self.assertEqual(report_path.stem, result["report_hash"])
        self.assertEqual(
            hashlib.sha256(report_path.read_bytes()).hexdigest(),
            result["report_hash"],
        )
        self.assertFalse(result["replayed"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(result["weekly_revision_id"], replayed["weekly_revision_id"])
        self.assertEqual(
            result["weekly_publication_id"], replayed["weekly_publication_id"]
        )

        revision = self.repository.get_weekly_revision(result["weekly_revision_id"])
        self.assertEqual(
            revision["review_publication_ids"],
            [first["publication_id"], second["publication_id"]],
        )
        self.assertEqual(
            revision["observation_result_revision_ids"],
            [first["result_revision_id"]],
        )
        self.assertEqual(
            revision["pool_status_event_ids"],
            [first["pool_event_id"], second["pool_event_id"]],
        )
        self.assertNotIn(outside["publication_id"], revision["review_publication_ids"])
        observation_statuses = {
            item["observation_id"]: item["current_result"]["status"]
            for item in revision["payload"]["observations"]
        }
        self.assertEqual(
            observation_statuses,
            {
                first["observation_id"]: "hit",
                second["observation_id"]: "pending",
            },
        )

    def test_changed_current_result_creates_new_revision_and_supersedes_weekly(self):
        daily = self.seed_daily("changed", "2026-08-17", result_status="hit")
        first = create_weekly_review_run(
            "2026-08-17",
            "2026-08-17",
            self.repository,
            output_root=self.root / "weekly-output",
        )
        self.repository.append_observation_result_revision(
            daily["observation_id"],
            "result-changed-corrected",
            "miss",
            {"actual_result": "条件未出现", "reason": "判断错误"},
            expected_result_revision_id=daily["result_revision_id"],
        )

        second = create_weekly_review_run(
            "2026-08-17",
            "2026-08-17",
            self.repository,
            output_root=self.root / "weekly-output",
        )
        replayed = create_weekly_review_run(
            "2026-08-17",
            "2026-08-17",
            self.repository,
            output_root=self.root / "weekly-output",
        )

        self.assertNotEqual(first["weekly_revision_id"], second["weekly_revision_id"])
        self.assertNotEqual(
            first["weekly_publication_id"], second["weekly_publication_id"]
        )
        self.assertEqual(
            second["supersedes_weekly_publication_id"],
            first["weekly_publication_id"],
        )
        self.assertTrue(Path(first["report_path"]).is_file())
        self.assertTrue(Path(second["report_path"]).is_file())
        self.assertTrue(replayed["replayed"])
        first_revision = self.repository.get_weekly_revision(
            first["weekly_revision_id"]
        )
        second_revision = self.repository.get_weekly_revision(
            second["weekly_revision_id"]
        )
        self.assertEqual(
            first_revision["observation_result_revision_ids"],
            [daily["result_revision_id"]],
        )
        self.assertEqual(
            second_revision["observation_result_revision_ids"],
            ["result-changed-corrected"],
        )

    def test_weekly_review_rejects_range_without_current_daily_publication(self):
        with self.assertRaisesRegex(ReviewRunDomainError, "没有当前正式日报"):
            create_weekly_review_run(
                "2026-08-17",
                "2026-08-21",
                self.repository,
                output_root=self.root / "weekly-output",
            )

    def test_weekly_review_rejects_tampered_source_record(self):
        daily = self.seed_daily("tampered", "2026-08-17", result_status="hit")
        connection = sqlite3.connect(self.root / "review.sqlite")
        try:
            connection.execute("DROP TRIGGER observations_reject_update")
            connection.execute(
                "UPDATE observations SET payload_json = ? WHERE observation_id = ?",
                (
                    json.dumps(
                        {"hypothesis": "被篡改的假设", "review_deadline": "2026-08-17"},
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    daily["observation_id"],
                ),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ReviewRunDomainError, "存储内容哈希不一致"):
            create_weekly_review_run(
                "2026-08-17",
                "2026-08-17",
                self.repository,
                output_root=self.root / "weekly-output",
            )

    def test_weekly_review_rejects_cross_publication_observation_binding(self):
        first = self.seed_daily("binding-first", "2026-08-17", result_status=None)
        second = self.seed_daily("binding-second", "2026-08-18", result_status=None)
        self.repository.create_observation(
            "observation-cross-binding",
            first["run_id"],
            first["review_revision_id"],
            "plan-cross-binding",
            {
                "hypothesis": "错误地混合两份日报来源",
                "review_deadline": "2026-08-18",
            },
            action_set_id=second["action_set_id"],
        )

        with self.assertRaisesRegex(
            ReviewRunRepositoryError,
            "同一份正式日报",
        ):
            create_weekly_review_run(
                "2026-08-17",
                "2026-08-18",
                self.repository,
                output_root=self.root / "weekly-output",
            )

    def test_daily_publication_change_after_snapshot_rejects_weekly_publish(self):
        daily = self.seed_daily("daily-race", "2026-08-17", result_status="hit")

        def change_daily_publication():
            self.repository.publish_review(
                "publication-daily-race-new",
                "2026-08-17",
                daily["run_id"],
                daily["review_revision_id"],
                "report-hash-daily-race-new",
                {"blob_key": "daily/daily-race-new.md"},
                expected_publication_id=daily["publication_id"],
                action_set_id=daily["action_set_id"],
            )

        self.assert_interleaved_source_change_is_rejected(change_daily_publication)

    def test_observation_result_change_after_snapshot_rejects_weekly_publish(self):
        daily = self.seed_daily("result-race", "2026-08-17", result_status="hit")

        def change_observation_result():
            self.repository.append_observation_result_revision(
                daily["observation_id"],
                "result-result-race-new",
                "miss",
                {"actual_result": "冻结后结果发生变化"},
                expected_result_revision_id=daily["result_revision_id"],
            )

        self.assert_interleaved_source_change_is_rejected(change_observation_result)

    def test_pool_head_change_after_snapshot_rejects_weekly_publish(self):
        daily = self.seed_daily("pool-race", "2026-08-17", result_status="hit")

        def change_pool_head():
            self.repository.append_pool_status_event(
                "pool-event-pool-race-new",
                "watch",
                "code-pool-race",
                "paused",
                {"reason": "冻结后池状态发生变化"},
                expected_event_id=daily["pool_event_id"],
            )

        self.assert_interleaved_source_change_is_rejected(change_pool_head)

    def assert_interleaved_source_change_is_rejected(self, change_source):
        original_publish = self.repository.publish_weekly_revision

        def publish_after_change(*args, **kwargs):
            change_source()
            return original_publish(*args, **kwargs)

        with patch.object(
            self.repository,
            "publish_weekly_revision",
            side_effect=publish_after_change,
        ):
            with self.assertRaisesRegex(
                OptimisticConcurrencyError,
                "来源指针.*变化",
            ):
                create_weekly_review_run(
                    "2026-08-17",
                    "2026-08-17",
                    self.repository,
                    output_root=self.root / "weekly-output",
                )
        self.assertIsNone(
            self.repository.get_current_weekly_publication(
                "2026-08-17",
                "2026-08-17",
            )
        )

    def seed_daily(
        self,
        suffix: str,
        trade_date: str,
        *,
        result_status: str | None,
    ) -> dict[str, str | None]:
        run_id = f"run-{suffix}"
        manifest_id = f"manifest-{suffix}"
        bundle_id = f"bundle-{suffix}"
        revision_id = f"revision-{suffix}"
        action_set_id = f"action-set-{suffix}"
        publication_id = f"publication-{suffix}"
        observation_id = f"observation-{suffix}"
        result_revision_id = (
            f"result-{suffix}" if result_status is not None else None
        )
        pool_event_id = f"pool-event-{suffix}"
        self.repository.create_review_run(run_id, trade_date, {"mode": "weekly-fixture"})
        self.repository.save_run_manifest(manifest_id, run_id, {"captures": []})
        self.repository.save_evidence_bundle(
            bundle_id,
            run_id,
            manifest_id,
            {"sealed": True},
        )
        self.repository.save_review_revision(
            revision_id,
            run_id,
            bundle_id,
            {
                "framework_version": "stock-review-v1",
                "framework_hash": f"framework-hash-{suffix}",
                "program_version": "commit-test",
                "renderer_version": "review-run-v1",
                "model_response_hashes": [],
            },
        )
        self.repository.save_action_set(
            action_set_id,
            run_id,
            revision_id,
            manifest_id,
            {"fixture": suffix},
        )
        self.repository.publish_review(
            publication_id,
            trade_date,
            run_id,
            revision_id,
            f"report-hash-{suffix}",
            {"blob_key": f"daily/{suffix}.md"},
            expected_publication_id=None,
            action_set_id=action_set_id,
        )
        self.repository.append_pool_status_event(
            pool_event_id,
            "watch",
            f"code-{suffix}",
            "active",
            {"reason": "正式日报动作"},
            expected_event_id=None,
            action_set_id=action_set_id,
        )
        self.repository.create_observation(
            observation_id,
            run_id,
            revision_id,
            f"plan-{suffix}",
            {
                "hypothesis": f"假设-{suffix}",
                "review_deadline": trade_date,
            },
            action_set_id=action_set_id,
        )
        if result_revision_id is not None:
            self.repository.append_observation_result_revision(
                observation_id,
                result_revision_id,
                result_status,
                {"actual_result": f"结果-{suffix}"},
                expected_result_revision_id=None,
            )
        return {
            "publication_id": publication_id,
            "run_id": run_id,
            "review_revision_id": revision_id,
            "action_set_id": action_set_id,
            "observation_id": observation_id,
            "result_revision_id": result_revision_id,
            "pool_event_id": pool_event_id,
        }


if __name__ == "__main__":
    unittest.main()
