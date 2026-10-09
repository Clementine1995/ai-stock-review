# 本文件验证可行动日报发布后，计划项按同一事务进入 Observation 并以追加修订回填结果。

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from stock_review.observations.review_due_observations import (
    list_due_observations,
    propose_observation_result,
    record_observation_result,
)
from stock_review.review_runs.complete_daily_review import complete_local_review
from stock_review.review_runs.canonical_hash import canonical_hash
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.finalize_daily_review import (
    finalize_daily_review,
    propose_daily_actions,
)
from stock_review.review_runs.plan_daily_review import plan_daily_review
from stock_review.review_runs.review_revision import (
    PlanItem,
    ScenarioBand,
    Step9PatternAssessment,
    StepAssessment,
)
from stock_review.review_runs.revise_daily_review import revise_daily_review
from stock_review.review_runs.run_daily_review import seal_daily_evidence, start_daily_review
from stock_review.scoring.verify_step9_patterns import STEP9_PATTERN_DEFINITIONS
from stock_review.storage.review_run_repository import (
    OptimisticConcurrencyError,
    ReviewRunRepository,
)


class SequentialIds:
    def __init__(self, start: int = 0) -> None:
        self.value = start

    def __call__(self) -> UUID:
        self.value += 1
        return UUID(int=self.value)


class ReviewRunActionableObservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repository = ReviewRunRepository(self.root / "review.sqlite")
        self.now = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
        self.manifest, self.revision, self.evidence_reference = self.create_actionable_draft()

    def test_actionable_publication_atomically_creates_pending_observation(self):
        proposal, _ = propose_daily_actions(
            self.manifest.run_id,
            self.revision.review_revision_id,
            self.actionable_selections(),
            self.repository,
            output_dir=self.root / "reports",
            now=self.now,
        )
        observation_action = next(
            action
            for action in proposal.action_set.actions
            if action.action_type == "create_observation"
        )
        self.assertNotIn("status", observation_action.payload)
        self.assertIsNone(self.repository.get_observation(observation_action.target_id))

        def fail_before_apply(stage):
            if stage == "before_apply":
                raise RuntimeError("模拟联合事务前故障")

        with self.assertRaisesRegex(RuntimeError, "模拟联合事务前故障"):
            finalize_daily_review(
                proposal,
                proposal.content_hash,
                self.repository,
                now=self.now,
                failure_injector=fail_before_apply,
            )
        self.assertIsNone(
            self.repository.get_current_review_publication(self.manifest.trade_date)
        )
        self.assertIsNone(self.repository.get_observation(observation_action.target_id))

        result = finalize_daily_review(
            proposal,
            proposal.content_hash,
            self.repository,
            now=self.now,
        )
        self.assertEqual([observation_action.target_id], result["observation_ids"])
        observation = self.repository.get_observation(observation_action.target_id)
        self.assertEqual(proposal.action_set.action_set_id, observation["action_set_id"])
        self.assertNotIn("status", observation["payload"])
        due = list_due_observations(self.repository, "2026-09-07")
        self.assertEqual(1, len(due))
        self.assertEqual("pending", due[0]["current_result"]["status"])

    def test_complete_multidomain_snapshot_passes_step_evidence_matrix(self):
        proposal, _ = propose_daily_actions(
            self.manifest.run_id,
            self.revision.review_revision_id,
            self.actionable_selections(),
            self.repository,
            output_dir=self.root / "reports",
            now=self.now,
        )

        self.assertEqual("actionable", proposal.final_revision.participation_decision)

    def test_single_bar_does_not_satisfy_continuous_history_capabilities(self):
        repository = ReviewRunRepository(self.root / "single-bar-history.sqlite")
        manifest, revision, _reference = self.create_actionable_draft(
            repository=repository,
            collector=self.fake_single_bar_history_collector,
            suffix="single-bar-history",
        )

        with self.assertRaisesRegex(
            ReviewRunDomainError,
            "板块连续比较|candidate_history",
        ):
            propose_daily_actions(
                manifest.run_id,
                revision.review_revision_id,
                self.actionable_selections(),
                repository,
                output_dir=self.root / "reports",
                now=self.now,
            )

    def test_capability_only_patterns_cannot_publish_actionable(self):
        steps = tuple(replace(step, pattern_assessments=tuple(
            replace(pattern, status="base_evidence_available_trigger_pending")
            for pattern in step.pattern_assessments
        )) if step.step_number == 9 else step for step in self.revision.steps)
        revised = revise_daily_review(
            self.manifest.run_id, self.revision.review_revision_id, self.revision.review_revision_id,
            {"steps": [step.to_dict() for step in steps],
             "candidate_plan_items": [plan.to_dict() for plan in self.revision.candidate_plan_items],
             "decision_note": "材料存在，但背景仍待核验。"},
            "OP-capability-only", self.repository, now=self.now,
        )
        with self.assertRaisesRegex(ReviewRunDomainError, "只有能力材料"):
            propose_daily_actions(self.manifest.run_id, revised.review_revision_id,
                                  self.actionable_selections(), self.repository,
                                  output_dir=self.root / "reports", now=self.now)

    def test_non_participation_rejects_assessment_pending_with_material_gaps(self):
        steps = tuple(
            replace(
                step,
                status="incomplete",
                pattern_assessments=tuple(
                    replace(pattern, status="assessment_pending")
                    if pattern.pattern_number == 1
                    else pattern
                    for pattern in step.pattern_assessments
                ),
            )
            if step.step_number == 9
            else step
            for step in self.revision.steps
        )
        revised = revise_daily_review(
            self.manifest.run_id,
            self.revision.review_revision_id,
            self.revision.review_revision_id,
            {
                "steps": [step.to_dict() for step in steps],
                "candidate_plan_items": [
                    plan.to_dict() for plan in self.revision.candidate_plan_items
                ],
                "decision_note": "模式材料仍有缺口，测试状态一致性。",
            },
            "OP-assessment-pending-with-gaps",
            self.repository,
            now=self.now,
        )

        with self.assertRaisesRegex(
            ReviewRunDomainError,
            "模式 1 仍有证据缺口，不能标记 assessment_pending",
        ):
            propose_daily_actions(
                self.manifest.run_id,
                revised.review_revision_id,
                {
                    "participation_decision": "non_participation",
                    "decision_note": "材料不足，用户确认次日不参与。",
                    "approved_plan_item_ids": [],
                    "rejected_plan_items": {"PLAN-bank": "材料不足"},
                },
                self.repository,
                output_dir=self.root / "reports",
                now=self.now,
            )

    def test_pattern_rejects_capability_that_is_falsely_declared_missing(self):
        steps = tuple(
            replace(
                step,
                pattern_assessments=tuple(
                    replace(
                        pattern,
                        missing_evidence=(*pattern.missing_evidence, "daily_kline"),
                    )
                    if pattern.pattern_number == 1
                    else pattern
                    for pattern in step.pattern_assessments
                ),
            )
            if step.step_number == 9
            else step
            for step in self.revision.steps
        )
        revised = revise_daily_review(
            self.manifest.run_id,
            self.revision.review_revision_id,
            self.revision.review_revision_id,
            {
                "steps": [step.to_dict() for step in steps],
                "candidate_plan_items": [
                    plan.to_dict() for plan in self.revision.candidate_plan_items
                ],
                "decision_note": "测试已具备能力不能继续列为缺口。",
            },
            "OP-stale-pattern-gap",
            self.repository,
            now=self.now,
        )

        with self.assertRaisesRegex(ReviewRunDomainError, "已经具备的证据错误标记为缺口"):
            propose_daily_actions(
                self.manifest.run_id,
                revised.review_revision_id,
                self.actionable_selections(),
                self.repository,
                output_dir=self.root / "reports",
                now=self.now,
            )

    def test_pattern_matched_object_must_come_from_its_frozen_references(self):
        steps = tuple(
            replace(
                step,
                object_ids=("stock-000001", "stock-999999"),
                pattern_assessments=tuple(
                    replace(pattern, object_ids=("stock-999999",))
                    if pattern.pattern_number == 1
                    else pattern
                    for pattern in step.pattern_assessments
                ),
            )
            if step.step_number == 9
            else step
            for step in self.revision.steps
        )
        revised = revise_daily_review(
            self.manifest.run_id,
            self.revision.review_revision_id,
            self.revision.review_revision_id,
            {
                "steps": [step.to_dict() for step in steps],
                "candidate_plan_items": [
                    plan.to_dict() for plan in self.revision.candidate_plan_items
                ],
                "decision_note": "测试模式对象必须来自冻结证据。",
            },
            "OP-unfrozen-pattern-object",
            self.repository,
            now=self.now,
        )

        with self.assertRaisesRegex(
            ReviewRunDomainError,
            "对象没有来自本模式引用的冻结股票或候选证据.*stock-999999",
        ):
            propose_daily_actions(
                self.manifest.run_id,
                revised.review_revision_id,
                self.actionable_selections(),
                self.repository,
                output_dir=self.root / "reports",
                now=self.now,
            )

    def test_step_nine_complete_state_requires_twelve_structured_results(self):
        steps = tuple(
            replace(
                step,
                conclusion="十二模式均已完成核验。",
                object_ids=(),
                plan_item_ids=(),
                pattern_assessments=(),
            )
            if step.step_number == 9
            else step
            for step in self.revision.steps
        )

        with self.assertRaisesRegex(ReviewRunDomainError, "十二模式逐项核验"):
            revise_daily_review(
                self.manifest.run_id,
                self.revision.review_revision_id,
                self.revision.review_revision_id,
                {
                    "steps": [step.to_dict() for step in steps],
                    "candidate_plan_items": [
                        item.to_dict() for item in self.revision.candidate_plan_items
                    ],
                    "decision_note": "拒绝用泛化结论代替逐项核验。",
                },
                "OP-revise-step-nine-generic",
                self.repository,
                now=self.now,
            )

    def test_step_nine_confirmed_patterns_require_own_trigger_evidence(self):
        steps = tuple(
            replace(
                step,
                pattern_assessments=tuple(
                    replace(pattern, status="confirmed", missing_evidence=())
                    for pattern in step.pattern_assessments
                ),
            )
            if step.step_number == 9
            else step
            for step in self.revision.steps
        )
        revision = revise_daily_review(
            self.manifest.run_id,
            self.revision.review_revision_id,
            self.revision.review_revision_id,
            {
                "steps": [step.to_dict() for step in steps],
                "candidate_plan_items": [
                    item.to_dict() for item in self.revision.candidate_plan_items
                ],
                "decision_note": "模拟把十二模式全部错误标记为确认。",
            },
            "OP-revise-step-nine-confirmed",
            self.repository,
            now=self.now,
        )

        with self.assertRaisesRegex(
            ReviewRunDomainError,
            "模式 1.*不能标记 confirmed",
        ):
            propose_daily_actions(
                self.manifest.run_id,
                revision.review_revision_id,
                self.actionable_selections(),
                self.repository,
                output_dir=self.root / "reports",
                now=self.now,
            )

    def test_step_nine_pattern_cannot_borrow_step_level_capabilities(self):
        steps = tuple(
            replace(
                step,
                pattern_assessments=tuple(
                    replace(pattern, evidence_references=())
                    if pattern.pattern_number == 1
                    else pattern
                    for pattern in step.pattern_assessments
                ),
            )
            if step.step_number == 9
            else step
            for step in self.revision.steps
        )
        revision = revise_daily_review(
            self.manifest.run_id,
            self.revision.review_revision_id,
            self.revision.review_revision_id,
            {
                "steps": [step.to_dict() for step in steps],
                "candidate_plan_items": [
                    item.to_dict() for item in self.revision.candidate_plan_items
                ],
                "decision_note": "模拟模式自身没有引用却借用 STEP 全局能力。",
            },
            "OP-revise-step-nine-borrowed",
            self.repository,
            now=self.now,
        )

        with self.assertRaisesRegex(
            ReviewRunDomainError,
            "模式 1 未记录实际证据缺口",
        ):
            propose_daily_actions(
                self.manifest.run_id,
                revision.review_revision_id,
                self.actionable_selections(),
                self.repository,
                output_dir=self.root / "reports",
                now=self.now,
            )

    def test_actionable_rejects_one_stock_capture_for_every_step(self):
        repository = ReviewRunRepository(self.root / "stock-only.sqlite")
        manifest, revision, _reference = self.create_actionable_draft(
            repository=repository,
            capture_specs=("fake:stocks",),
            collector=self.fake_stock_only_collector,
            suffix="stock-only",
        )

        with self.assertRaisesRegex(
            ReviewRunDomainError,
            "STEP 最小证据矩阵.*STEP 1",
        ):
            propose_daily_actions(
                manifest.run_id,
                revision.review_revision_id,
                self.actionable_selections(),
                repository,
                output_dir=self.root / "reports",
                now=self.now,
            )

    def test_empty_snapshot_does_not_supply_missing_market_or_sector_fields(self):
        repository = ReviewRunRepository(self.root / "empty-snapshot.sqlite")
        manifest, revision, _reference = self.create_actionable_draft(
            repository=repository,
            capture_specs=("fake:snapshot", "fake:stocks"),
            collector=self.fake_empty_snapshot_with_stock_collector,
            suffix="empty-snapshot",
        )

        with self.assertRaisesRegex(
            ReviewRunDomainError,
            "STEP 最小证据矩阵.*STEP 1",
        ):
            propose_daily_actions(
                manifest.run_id,
                revision.review_revision_id,
                self.actionable_selections(),
                repository,
                output_dir=self.root / "reports",
                now=self.now,
            )

    def test_empty_snapshot_cannot_publish_as_non_participation(self):
        repository = ReviewRunRepository(self.root / "empty-non-participation.sqlite")
        manifest = plan_daily_review(
            "2026-09-04",
            ("fake:snapshot",),
            evidence_dir=self.root / "evidence-empty-non-participation",
            output_dir=self.root / "reports",
            database_path=repository.database_path,
            now=self.now,
            manifest_lifetime=timedelta(minutes=30),
            id_factory=SequentialIds(),
        )
        start_daily_review(
            manifest,
            manifest.content_hash,
            repository,
            collectors={"fake": self.fake_empty_snapshot_collector},
            now=self.now,
            event_id_factory=SequentialIds(),
        )
        seal_daily_evidence(
            manifest.run_id,
            manifest.trade_date,
            manifest.content_hash,
            repository,
            now=self.now,
            event_id_factory=SequentialIds(),
        )
        draft = complete_local_review(
            manifest.run_id,
            manifest.trade_date,
            None,
            manifest.content_hash,
            "OP-local-empty-non-participation",
            repository,
            framework_path=Path("stock-review.md"),
            now=self.now,
        )

        draft = revise_daily_review(
            manifest.run_id, draft.review_revision_id, draft.review_revision_id,
            {"steps": [dict(step.to_dict(), conclusion=f"{step.title}：" + "；".join(step.evidence_gaps),
                            open_questions=[]) for step in draft.steps],
             "candidate_plan_items": [], "decision_note": "空快照不支持交易判断。"},
            "OP-reviewed-empty-non-participation", repository, now=self.now,
        )

        with self.assertRaisesRegex(
            ReviewRunDomainError,
            "non_participation 的核心分析 STEP 1 未达到可发布条件",
        ):
            propose_daily_actions(
                manifest.run_id,
                draft.review_revision_id,
                {
                    "participation_decision": "non_participation",
                    "decision_note": "没有足够证据，不能形成正式不参与结论。",
                    "approved_plan_item_ids": [],
                    "rejected_plan_items": {},
                },
                repository,
                output_dir=self.root / "reports",
                now=self.now,
            )

        self.assertIsNone(repository.get_current_review_publication(manifest.trade_date))

    def test_proposal_reloads_capture_file_before_building_actions(self):
        self.tamper_first_capture(self.repository, self.manifest)

        with self.assertRaisesRegex(ReviewRunDomainError, "采集证据文件"):
            propose_daily_actions(
                self.manifest.run_id,
                self.revision.review_revision_id,
                self.actionable_selections(),
                self.repository,
                output_dir=self.root / "reports",
                now=self.now,
            )

    def test_finalize_reloads_capture_file_after_proposal(self):
        proposal, _ = propose_daily_actions(
            self.manifest.run_id,
            self.revision.review_revision_id,
            self.actionable_selections(),
            self.repository,
            output_dir=self.root / "reports",
            now=self.now,
        )
        self.tamper_first_capture(self.repository, self.manifest)

        with self.assertRaisesRegex(ReviewRunDomainError, "采集证据文件"):
            finalize_daily_review(
                proposal,
                proposal.content_hash,
                self.repository,
                now=self.now,
            )

        self.assertIsNone(
            self.repository.get_current_review_publication(self.manifest.trade_date)
        )

    def test_result_correction_preserves_history_and_replay_is_idempotent(self):
        observation_id = self.publish_actionable_review()
        hit_action = propose_observation_result(
            observation_id,
            None,
            "hit",
            "次日竞价与板块量能均符合计划。",
            "",
            (self.evidence_reference,),
            self.repository,
            now=self.now,
        )
        first = record_observation_result(
            hit_action,
            hit_action["content_hash"],
            self.repository,
            now=self.now + timedelta(minutes=1),
        )
        self.assertFalse(first["replayed"])

        replayed = record_observation_result(
            hit_action,
            hit_action["content_hash"],
            self.repository,
            now=self.now + timedelta(days=1),
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(first["result_revision_ids"], replayed["result_revision_ids"])

        hit_revision_id = first["result_revision_ids"][0]
        invalid_action = propose_observation_result(
            observation_id,
            hit_revision_id,
            "invalid",
            "次日停牌，原假设无法形成可比较样本。",
            "invalid_sample",
            (self.evidence_reference,),
            self.repository,
            now=self.now + timedelta(minutes=3),
        )
        corrected = record_observation_result(
            invalid_action,
            invalid_action["content_hash"],
            self.repository,
            now=self.now + timedelta(minutes=4),
        )
        invalid_revision_id = corrected["result_revision_ids"][0]
        self.assertNotEqual(hit_revision_id, invalid_revision_id)
        history = self.repository.list_observation_result_revisions(observation_id)
        self.assertEqual([hit_revision_id, invalid_revision_id], [item["result_revision_id"] for item in history])
        current = self.repository.get_current_observation_result(observation_id)
        self.assertEqual("invalid", current["status"])
        self.assertEqual(hit_revision_id, current["supersedes_result_revision_id"])

    def test_expired_staged_result_action_does_not_block_same_result_reproposal(self):
        observation_id = self.publish_actionable_review()
        first_action = propose_observation_result(
            observation_id,
            None,
            "hit",
            "竞价符合预期。",
            "",
            (self.evidence_reference,),
            self.repository,
            now=self.now,
        )
        with patch.object(
            self.repository,
            "apply_action_set",
            side_effect=RuntimeError("模拟结果动作暂存后故障"),
        ):
            with self.assertRaisesRegex(RuntimeError, "模拟结果动作暂存后故障"):
                record_observation_result(
                    first_action,
                    first_action["content_hash"],
                    self.repository,
                    now=self.now + timedelta(minutes=1),
                )

        second_action = propose_observation_result(
            observation_id,
            None,
            "hit",
            "竞价符合预期。",
            "",
            (self.evidence_reference,),
            self.repository,
            now=self.now + timedelta(minutes=30),
        )
        self.assertNotEqual(first_action["action_set_id"], second_action["action_set_id"])

        result = record_observation_result(
            second_action,
            second_action["content_hash"],
            self.repository,
            now=self.now + timedelta(minutes=31),
        )

        self.assertFalse(result["replayed"])
        self.assertEqual(
            [second_action["result_revision_id"]],
            result["result_revision_ids"],
        )

    def test_copied_database_cannot_receive_observation_result(self):
        observation_id = self.publish_actionable_review()
        action = propose_observation_result(
            observation_id,
            None,
            "hit",
            "竞价符合预期。",
            "",
            (self.evidence_reference,),
            self.repository,
            now=self.now,
        )
        clone_path = self.root / "clone.sqlite"
        shutil.copy2(self.repository.database_path, clone_path)
        clone_repository = ReviewRunRepository(clone_path)

        with self.assertRaisesRegex(ReviewRunDomainError, "实际 SQLite 路径"):
            record_observation_result(
                action,
                action["content_hash"],
                clone_repository,
                now=self.now + timedelta(minutes=1),
            )
        self.assertEqual(
            [],
            clone_repository.list_observation_result_revisions(observation_id),
        )

    def test_stale_expected_revision_rejects_the_whole_result_action(self):
        observation_id = self.publish_actionable_review()
        stale_action = propose_observation_result(
            observation_id,
            None,
            "miss",
            "竞价低于失效线。",
            "judgment_error",
            (self.evidence_reference,),
            self.repository,
            now=self.now,
        )
        hit_action = propose_observation_result(
            observation_id,
            None,
            "hit",
            "竞价符合预期。",
            "",
            (self.evidence_reference,),
            self.repository,
            now=self.now,
        )
        applied = record_observation_result(
            hit_action,
            hit_action["content_hash"],
            self.repository,
            now=self.now + timedelta(minutes=1),
        )
        audit_count = len(self.repository.list_pending_outbox())

        with self.assertRaises(OptimisticConcurrencyError):
            record_observation_result(
                stale_action,
                stale_action["content_hash"],
                self.repository,
                now=self.now + timedelta(minutes=2),
            )
        history = self.repository.list_observation_result_revisions(observation_id)
        self.assertEqual(applied["result_revision_ids"], [item["result_revision_id"] for item in history])
        self.assertEqual(audit_count, len(self.repository.list_pending_outbox()))

    def test_result_action_rejects_invalid_confirmed_creation_time(self):
        observation_id = self.publish_actionable_review()
        action = propose_observation_result(
            observation_id,
            None,
            "hit",
            "竞价符合预期。",
            "",
            (self.evidence_reference,),
            self.repository,
            now=self.now,
        )
        action["created_at"] = "not-a-time"
        action["content_hash"] = canonical_hash(
            {key: value for key, value in action.items() if key != "content_hash"}
        )

        with self.assertRaisesRegex(ReviewRunDomainError, "创建时间无效"):
            record_observation_result(
                action,
                action["content_hash"],
                self.repository,
                now=self.now + timedelta(minutes=1),
            )

        self.assertEqual(
            [],
            self.repository.list_observation_result_revisions(observation_id),
        )

    def test_revise_recovers_saved_revision_when_event_append_failed(self):
        edits = {
            "steps": [step.to_dict() for step in self.revision.steps],
            "candidate_plan_items": [
                item.to_dict() for item in self.revision.candidate_plan_items
            ],
            "decision_note": "同一冻结证据下补充确认说明。",
        }
        with patch.object(
            self.repository,
            "append_review_run_event",
            side_effect=RuntimeError("模拟修订事件失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "模拟修订事件失败"):
                revise_daily_review(
                    self.manifest.run_id,
                    self.revision.review_revision_id,
                    self.revision.review_revision_id,
                    edits,
                    "OP-revise-recovery",
                    self.repository,
                    now=self.now,
                )

        recovered = revise_daily_review(
            self.manifest.run_id,
            self.revision.review_revision_id,
            self.revision.review_revision_id,
            edits,
            "OP-revise-recovery",
            self.repository,
            now=self.now + timedelta(minutes=1),
        )
        events = self.repository.list_review_run_events(self.manifest.run_id)
        self.assertEqual(recovered.review_revision_id, events[-1]["payload"]["review_revision_id"])
        self.assertIsNotNone(self.repository.get_audit_event(f"AUDIT-{events[-1]['event_id']}"))

        changed_edits = {
            **edits,
            "decision_note": "相同 operation 不能被解释为另一份编辑。",
        }
        with self.assertRaisesRegex(ReviewRunDomainError, "不同基础修订或编辑内容"):
            revise_daily_review(
                self.manifest.run_id,
                self.revision.review_revision_id,
                self.revision.review_revision_id,
                changed_edits,
                "OP-revise-recovery",
                self.repository,
                now=self.now + timedelta(minutes=2),
            )

    def publish_actionable_review(self) -> str:
        proposal, _ = propose_daily_actions(
            self.manifest.run_id,
            self.revision.review_revision_id,
            self.actionable_selections(),
            self.repository,
            output_dir=self.root / "reports",
            now=self.now,
        )
        result = finalize_daily_review(
            proposal,
            proposal.content_hash,
            self.repository,
            now=self.now,
        )
        return result["observation_ids"][0]

    def create_actionable_draft(
        self,
        *,
        repository=None,
        capture_specs=("fake:snapshot",),
        collector=None,
        suffix="primary",
    ):
        repository = repository or self.repository
        collector = collector or self.fake_complete_snapshot_collector
        manifest = plan_daily_review(
            "2026-09-04",
            tuple(capture_specs),
            evidence_dir=self.root / f"evidence-{suffix}",
            output_dir=self.root / "reports",
            database_path=repository.database_path,
            now=self.now,
            manifest_lifetime=timedelta(minutes=30),
            id_factory=SequentialIds(),
        )
        start_daily_review(
            manifest,
            manifest.content_hash,
            repository,
            collectors={"fake": collector},
            now=self.now,
            event_id_factory=SequentialIds(),
        )
        _, bundle = seal_daily_evidence(
            manifest.run_id,
            manifest.trade_date,
            manifest.content_hash,
            repository,
            now=self.now,
            event_id_factory=SequentialIds(),
        )
        draft = complete_local_review(
            manifest.run_id,
            manifest.trade_date,
            None,
            manifest.content_hash,
            "OP-local-actionable",
            repository,
            framework_path=Path("stock-review.md"),
            now=self.now,
        )
        references = tuple(
            f"capture:{capture.slot_id}:{capture.normalized_evidence_hash}"
            for capture in bundle.captures
            if capture.status == "available"
        )
        plan_capture = next(
            (capture for capture in bundle.captures if capture.scope == "stocks"),
            bundle.captures[0],
        )
        plan_reference = (
            f"capture:{plan_capture.slot_id}:{plan_capture.normalized_evidence_hash}"
        )
        plan = self.make_plan_item(plan_reference)
        steps = self.make_complete_steps(draft.steps, references)
        revision = revise_daily_review(
            manifest.run_id,
            draft.review_revision_id,
            draft.review_revision_id,
            {
                "steps": [step.to_dict() for step in steps],
                "candidate_plan_items": [plan.to_dict()],
                "decision_note": "已核对全部证据并形成唯一候选计划。",
            },
            "OP-revise-actionable",
            repository,
            now=self.now,
        )
        return manifest, revision, plan_reference

    @staticmethod
    def make_complete_steps(base_steps, evidence_references):
        references = (
            (evidence_references,)
            if isinstance(evidence_references, str)
            else tuple(evidence_references)
        )
        steps = []
        for base in base_steps:
            number = base.step_number
            steps.append(
                StepAssessment(
                    step_number=number,
                    title=base.title,
                    status="complete",
                    conclusion=f"STEP {number} 已由用户结合冻结证据确认。",
                    evidence_references=references,
                    next_day_checks=("按同一冻结指标验证。",),
                    direction_ids=("banking",) if number in {4, 5} else (),
                    object_ids=("stock-000001",) if number in {6, 7, 8, 9, 10} else (),
                    plan_item_ids=("PLAN-bank",) if number in {8, 10} else (),
                    evidence_gaps=(
                        tuple(
                            f"模式 {definition.pattern_number} 缺少实际可用的" + "、".join(
                                {"call_auction": "竞价", "intraday": "分时",
                                 "previous_day_feedback": "前日反馈", "next_day_feedback": "次日反馈",
                                 "regulatory_event": "监管事实"}[capability]
                                for capability in definition.trigger_evidence if capability != "daily_kline"
                            ) for definition in STEP9_PATTERN_DEFINITIONS
                        ) if number == 9 else ()
                    ),
                    pattern_assessments=(
                        ReviewRunActionableObservationTest.make_pending_pattern_assessments(
                            references
                        )
                        if number == 9
                        else ()
                    ),
                )
            )
        return tuple(steps)

    @staticmethod
    def make_pending_pattern_assessments(evidence_references):
        return tuple(
            Step9PatternAssessment(
                pattern_number=definition.pattern_number,
                pattern_name=definition.name,
                status="background_matched_trigger_pending",
                object_ids=("stock-000001",),
                evidence_references=tuple(evidence_references),
                missing_evidence=tuple(
                    capability
                    for capability in definition.trigger_evidence
                    if capability != "daily_kline"
                ),
            )
            for definition in STEP9_PATTERN_DEFINITIONS
        )

    @staticmethod
    def make_plan_item(evidence_reference):
        common = {
            "source": evidence_reference,
            "metric": "auction_change_pct",
            "unit": "percent",
            "observable_at": "next_trade_day_09:25",
        }
        return PlanItem(
            plan_item_id="PLAN-bank",
            object_id="stock-000001",
            object_type="stock",
            object_code="000001",
            object_name="平安银行",
            exchange="SZSE",
            sector_id="banking",
            role="容量核心",
            role_source="fake:stocks",
            role_confirmed=True,
            role_evidence_references=(evidence_reference,),
            direction_ids=("banking",),
            independent_reason="",
            hypothesis="银行方向保持承接，容量核心强于板块中位数。",
            confirmation_condition="竞价涨幅不低于 1%，且板块量能未明显收缩。",
            invalidation_condition="竞价涨幅低于 0%，或板块量能显著收缩。",
            scenario_bands=(
                ScenarioBand(
                    scenario="abandon",
                    condition_text="竞价涨幅低于 0%",
                    upper_bound=Decimal("0"),
                    action="放弃",
                    **common,
                ),
                ScenarioBand(
                    scenario="under_expectation",
                    condition_text="竞价涨幅在 0% 到 1% 之间",
                    lower_bound=Decimal("0"),
                    upper_bound=Decimal("1"),
                    action="观察不参与",
                    **common,
                ),
                ScenarioBand(
                    scenario="expectation",
                    condition_text="竞价涨幅在 1% 到 2% 之间",
                    lower_bound=Decimal("1"),
                    upper_bound=Decimal("2"),
                    action="按计划低风险参与",
                    **common,
                ),
                ScenarioBand(
                    scenario="over_expectation",
                    condition_text="竞价涨幅不低于 2%",
                    lower_bound=Decimal("2"),
                    action="等待分歧后确认",
                    **common,
                ),
            ),
            higher_is_better=True,
            condition_universe=(),
            action_type="conditional_watch",
            max_risk=Decimal("0.5"),
            risk_unit="portfolio_percent",
            evidence_references=(evidence_reference,),
            review_deadline="2026-09-07",
        )

    @staticmethod
    def actionable_selections():
        return {
            "participation_decision": "actionable",
            "decision_note": "用户确认批准唯一计划项。",
            "approved_plan_item_ids": ["PLAN-bank"],
            "rejected_plan_items": {},
        }

    @staticmethod
    def fake_complete_snapshot_collector(trade_date, _slot):
        return {
            "source": "fake",
            "sample_date": trade_date,
            "market": {
                "indices": [{"name": "上证指数", "close": 3800}],
                "total_amount": 1200000000000,
            },
            "sentiment": {
                "limit_up_count": 60,
                "limit_down_count": 5,
                "broken_board_rate": 0.2,
                "highest_board": 4,
            },
            "sectors": [
                {
                    "sector_id": "banking",
                    "name": "银行",
                    "rank": 1,
                    "daily_bars": [
                        {"date": "2026-09-03", "change_percent": 0.8},
                        {"date": trade_date, "change_percent": 1.2},
                    ],
                }
            ],
            "stocks": [
                {
                    "code": "000001",
                    "name": "平安银行",
                    "exchange": "SZSE",
                    "sector_id": "banking",
                    "role": "容量核心",
                    "role_source": "fake:stocks",
                    "daily_bars": [
                        {"date": "2026-09-03", "close": 12.2},
                        {"date": trade_date, "close": 12.5},
                    ],
                },
                {
                    "code": "000002",
                    "name": "万科A",
                    "close": 8.5,
                    "daily_bars": [
                        {"date": "2026-09-03", "close": 8.3},
                        {"date": trade_date, "close": 8.5},
                    ],
                },
            ],
        }

    @staticmethod
    def fake_stock_only_collector(trade_date, _slot):
        return {
            "source": "fake",
            "sample_date": trade_date,
            "stocks": [
                {
                    "code": "000001",
                    "name": "平安银行",
                    "exchange": "SZSE",
                    "sector_id": "banking",
                    "role": "容量核心",
                    "role_source": "fake:stocks",
                    "daily_bars": [{"date": trade_date, "close": 12.5}],
                }
            ],
        }

    @classmethod
    def fake_single_bar_history_collector(cls, trade_date, slot):
        payload = cls.fake_complete_snapshot_collector(trade_date, slot)
        payload["sectors"][0]["daily_bars"] = [
            {"date": trade_date, "change_percent": 1.2}
        ]
        payload["stocks"][0]["daily_bars"] = [
            {"date": trade_date, "close": 12.5}
        ]
        return payload

    @classmethod
    def fake_empty_snapshot_with_stock_collector(cls, trade_date, slot):
        if slot.scope == "stocks":
            return cls.fake_stock_only_collector(trade_date, slot)
        return cls.fake_empty_snapshot_collector(trade_date, slot)

    @staticmethod
    def fake_empty_snapshot_collector(trade_date, _slot):
        return {
            "source": "fake",
            "sample_date": trade_date,
            "market": {},
            "sentiment": {},
            "sectors": [],
            "stocks": [],
            "events": [],
        }

    @staticmethod
    def tamper_first_capture(repository, manifest):
        capture = repository.get_evidence_capture(
            f"CAPTURE-{manifest.captures[0].operation_id}"
        )
        Path(capture["payload"]["output_path"]).write_text("{}\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
