from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from stock_review.storage.review_run_repository import (
    ImmutableRecordError,
    OptimisticConcurrencyError,
    RecordNotFoundError,
    ReviewRunRepository,
    ReviewRunRepositoryError,
)


class ReviewRunRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "review_run.sqlite"
        self.repository = ReviewRunRepository(self.database_path)
        self.repository.initialize_schema()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_schema_and_review_artifacts_are_immutable_and_round_trip(self):
        records = self.seed_run("base")

        first_event = self.repository.append_review_run_event(
            records["run_id"],
            "event-base-started",
            "run_started",
            {"operation_id": "operation-start"},
            expected_previous_event_id=None,
        )
        replayed_event = self.repository.append_review_run_event(
            records["run_id"],
            "event-base-started",
            "run_started",
            {"operation_id": "operation-start"},
            expected_previous_event_id=None,
        )
        with self.assertRaises(ImmutableRecordError):
            self.repository.append_review_run_event(
                records["run_id"],
                "event-base-started",
                "run_started",
                {"operation_id": "operation-changed"},
                expected_previous_event_id=None,
            )
        second_event = self.repository.append_review_run_event(
            records["run_id"],
            "event-base-evidence",
            "evidence_ready",
            {"evidence_bundle_id": records["evidence_bundle_id"]},
            expected_previous_event_id="event-base-started",
        )

        self.assertEqual(self.repository.list_schema_versions()[0]["version"], 1)
        self.assertEqual(first_event["sequence_number"], 1)
        self.assertEqual(replayed_event["sequence_number"], 1)
        self.assertEqual(second_event["sequence_number"], 2)
        self.assertEqual(
            self.repository.get_run_manifest(records["manifest_id"])["payload"],
            {"captures": ["market"]},
        )
        self.assertEqual(
            self.repository.get_evidence_capture(records["evidence_capture_id"])[
                "payload"
            ]["status"],
            "available",
        )
        self.assertEqual(
            self.repository.get_evidence_bundle(records["evidence_bundle_id"])[
                "payload"
            ]["capture_ids"],
            [records["evidence_capture_id"]],
        )
        self.assertEqual(
            self.repository.get_review_revision(records["review_revision_id"])[
                "payload"
            ]["steps"],
            [1, 2, 3],
        )
        self.assertEqual(
            self.repository.get_finalization_proposal(records["proposal_id"])[
                "payload"
            ]["decision"],
            "non_participation",
        )
        self.assertEqual(
            self.repository.get_action_set(records["action_set_id"])["payload"][
                "audit"
            ]["event_type"],
            "daily.finalized",
        )

        with self.assertRaises(OptimisticConcurrencyError):
            self.repository.append_review_run_event(
                records["run_id"],
                "event-base-stale",
                "draft_ready",
                {},
                expected_previous_event_id="event-base-started",
            )

        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE review_run_events SET event_type = 'changed' WHERE event_id = ?",
                    ("event-base-started",),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM review_revisions WHERE review_revision_id = ?",
                    (records["review_revision_id"],),
                )
        finally:
            connection.close()

    def test_legacy_import_registration_is_idempotent_and_filterable(self):
        first = self.repository.register_legacy_import(
            "evidence_snapshot",
            "2026-08-17",
            "legacy",
            {"path": "data/evidence/2026-08-17_snapshot.json"},
            imported_at="2026-09-05T00:00:00+00:00",
        )
        replayed = self.repository.register_legacy_import(
            "evidence_snapshot",
            "2026-08-17",
            "legacy",
            {"path": "data/evidence/2026-08-17_snapshot.json"},
        )
        self.repository.register_legacy_import(
            "daily_report",
            "reports/daily/2026-08-17_detailed_review.md",
            "superseded",
            {"sha256": "report-hash"},
        )

        self.assertEqual(first["content_hash"], replayed["content_hash"])
        self.assertEqual(
            self.repository.list_legacy_imports(source_kind="evidence_snapshot")[0][
                "source_key"
            ],
            "2026-08-17",
        )
        self.assertEqual(
            len(self.repository.list_legacy_imports(classification="superseded")),
            1,
        )
        with self.assertRaises(ImmutableRecordError):
            self.repository.register_legacy_import(
                "evidence_snapshot",
                "2026-08-17",
                "system_validation",
                {"path": "data/evidence/2026-08-17_snapshot.json"},
            )

    def test_initialize_schema_rejects_unmigrated_observation_table(self):
        legacy_path = Path(self.temporary_directory.name) / "legacy.sqlite"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                "CREATE TABLE observations (observation_id TEXT PRIMARY KEY, status TEXT)"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            ReviewRunRepositoryError, "显式 ReviewRun 迁移"
        ):
            ReviewRunRepository(legacy_path).initialize_schema()

        connection = sqlite3.connect(legacy_path)
        try:
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertEqual(table_names, {"observations"})

    def test_model_request_and_response_are_fingerprint_bound_and_immutable(self):
        records = self.seed_run("model")
        request = self.repository.save_model_request(
            "model-request-main",
            records["run_id"],
            records["evidence_bundle_id"],
            records["review_revision_id"],
            records["review_revision_id"],
            "request-fingerprint-main",
            {"messages": [{"role": "user", "content": "生成复盘"}]},
        )
        replayed_request = self.repository.save_model_request(
            "model-request-main",
            records["run_id"],
            records["evidence_bundle_id"],
            records["review_revision_id"],
            records["review_revision_id"],
            "request-fingerprint-main",
            {"messages": [{"content": "生成复盘", "role": "user"}]},
        )
        response = self.repository.save_model_response(
            "model-response-main",
            "model-request-main",
            "request-fingerprint-main",
            {"steps": {"1": "市场事实"}, "model_id": "fake-model"},
        )
        replayed_response = self.repository.save_model_response(
            "model-response-main",
            "model-request-main",
            "request-fingerprint-main",
            {"model_id": "fake-model", "steps": {"1": "市场事实"}},
        )

        self.assertEqual(request["content_hash"], replayed_request["content_hash"])
        self.assertEqual(
            request["expected_current_revision_id"], records["review_revision_id"]
        )
        self.assertEqual(
            self.repository.get_model_request("model-request-main")[
                "request_fingerprint"
            ],
            "request-fingerprint-main",
        )
        self.assertEqual(response["content_hash"], replayed_response["content_hash"])
        self.assertEqual(response["run_id"], records["run_id"])
        self.assertEqual(
            self.repository.get_model_response("model-response-main")[
                "evidence_bundle_id"
            ],
            records["evidence_bundle_id"],
        )

        with self.assertRaisesRegex(ReviewRunRepositoryError, "请求指纹"):
            self.repository.save_model_response(
                "model-response-wrong-fingerprint",
                "model-request-main",
                "request-fingerprint-wrong",
                {"steps": {}},
            )
        with self.assertRaises(ImmutableRecordError):
            self.repository.save_model_request(
                "model-request-main",
                records["run_id"],
                records["evidence_bundle_id"],
                records["review_revision_id"],
                records["review_revision_id"],
                "request-fingerprint-main",
                {"messages": []},
            )
        with self.assertRaises(ImmutableRecordError):
            self.repository.save_model_response(
                "model-response-main",
                "model-request-main",
                "request-fingerprint-main",
                {"steps": {"1": "被篡改"}},
            )
        self.assertIsNone(
            self.repository.get_model_response("model-response-wrong-fingerprint")
        )

    def test_review_publication_supersede_requires_expected_current_id(self):
        first_run = self.seed_run("first", trade_date="2026-08-17")
        second_run = self.seed_run("second", trade_date="2026-08-17")
        self.repository.publish_review(
            "publication-first",
            "2026-08-17",
            first_run["run_id"],
            first_run["review_revision_id"],
            "report-hash-first",
            {"path": "blobs/report-hash-first.md"},
            expected_publication_id=None,
        )

        with self.assertRaises(OptimisticConcurrencyError):
            self.repository.publish_review(
                "publication-stale",
                "2026-08-17",
                second_run["run_id"],
                second_run["review_revision_id"],
                "report-hash-stale",
                {"path": "blobs/report-hash-stale.md"},
                expected_publication_id=None,
            )

        self.assertEqual(
            self.repository.get_current_review_publication("2026-08-17")[
                "publication_id"
            ],
            "publication-first",
        )
        self.assertEqual(
            len(self.repository.list_review_publications("2026-08-17")), 1
        )

        second_publication = self.repository.publish_review(
            "publication-second",
            "2026-08-17",
            second_run["run_id"],
            second_run["review_revision_id"],
            "report-hash-second",
            {"path": "blobs/report-hash-second.md"},
            expected_publication_id="publication-first",
        )

        self.assertEqual(
            second_publication["supersedes_publication_id"], "publication-first"
        )
        self.assertEqual(
            self.repository.get_current_review_publication("2026-08-17")[
                "publication_id"
            ],
            "publication-second",
        )
        self.assertEqual(
            len(self.repository.list_review_publications("2026-08-17")), 2
        )

    def test_current_review_publications_are_filtered_by_inclusive_date_range(self):
        before = self.seed_run("publication-before", trade_date="2026-08-14")
        middle = self.seed_run("publication-middle", trade_date="2026-08-17")
        after = self.seed_run("publication-after", trade_date="2026-08-18")
        replacement = self.seed_run(
            "publication-replacement", trade_date="2026-08-17"
        )
        for publication_id, records in (
            ("publication-before", before),
            ("publication-middle", middle),
            ("publication-after", after),
        ):
            self.repository.publish_review(
                publication_id,
                self.repository.get_review_run(records["run_id"])["trade_date"],
                records["run_id"],
                records["review_revision_id"],
                f"hash-{publication_id}",
                {"path": f"{publication_id}.md"},
                expected_publication_id=None,
            )
        self.repository.publish_review(
            "publication-middle-current",
            "2026-08-17",
            replacement["run_id"],
            replacement["review_revision_id"],
            "hash-publication-middle-current",
            {"path": "publication-middle-current.md"},
            expected_publication_id="publication-middle",
        )

        publications = self.repository.list_current_review_publications(
            start_date="2026-08-17", end_date="2026-08-18"
        )

        self.assertEqual(
            [item["publication_id"] for item in publications],
            ["publication-middle-current", "publication-after"],
        )
        self.assertNotIn(
            "publication-middle",
            [item["publication_id"] for item in publications],
        )

    def test_observation_results_are_append_only_and_require_expected_head(self):
        records = self.seed_run("observation")
        observation = self.repository.create_observation(
            "observation-mainline",
            records["run_id"],
            records["review_revision_id"],
            "plan-item-mainline",
            {
                "hypothesis": "主线延续",
                "confirmation_condition": "次日仍在同源榜前列",
                "invalidation_condition": "退出同源榜单",
                "review_deadline": "2026-08-18",
            },
        )

        self.assertNotIn("status", observation["payload"])
        self.assertEqual(
            self.repository.get_current_observation_result("observation-mainline")[
                "status"
            ],
            "pending",
        )
        first_result = self.repository.append_observation_result_revision(
            "observation-mainline",
            "result-mainline-first",
            "hit",
            {"actual_result": "仍在榜单前列", "evidence": ["snapshot-next"]},
            expected_result_revision_id=None,
        )
        with self.assertRaises(OptimisticConcurrencyError):
            self.repository.append_observation_result_revision(
                "observation-mainline",
                "result-mainline-stale",
                "miss",
                {"actual_result": "旧客户端回填"},
                expected_result_revision_id=None,
            )
        corrected = self.repository.append_observation_result_revision(
            "observation-mainline",
            "result-mainline-corrected",
            "invalid",
            {"actual_result": "行情源日期错配", "reason": "样本无效"},
            expected_result_revision_id="result-mainline-first",
        )

        self.assertEqual(first_result["status"], "hit")
        self.assertEqual(
            corrected["supersedes_result_revision_id"], "result-mainline-first"
        )
        self.assertEqual(
            self.repository.get_current_observation_result("observation-mainline")[
                "result_revision_id"
            ],
            "result-mainline-corrected",
        )
        self.assertEqual(
            len(
                self.repository.list_observation_result_revisions(
                    "observation-mainline"
                )
            ),
            2,
        )
        with self.assertRaises(ReviewRunRepositoryError):
            self.repository.create_observation(
                "observation-invalid-base",
                records["run_id"],
                records["review_revision_id"],
                "plan-item-invalid",
                {"hypothesis": "错误示例", "status": "pending"},
            )

    def test_observation_listing_filters_dates_and_projects_current_results(self):
        first_run = self.seed_run("observation-list-first", "2026-08-17")
        second_run = self.seed_run("observation-list-second", "2026-08-18")
        self.repository.create_observation(
            "observation-list-pending",
            first_run["run_id"],
            first_run["review_revision_id"],
            "plan-list-pending",
            {"hypothesis": "等待验证", "review_deadline": "2026-08-18"},
        )
        self.repository.create_observation(
            "observation-list-completed",
            first_run["run_id"],
            first_run["review_revision_id"],
            "plan-list-completed",
            {"hypothesis": "已验证", "review_deadline": "2026-08-19"},
        )
        self.repository.append_observation_result_revision(
            "observation-list-completed",
            "result-list-completed",
            "hit",
            {"actual_result": "成立条件出现"},
            expected_result_revision_id=None,
        )
        self.repository.create_observation(
            "observation-list-next-date",
            second_run["run_id"],
            second_run["review_revision_id"],
            "plan-list-next-date",
            {"hypothesis": "下一日样本", "review_deadline": "2026-08-18"},
        )

        first_date = self.repository.list_observations(review_date="2026-08-17")
        due = self.repository.list_observations(
            due_on_or_before="2026-08-18"
        )
        next_date_due = self.repository.list_observations(
            review_date="2026-08-18", due_on_or_before="2026-08-18"
        )

        first_date_by_id = {item["observation_id"]: item for item in first_date}
        self.assertEqual(set(first_date_by_id), {
            "observation-list-pending",
            "observation-list-completed",
        })
        self.assertEqual(
            first_date_by_id["observation-list-pending"]["current_result"][
                "status"
            ],
            "pending",
        )
        self.assertEqual(
            first_date_by_id["observation-list-completed"]["current_result"][
                "status"
            ],
            "hit",
        )
        self.assertEqual(
            {item["observation_id"] for item in due},
            {"observation-list-pending", "observation-list-next-date"},
        )
        self.assertEqual(
            [item["observation_id"] for item in next_date_due],
            ["observation-list-next-date"],
        )

    def test_action_set_version_conflict_rolls_back_all_effects(self):
        existing_run = self.seed_run("existing", trade_date="2026-08-17")
        final_run = self.seed_run("final", trade_date="2026-08-17")
        self.repository.publish_review(
            "publication-existing",
            "2026-08-17",
            existing_run["run_id"],
            existing_run["review_revision_id"],
            "report-existing",
            {},
            expected_publication_id=None,
        )
        self.repository.append_pool_status_event(
            "pool-event-existing",
            "hot",
            "600206",
            "active",
            {"reason": "用户确认"},
            expected_event_id=None,
        )
        self.repository.create_observation(
            "observation-due",
            existing_run["run_id"],
            existing_run["review_revision_id"],
            "plan-item-due",
            {"hypothesis": "次日延续"},
        )
        self.repository.append_observation_result_revision(
            "observation-due",
            "result-due-existing",
            "hit",
            {"actual_result": "首次回填"},
            expected_result_revision_id=None,
        )
        stale_payload = self.build_action_set_payload(
            final_run,
            action_suffix="stale",
            expected_result_revision_id="result-that-never-existed",
        )
        self.repository.save_action_set(
            "action-set-stale",
            final_run["run_id"],
            final_run["review_revision_id"],
            final_run["manifest_id"],
            stale_payload,
        )

        with self.assertRaises(OptimisticConcurrencyError):
            self.repository.apply_action_set("action-set-stale")

        self.assertEqual(
            self.repository.get_current_review_publication("2026-08-17")[
                "publication_id"
            ],
            "publication-existing",
        )
        self.assertEqual(
            self.repository.get_current_pool_status("hot", "600206")["event_id"],
            "pool-event-existing",
        )
        self.assertIsNone(self.repository.get_observation("observation-created-stale"))
        self.assertIsNone(self.repository.get_audit_event("audit-stale"))
        self.assertEqual(self.repository.list_pending_outbox(), [])
        self.assertEqual(
            len(self.repository.list_review_publications("2026-08-17")), 1
        )
        self.assertEqual(
            len(self.repository.list_pool_status_events("hot", "600206")), 1
        )
        self.assertEqual(
            len(
                self.repository.list_observation_result_revisions(
                    "observation-due"
                )
            ),
            1,
        )

        stale_pool_payload = self.build_action_set_payload(
            final_run,
            action_suffix="stale-pool",
            expected_result_revision_id="result-due-existing",
        )
        stale_pool_payload["pool_actions"][0]["expected_event_id"] = (
            "pool-event-that-never-existed"
        )
        self.repository.save_action_set(
            "action-set-stale-pool",
            final_run["run_id"],
            final_run["review_revision_id"],
            final_run["manifest_id"],
            stale_pool_payload,
        )
        with self.assertRaises(OptimisticConcurrencyError):
            self.repository.apply_action_set("action-set-stale-pool")
        self.assertIsNone(
            next(
                (
                    item
                    for item in self.repository.list_review_publications(
                        "2026-08-17"
                    )
                    if item["publication_id"] == "publication-stale-pool"
                ),
                None,
            )
        )
        self.assertIsNone(self.repository.get_audit_event("audit-stale-pool"))

        valid_payload = self.build_action_set_payload(
            final_run,
            action_suffix="valid",
            expected_result_revision_id="result-due-existing",
        )
        self.repository.save_action_set(
            "action-set-valid",
            final_run["run_id"],
            final_run["review_revision_id"],
            final_run["manifest_id"],
            valid_payload,
        )
        applied = self.repository.apply_action_set("action-set-valid")
        replayed = self.repository.apply_action_set("action-set-valid")

        self.assertFalse(applied["replayed"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(
            self.repository.get_current_review_publication("2026-08-17")[
                "publication_id"
            ],
            "publication-valid",
        )
        self.assertEqual(
            self.repository.get_current_pool_status("hot", "600206")["event_id"],
            "pool-event-valid",
        )
        self.assertIsNotNone(self.repository.get_observation("observation-created-valid"))
        self.assertEqual(
            self.repository.get_current_observation_result("observation-due")[
                "result_revision_id"
            ],
            "result-valid",
        )
        self.assertEqual(
            [item["outbox_id"] for item in self.repository.list_pending_outbox()],
            ["outbox-valid"],
        )
        connection = sqlite3.connect(self.database_path)
        try:
            application_count = connection.execute(
                "SELECT COUNT(*) FROM action_set_applications WHERE action_set_id = ?",
                ("action-set-valid",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(application_count, 1)

    def test_weekly_revision_keeps_pinned_inputs_across_later_changes(self):
        records = self.seed_run("weekly", trade_date="2026-08-17")
        self.repository.publish_review(
            "publication-weekly-source",
            "2026-08-17",
            records["run_id"],
            records["review_revision_id"],
            "daily-report-hash",
            {},
            expected_publication_id=None,
        )
        self.repository.append_pool_status_event(
            "pool-event-weekly-source",
            "watch",
            "002428",
            "active",
            {},
            expected_event_id=None,
        )
        self.repository.create_observation(
            "observation-weekly-source",
            records["run_id"],
            records["review_revision_id"],
            "plan-item-weekly-source",
            {"hypothesis": "方向延续"},
        )
        self.repository.append_observation_result_revision(
            "observation-weekly-source",
            "result-weekly-source",
            "hit",
            {"actual_result": "满足"},
            expected_result_revision_id=None,
        )
        first_source_heads = self.repository.load_weekly_source_snapshot(
            "2026-08-17", "2026-08-21"
        )["source_heads"]
        weekly_revision = self.repository.save_weekly_revision(
            "weekly-revision-first",
            "2026-08-17",
            "2026-08-21",
            {
                "framework_hash": "framework-v1",
                "program_version": "commit-a",
                "source_heads": first_source_heads,
            },
            review_publication_ids=["publication-weekly-source"],
            observation_result_revision_ids=["result-weekly-source"],
            pool_status_event_ids=["pool-event-weekly-source"],
        )
        self.repository.publish_weekly_revision(
            "weekly-publication-first",
            "2026-08-17",
            "2026-08-21",
            "weekly-revision-first",
            "weekly-report-first",
            {},
            expected_weekly_publication_id=None,
            expected_source_heads=first_source_heads,
        )

        self.repository.append_observation_result_revision(
            "observation-weekly-source",
            "result-weekly-corrected",
            "invalid",
            {"actual_result": "后续判定样本无效"},
            expected_result_revision_id="result-weekly-source",
        )
        self.repository.append_pool_status_event(
            "pool-event-weekly-later",
            "watch",
            "002428",
            "paused",
            {},
            expected_event_id="pool-event-weekly-source",
        )

        stored_revision = self.repository.get_weekly_revision(
            "weekly-revision-first"
        )
        self.assertEqual(
            weekly_revision["observation_result_revision_ids"],
            ["result-weekly-source"],
        )
        self.assertEqual(
            stored_revision["pool_status_event_ids"],
            ["pool-event-weekly-source"],
        )
        with self.assertRaises(RecordNotFoundError):
            self.repository.save_weekly_revision(
                "weekly-revision-missing-pin",
                "2026-08-17",
                "2026-08-21",
                {},
                review_publication_ids=["publication-missing"],
            )
        self.assertIsNone(
            self.repository.get_weekly_revision("weekly-revision-missing-pin")
        )

        second_source_heads = self.repository.load_weekly_source_snapshot(
            "2026-08-17", "2026-08-21"
        )["source_heads"]
        second_revision = self.repository.save_weekly_revision(
            "weekly-revision-second",
            "2026-08-17",
            "2026-08-21",
            {
                "framework_hash": "framework-v1",
                "program_version": "commit-b",
                "source_heads": second_source_heads,
            },
            review_publication_ids=["publication-weekly-source"],
            observation_result_revision_ids=["result-weekly-corrected"],
            pool_status_event_ids=["pool-event-weekly-later"],
        )
        with self.assertRaises(OptimisticConcurrencyError):
            self.repository.publish_weekly_revision(
                "weekly-publication-stale",
                "2026-08-17",
                "2026-08-21",
                second_revision["weekly_revision_id"],
                "weekly-report-stale",
                {},
                expected_weekly_publication_id=None,
                expected_source_heads=second_source_heads,
            )
        self.repository.publish_weekly_revision(
            "weekly-publication-second",
            "2026-08-17",
            "2026-08-21",
            second_revision["weekly_revision_id"],
            "weekly-report-second",
            {},
            expected_weekly_publication_id="weekly-publication-first",
            expected_source_heads=second_source_heads,
        )
        self.assertEqual(
            self.repository.get_current_weekly_publication(
                "2026-08-17", "2026-08-21"
            )["weekly_publication_id"],
            "weekly-publication-second",
        )

    def test_weekly_publication_cannot_omit_source_heads(self):
        records = self.seed_run("weekly-heads", trade_date="2026-08-17")
        self.repository.publish_review(
            "publication-weekly-heads",
            "2026-08-17",
            records["run_id"],
            records["review_revision_id"],
            "daily-report-heads",
            {},
            expected_publication_id=None,
        )
        source_heads = self.repository.load_weekly_source_snapshot(
            "2026-08-17", "2026-08-21"
        )["source_heads"]
        self.repository.save_weekly_revision(
            "weekly-revision-heads",
            "2026-08-17",
            "2026-08-21",
            {"source_heads": source_heads},
            review_publication_ids=["publication-weekly-heads"],
        )

        with self.assertRaisesRegex(TypeError, "expected_source_heads"):
            self.repository.publish_weekly_revision(
                "weekly-publication-heads",
                "2026-08-17",
                "2026-08-21",
                "weekly-revision-heads",
                "weekly-report-heads",
                {},
                expected_weekly_publication_id=None,
            )
        self.assertIsNone(
            self.repository.get_current_weekly_publication(
                "2026-08-17", "2026-08-21"
            )
        )

    def test_audit_outbox_is_atomic_and_delivery_is_idempotent(self):
        records = self.seed_run("audit")
        first = self.repository.append_audit_event(
            "audit-standalone",
            "observation.result_recorded",
            {"command": "observation record-result"},
            "outbox-standalone",
            {"line": "结果已回填"},
            run_id=records["run_id"],
        )
        replayed = self.repository.append_audit_event(
            "audit-standalone",
            "observation.result_recorded",
            {"command": "observation record-result"},
            "outbox-standalone",
            {"line": "结果已回填"},
            run_id=records["run_id"],
        )

        self.assertEqual(first["audit_event_id"], replayed["audit_event_id"])
        self.assertEqual(
            self.repository.list_pending_outbox()[0]["payload"],
            {"line": "结果已回填"},
        )
        delivered = self.repository.mark_outbox_delivered(
            "outbox-standalone", "2026-09-05T12:00:00+00:00"
        )
        delivered_again = self.repository.mark_outbox_delivered("outbox-standalone")
        replayed_after_delivery = self.repository.append_audit_event(
            "audit-standalone",
            "observation.result_recorded",
            {"command": "observation record-result"},
            "outbox-standalone",
            {"line": "结果已回填"},
            run_id=records["run_id"],
        )

        self.assertEqual(delivered["delivered_at"], delivered_again["delivered_at"])
        self.assertEqual(
            replayed_after_delivery["audit_event_id"], "audit-standalone"
        )
        self.assertEqual(self.repository.list_pending_outbox(), [])
        self.assertEqual(
            self.repository.get_audit_event("audit-standalone")["payload"][
                "command"
            ],
            "observation record-result",
        )

    def seed_run(
        self, suffix: str, trade_date: str = "2026-08-17"
    ) -> dict[str, str]:
        records = {
            "run_id": f"run-{suffix}",
            "manifest_id": f"manifest-{suffix}",
            "evidence_capture_id": f"capture-{suffix}",
            "evidence_bundle_id": f"bundle-{suffix}",
            "review_revision_id": f"revision-{suffix}",
            "proposal_id": f"proposal-{suffix}",
            "action_set_id": f"action-set-seed-{suffix}",
        }
        self.repository.create_review_run(
            records["run_id"], trade_date, {"framework_hash": "framework-hash"}
        )
        self.repository.save_run_manifest(
            records["manifest_id"],
            records["run_id"],
            {"captures": ["market"]},
        )
        self.repository.save_evidence_capture(
            records["evidence_capture_id"],
            records["run_id"],
            records["manifest_id"],
            {"source": "fake", "status": "available"},
        )
        self.repository.save_evidence_bundle(
            records["evidence_bundle_id"],
            records["run_id"],
            records["manifest_id"],
            {"capture_ids": [records["evidence_capture_id"]]},
        )
        self.repository.save_review_revision(
            records["review_revision_id"],
            records["run_id"],
            records["evidence_bundle_id"],
            {"steps": [1, 2, 3], "approved_plan_items": []},
        )
        self.repository.save_finalization_proposal(
            records["proposal_id"],
            records["run_id"],
            records["review_revision_id"],
            {"decision": "non_participation"},
        )
        self.repository.save_action_set(
            records["action_set_id"],
            records["run_id"],
            records["review_revision_id"],
            records["manifest_id"],
            {
                "publication": None,
                "pool_actions": [],
                "observation_creates": [],
                "observation_result_actions": [],
                "audit": {
                    "audit_event_id": f"audit-seed-{suffix}",
                    "event_type": "daily.finalized",
                    "payload": {},
                    "outbox_id": f"outbox-seed-{suffix}",
                    "outbox_payload": {},
                },
            },
            proposal_id=records["proposal_id"],
        )
        return records

    def build_action_set_payload(
        self,
        run: dict[str, str],
        action_suffix: str,
        expected_result_revision_id: str,
    ) -> dict[str, object]:
        return {
            "publication": {
                "publication_id": f"publication-{action_suffix}",
                "trade_date": "2026-08-17",
                "run_id": run["run_id"],
                "review_revision_id": run["review_revision_id"],
                "report_hash": f"report-{action_suffix}",
                "expected_publication_id": "publication-existing",
                "payload": {"path": f"blobs/report-{action_suffix}.md"},
            },
            "pool_actions": [
                {
                    "event_id": f"pool-event-{action_suffix}",
                    "pool_type": "hot",
                    "code": "600206",
                    "status": "paused",
                    "expected_event_id": "pool-event-existing",
                    "payload": {"reason": "最终动作确认"},
                }
            ],
            "observation_creates": [
                {
                    "observation_id": f"observation-created-{action_suffix}",
                    "review_revision_id": run["review_revision_id"],
                    "plan_item_id": f"plan-item-{action_suffix}",
                    "payload": {
                        "hypothesis": "次日反馈可验证",
                        "review_deadline": "2026-08-18",
                    },
                }
            ],
            "observation_result_actions": [
                {
                    "observation_id": "observation-due",
                    "result_revision_id": f"result-{action_suffix}",
                    "status": "miss",
                    "expected_result_revision_id": expected_result_revision_id,
                    "payload": {
                        "actual_result": "条件未出现",
                        "reason": "判断错误",
                    },
                }
            ],
            "audit": {
                "audit_event_id": f"audit-{action_suffix}",
                "event_type": "daily.finalized",
                "payload": {"command": "daily finalize"},
                "outbox_id": f"outbox-{action_suffix}",
                "outbox_payload": {"line": f"finalized {action_suffix}"},
            },
        }


if __name__ == "__main__":
    unittest.main()
