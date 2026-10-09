# 本文件用临时 SQLite 和假证据验证模型请求指纹、受限投影、迟到响应与幂等。

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from stock_review.review_runs.canonical_hash import canonical_hash
from stock_review.review_runs.complete_daily_review import complete_local_review
from stock_review.review_runs.complete_model_review import (
    complete_model_review,
    prepare_model_review,
)
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.plan_daily_review import plan_daily_review
from stock_review.review_runs.review_run_event import ReviewRunEvent
from stock_review.review_runs.run_daily_review import (
    load_run_projection,
    seal_daily_evidence,
    start_daily_review,
)
from stock_review.review_runs.verify_daily_review import verify_daily_review
from stock_review.storage.review_run_repository import ReviewRunRepository


class SequentialIds:
    def __init__(self, start: int = 0) -> None:
        self.value = start

    def __call__(self) -> UUID:
        self.value += 1
        return UUID(int=self.value)


class CompleteModelReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.now = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
        self.repository = ReviewRunRepository(self.root / "review.sqlite")
        self.manifest = plan_daily_review(
            "2026-09-04",
            ("fake:snapshot",),
            evidence_dir=self.root / "evidence",
            output_dir=self.root / "reports",
            database_path=self.root / "review.sqlite",
            model_ids=("review-model",),
            now=self.now,
            manifest_lifetime=timedelta(minutes=30),
            id_factory=SequentialIds(),
        )
        start_daily_review(
            self.manifest,
            self.manifest.content_hash,
            self.repository,
            collectors={"fake": self.fake_collector},
            now=self.now,
            event_id_factory=SequentialIds(100),
        )
        seal_daily_evidence(
            self.manifest.run_id,
            self.manifest.trade_date,
            self.manifest.content_hash,
            self.repository,
            now=self.now,
            event_id_factory=SequentialIds(200),
        )

    def test_request_and_response_are_fingerprint_bound_and_idempotent(self):
        request = self.prepare("OP-model-request")
        replayed_request = self.prepare(
            "OP-model-request",
            now=self.now + timedelta(minutes=5),
        )
        unsigned_request = {
            key: value for key, value in request.items() if key != "request_fingerprint"
        }
        request_record = self.repository.get_model_request(request["request_id"])

        self.assertEqual(request, replayed_request)
        self.assertEqual(canonical_hash(unsigned_request), request["request_fingerprint"])
        self.assertEqual(2, request["schema_version"])
        frozen_input = request["frozen_input"]
        self.assertNotIn("normalized_evidence", frozen_input)
        self.assertEqual(
            [str(step_number) for step_number in range(1, 11)],
            list(frozen_input["normalized_evidence_by_step"]),
        )
        self.assertEqual(
            3800,
            frozen_input["normalized_evidence_by_step"]["1"][0]["normalized"]
            ["market"]["indices"][0]["close"],
        )
        self.assertNotIn(
            "stocks",
            frozen_input["normalized_evidence_by_step"]["1"][0]["normalized"],
        )
        self.assertNotIn(
            "schema_version",
            frozen_input["normalized_evidence_by_step"]["1"][0]["normalized"],
        )
        self.assertNotIn(
            "quality",
            frozen_input["normalized_evidence_by_step"]["1"][0]["normalized"],
        )
        self.assertEqual(
            "平安银行",
            frozen_input["normalized_evidence_by_step"]["6"][0]["normalized"]
            ["stocks"][0]["name"],
        )
        self.assertEqual(request["request_fingerprint"], request_record["request_fingerprint"])
        self.assertEqual(canonical_hash(request), request_record["content_hash"])
        requested_events = self.events_of_type("model_requested")
        self.assertEqual(1, len(requested_events))
        self.assertEqual(
            request["request_fingerprint"],
            requested_events[0].model_request_hash,
        )

        response = self.valid_response("模型只归纳冻结证据。")
        revision = self.complete(
            request,
            response,
            operation_id="OP-model-complete",
        )
        replayed_revision = self.complete(
            request,
            response,
            operation_id="OP-model-complete",
            now=self.now + timedelta(minutes=10),
        )

        response_hash = canonical_hash(response)
        response_id = self.stable_id("MODELRESPONSE", "OP-model-complete")
        response_record = self.repository.get_model_response(response_id)

        self.assertEqual(revision, replayed_revision)
        self.assertIn(response_hash, revision.model_response_hashes)
        self.assertEqual(request["request_fingerprint"], response_record["request_fingerprint"])
        self.assertEqual(response_hash, response_record["payload"]["response_hash"])
        self.assertEqual(canonical_hash(response_record["payload"]), response_record["content_hash"])
        self.assertEqual(1, len(self.events_of_type("model_completed")))
        self.assertEqual(1, len(self.events_of_type("revision_added")))

        verified = verify_daily_review(self.manifest.run_id, self.repository)
        self.assertTrue(verified.valid, verified.to_dict())

        connection = sqlite3.connect(self.root / "review.sqlite")
        try:
            connection.execute("DROP TRIGGER model_responses_reject_delete")
            connection.execute("DELETE FROM model_responses")
            connection.commit()
        finally:
            connection.close()
        missing_response = verify_daily_review(self.manifest.run_id, self.repository)
        model_check = next(
            check for check in missing_response.checks if check.name == "model_artifacts"
        )
        self.assertFalse(model_check.ok)
        self.assertIn("数量不一致", model_check.detail)

    def test_copied_database_cannot_save_model_request_or_response(self):
        request_clone_path = self.root / "request-clone.sqlite"
        shutil.copy2(self.repository.database_path, request_clone_path)
        request_clone = ReviewRunRepository(request_clone_path)
        with self.assertRaisesRegex(ReviewRunDomainError, "实际 SQLite 路径"):
            prepare_model_review(
                self.manifest.run_id,
                self.manifest.trade_date,
                None,
                None,
                "review-model",
                "OP-clone-request",
                request_clone,
                now=self.now,
            )

        request = self.prepare("OP-original-request")
        response_clone_path = self.root / "response-clone.sqlite"
        shutil.copy2(self.repository.database_path, response_clone_path)
        response_clone = ReviewRunRepository(response_clone_path)
        with self.assertRaisesRegex(ReviewRunDomainError, "实际 SQLite 路径"):
            complete_model_review(
                self.manifest.run_id,
                self.manifest.trade_date,
                request["request_id"],
                None,
                None,
                self.valid_response("复制库不得保存模型响应。"),
                self.manifest.content_hash,
                "OP-clone-response",
                response_clone,
                framework_path=Path("stock-review.md"),
                now=self.now,
            )

    def test_expired_manifest_blocks_model_request_and_response_writes(self):
        expired_request_operation = "OP-expired-manifest-request"
        with self.assertRaisesRegex(ReviewRunDomainError, "RunManifest 已过期"):
            self.prepare(
                expired_request_operation,
                now=self.now + timedelta(minutes=30),
            )
        self.assertIsNone(
            self.repository.get_model_request(
                self.stable_id("MODELREQUEST", expired_request_operation)
            )
        )

        request = self.prepare("OP-before-manifest-expiry")
        expired_response_operation = "OP-expired-manifest-response"
        with self.assertRaisesRegex(ReviewRunDomainError, "RunManifest 已过期"):
            self.complete(
                request,
                self.valid_response("过期授权不能保存模型响应。"),
                operation_id=expired_response_operation,
                now=self.now + timedelta(minutes=30),
            )

        self.assertIsNone(
            self.repository.get_model_response(
                self.stable_id("MODELRESPONSE", expired_response_operation)
            )
        )
        self.assertIsNone(
            self.repository.get_review_revision(
                self.stable_id("REV", expired_response_operation)
            )
        )

    def test_verify_rejects_tampered_model_response_relation_binding(self):
        request = self.prepare("OP-model-relation-request")
        revision = self.complete(
            request,
            self.valid_response("模型归纳冻结证据。"),
            operation_id="OP-model-relation-complete",
        )
        response_id = self.stable_id(
            "MODELRESPONSE",
            "OP-model-relation-complete",
        )
        connection = sqlite3.connect(self.root / "review.sqlite")
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DROP TRIGGER model_responses_reject_update")
            connection.execute(
                """
                UPDATE model_responses SET expected_current_revision_id = ?
                WHERE model_response_id = ?
                """,
                (revision.review_revision_id, response_id),
            )
            connection.commit()
        finally:
            connection.close()

        verified = verify_daily_review(self.manifest.run_id, self.repository)
        model_check = next(
            check for check in verified.checks if check.name == "model_artifacts"
        )
        self.assertFalse(model_check.ok)
        self.assertIn("绑定不一致", model_check.detail)

    def test_verify_rejects_tampered_model_response_payload_id(self):
        request = self.prepare("OP-model-payload-request")
        self.complete(
            request,
            self.valid_response("模型归纳冻结证据。"),
            operation_id="OP-model-payload-complete",
        )
        response_id = self.stable_id(
            "MODELRESPONSE",
            "OP-model-payload-complete",
        )
        record = self.repository.get_model_response(response_id)
        payload = {**record["payload"], "model_response_id": "MODELRESPONSE-forged"}
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection = sqlite3.connect(self.root / "review.sqlite")
        try:
            connection.execute("DROP TRIGGER model_responses_reject_update")
            connection.execute(
                """
                UPDATE model_responses SET payload_json = ?, content_hash = ?
                WHERE model_response_id = ?
                """,
                (payload_json, canonical_hash(payload), response_id),
            )
            connection.commit()
        finally:
            connection.close()

        verified = verify_daily_review(self.manifest.run_id, self.repository)
        model_check = next(
            check for check in verified.checks if check.name == "model_artifacts"
        )
        self.assertFalse(model_check.ok)
        self.assertIn("绑定不一致", model_check.detail)

    def test_model_response_projects_only_allowed_step_fields(self):
        base = self.local_revision("OP-local-base", None)
        request = self.prepare(
            "OP-model-on-base",
            base_revision_id=base.review_revision_id,
        )
        original = base.steps[0]
        response = {
            "steps": [
                {
                    "step_number": 1,
                    "conclusion": "模型归纳后的结论。",
                    "counter_evidence": ["反向证据仍需人工核对。"],
                    "open_questions": ["次日是否延续？"],
                }
            ]
        }

        revision = self.complete(
            request,
            response,
            base_revision_id=base.review_revision_id,
            operation_id="OP-model-projection",
        )
        projected = revision.steps[0]

        self.assertEqual(base.review_revision_id, revision.replaces_review_revision_id)
        self.assertEqual("模型归纳后的结论。", projected.conclusion)
        self.assertEqual(("反向证据仍需人工核对。",), projected.counter_evidence)
        self.assertEqual(("次日是否延续？",), projected.open_questions)
        for field in (
            "title",
            "status",
            "evidence_references",
            "next_day_checks",
            "direction_ids",
            "object_ids",
            "plan_item_ids",
            "integrity_issues",
            "evidence_gaps",
        ):
            self.assertEqual(getattr(original, field), getattr(projected, field))
        self.assertEqual(base.candidate_plan_items, revision.candidate_plan_items)
        self.assertEqual(base.approved_plan_items, revision.approved_plan_items)
        self.assertEqual(base.decision_note, revision.decision_note)
        self.assertEqual(base.steps[1:], revision.steps[1:])
        self.assertIn("缺少实际可用的两市量能", projected.evidence_gaps)

        # 单步模型成功不能替代其余 STEP 的业务修订，也不能绕过不参与发布门禁。
        from stock_review.review_runs.finalize_daily_review import propose_daily_actions

        with self.assertRaisesRegex(ReviewRunDomainError, "通用模板"):
            propose_daily_actions(
                self.manifest.run_id, revision.review_revision_id,
                {"participation_decision": "non_participation", "decision_note": "量能不足，暂不参与。",
                 "approved_plan_item_ids": [], "rejected_plan_items": {}},
                self.repository, output_dir=self.root / "reports", now=self.now,
            )

    def test_model_response_cannot_add_decision_objects_thresholds_or_plans(self):
        request = self.prepare("OP-model-boundary")
        invalid_responses = (
            {
                "steps": [{"step_number": 1, "conclusion": "结论"}],
                "candidate_plan_items": [],
            },
            {
                "steps": [
                    {
                        "step_number": 1,
                        "conclusion": "结论",
                        "object_ids": ["new-object"],
                    }
                ]
            },
            {
                "steps": [{"step_number": 1, "conclusion": "结论"}],
                "decision_note": "模型自行改变决策。",
            },
        )

        for position, response in enumerate(invalid_responses, start=1):
            with self.subTest(position=position):
                with self.assertRaisesRegex(ReviewRunDomainError, "未授权字段|只能修改 STEP"):
                    self.complete(
                        request,
                        response,
                        operation_id=f"OP-invalid-{position}",
                    )

        self.assertFalse(self.events_of_type("model_completed"))
        self.assertFalse(self.events_of_type("revision_added"))
        self.assertEqual("pending", load_run_projection(self.repository, self.manifest.run_id).model_state)

    def test_model_response_rejects_loose_types_and_unfrozen_numbers(self):
        request = self.prepare("OP-model-fact-boundary")
        invalid_responses = (
            {"steps": [{"step_number": True, "conclusion": "结论"}]},
            {"steps": [{"step_number": 1.0, "conclusion": "结论"}]},
            {
                "steps": [
                    {
                        "step_number": 1,
                        "conclusion": "结论",
                        "counter_evidence": [42],
                    }
                ]
            },
            {
                "steps": [
                    {
                        "step_number": 1,
                        "conclusion": "上证指数收于 9999 点。",
                        "counter_evidence": [],
                        "open_questions": [],
                    }
                ]
            },
        )

        for position, response in enumerate(invalid_responses, start=1):
            with self.subTest(position=position):
                with self.assertRaises(ReviewRunDomainError):
                    self.complete(
                        request,
                        response,
                        operation_id=f"OP-fact-invalid-{position}",
                    )

        valid = self.complete(
            request,
            self.valid_response("上证指数冻结收盘值为 3800 点。"),
            operation_id="OP-fact-valid",
        )
        self.assertEqual("上证指数冻结收盘值为 3800 点。", valid.steps[0].conclusion)

    def test_model_response_rejects_unfrozen_entities_and_assertive_labels(self):
        request = self.prepare("OP-model-semantic-boundary")
        invalid_conclusions = (
            ("贵州茅台是今日唯一主线龙头，已形成强势共识。", "实体或事实断言"),
            ("贵州茅台收于 3800 点。", "实体或事实断言"),
            ("贵州茅台今日表现优异。", "实体或事实断言"),
            ("市场上涨60家。", "代码或数值"),
            ("市场上涨1家。", "代码或数值"),
            ("市场上涨4家。", "代码或数值"),
            ("市场上涨9家。", "代码或数值"),
            ("平安银行是核心。", "实体或事实断言"),
        )

        for position, (conclusion, error_text) in enumerate(
            invalid_conclusions,
            start=1,
        ):
            with self.subTest(conclusion=conclusion):
                with self.assertRaisesRegex(
                    ReviewRunDomainError,
                    error_text,
                ):
                    self.complete(
                        request,
                        self.valid_response(conclusion),
                        operation_id=f"OP-model-semantic-invalid-{position}",
                    )

        self.assertFalse(self.events_of_type("model_completed"))
        self.assertFalse(self.events_of_type("revision_added"))

        uncertain_response = self.valid_response(
            "2026-09-04 上证指数冻结收盘值为 3800 点。"
        )
        uncertain_response["steps"][0]["open_questions"] = [
            "市场上涨家数是否仍待确认？"
        ]
        revision = self.complete(
            request,
            uncertain_response,
            operation_id="OP-model-semantic-uncertain",
        )
        self.assertEqual(
            ("市场上涨家数是否仍待确认？",),
            revision.steps[0].open_questions,
        )

    def test_late_response_is_rejected_before_response_or_revision_is_saved(self):
        request = self.prepare("OP-model-late-request")
        current = self.local_revision("OP-human-revision", None)

        with self.assertRaisesRegex(ReviewRunDomainError, "迟到响应"):
            self.complete(
                request,
                self.valid_response("迟到的模型结论。"),
                operation_id="OP-model-late-complete",
            )

        response_id = self.stable_id("MODELRESPONSE", "OP-model-late-complete")
        self.assertIsNone(self.repository.get_model_response(response_id))
        self.assertFalse(self.events_of_type("model_completed"))
        self.assertEqual(
            current.review_revision_id,
            load_run_projection(self.repository, self.manifest.run_id).current_review_revision_id,
        )

    def test_without_base_uses_conservative_skeleton_and_adds_one_revision_event(self):
        request = self.prepare("OP-model-no-base")
        revision = self.complete(
            request,
            self.valid_response("只改变第一步结论。"),
            operation_id="OP-model-no-base-complete",
        )

        self.assertIsNone(revision.replaces_review_revision_id)
        self.assertEqual(tuple(range(1, 11)), tuple(step.step_number for step in revision.steps))
        self.assertEqual("只改变第一步结论。", revision.steps[0].conclusion)
        self.assertTrue(all(step.status == "incomplete" for step in revision.steps))
        self.assertFalse(revision.candidate_plan_items)
        self.assertEqual(1, len(self.events_of_type("revision_added")))
        projection = load_run_projection(self.repository, self.manifest.run_id)
        self.assertEqual("draft_ready", projection.state)
        self.assertEqual(revision.review_revision_id, projection.current_review_revision_id)

    def test_retry_completes_revision_event_after_interrupted_first_attempt(self):
        request = self.prepare("OP-model-recovery-request")
        response = self.valid_response("可重试的模型结论。")
        original_append = self.repository.append_review_run_event

        def interrupt_revision_event(*args, **kwargs):
            if args[2] == "revision_added":
                raise RuntimeError("模拟 revision 事件追加中断")
            return original_append(*args, **kwargs)

        with patch.object(
            self.repository,
            "append_review_run_event",
            side_effect=interrupt_revision_event,
        ):
            with self.assertRaisesRegex(RuntimeError, "模拟 revision"):
                self.complete(
                    request,
                    response,
                    operation_id="OP-model-recovery-complete",
                )

        revision = self.complete(
            request,
            response,
            operation_id="OP-model-recovery-complete",
        )
        self.assertEqual(1, len(self.events_of_type("model_completed")))
        self.assertEqual(1, len(self.events_of_type("revision_added")))
        self.assertEqual(
            revision.review_revision_id,
            load_run_projection(self.repository, self.manifest.run_id).current_review_revision_id,
        )

    def prepare(
        self,
        operation_id,
        base_revision_id=None,
        now=None,
    ):
        return prepare_model_review(
            self.manifest.run_id,
            self.manifest.trade_date,
            base_revision_id,
            base_revision_id,
            "review-model",
            operation_id,
            self.repository,
            now=now or self.now,
        )

    def complete(
        self,
        request,
        response,
        *,
        operation_id,
        base_revision_id=None,
        now=None,
    ):
        return complete_model_review(
            self.manifest.run_id,
            self.manifest.trade_date,
            request["request_id"],
            base_revision_id,
            base_revision_id,
            response,
            self.manifest.content_hash,
            operation_id,
            self.repository,
            framework_path=Path("stock-review.md"),
            now=now or self.now,
        )

    def local_revision(self, operation_id, expected_revision_id):
        return complete_local_review(
            self.manifest.run_id,
            self.manifest.trade_date,
            expected_revision_id,
            self.manifest.content_hash,
            operation_id,
            self.repository,
            framework_path=Path("stock-review.md"),
            now=self.now,
        )

    def events_of_type(self, event_type):
        return [
            ReviewRunEvent.from_dict(record["payload"])
            for record in self.repository.list_review_run_events(self.manifest.run_id)
            if record["event_type"] == event_type
        ]

    def stable_id(self, prefix, operation_id):
        digest = canonical_hash(
            {"run_id": self.manifest.run_id, "operation_id": operation_id}
        )[:24]
        return f"{prefix}-{digest}"

    @staticmethod
    def valid_response(conclusion):
        return {
            "steps": [
                {
                    "step_number": 1,
                    "conclusion": conclusion,
                    "counter_evidence": [],
                    "open_questions": ["需要人工确认。"],
                }
            ]
        }

    @staticmethod
    def fake_collector(trade_date, _slot):
        return {
            "schema_version": 1,
            "source": "fake",
            "sample_date": trade_date,
            "quality": {"available_count": 1},
            "market": {"indices": [{"name": "上证指数", "close": 3800}]},
            "sentiment": {},
            "sectors": [],
            "stocks": [
                {
                    "code": "000001",
                    "name": "平安银行",
                    "exchange": "SZSE",
                    "sector_id": "banking",
                    "role": "容量核心",
                    "role_source": "fake:stocks",
                }
            ],
        }


if __name__ == "__main__":
    unittest.main()
