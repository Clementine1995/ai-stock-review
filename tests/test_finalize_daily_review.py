# 本文件验证单一正式日报的内容寻址发布、原子 ActionSet 与失败恢复。

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from stock_review.review_runs.complete_daily_review import complete_local_review
from stock_review.review_runs.action_set import BusinessAction
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.finalize_daily_review import (
    finalize_daily_review,
    propose_daily_actions,
)
from stock_review.review_runs.plan_daily_review import plan_daily_review
from stock_review.review_runs.run_daily_review import (
    load_run_projection,
    seal_daily_evidence,
    start_daily_review,
)
from stock_review.review_runs.revise_daily_review import revise_daily_review
from stock_review.review_runs.verify_daily_review import (
    describe_daily_review,
    verify_daily_review,
)
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


class FinalizeDailyReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.now = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
        self.repository = ReviewRunRepository(self.root / "review.sqlite")
        self.manifest, self.draft = self.create_draft(0)

    def test_non_participation_proposal_is_read_only_then_publishes_one_report(self):
        proposal, preview = self.propose(self.manifest, self.draft)
        self.assertIsNone(self.repository.get_current_review_publication(self.manifest.trade_date))
        self.assertFalse(Path(proposal.action_set.file_effects[1]).exists())
        self.assertIn("non_participation", preview)
        self.assertIn("## STEP 10:", preview)
        self.assertIn(
            "- 发布状态：finalized（是否已提交及是否为当前版本，以 publication 投影为准）",
            preview,
        )
        self.assertNotIn("待最终确认", preview)
        self.assertIn(
            f"- ActionSet ID：{proposal.action_set.action_set_id}",
            preview,
        )
        self.assertIn(
            f"action_sets[{proposal.action_set.action_set_id}].content_hash",
            preview,
        )
        self.assertIn("- Publication ID：PUB-", preview)
        self.assertIn("review_publications[PUB-", preview)

        result = finalize_daily_review(
            proposal,
            proposal.content_hash,
            self.repository,
            now=self.now,
        )
        report_path = Path(result["report_path"])
        self.assertTrue(report_path.exists())
        self.assertEqual(
            proposal.action_set.report_hash,
            hashlib.sha256(report_path.read_bytes()).hexdigest(),
        )
        current = self.repository.get_current_review_publication(self.manifest.trade_date)
        self.assertEqual(result["publication_id"], current["publication_id"])
        self.assertEqual(proposal.action_set.content_hash, result["action_set_hash"])
        self.assertEqual(current["content_hash"], result["publication_hash"])
        self.assertEqual(proposal.action_set.report_hash, result["report_hash"])
        self.assertEqual(preview, report_path.read_text(encoding="utf-8"))
        self.assertEqual("published", load_run_projection(self.repository, self.manifest.run_id).state)
        self.assertEqual([], result["observation_ids"])

        replayed = finalize_daily_review(
            proposal,
            proposal.content_hash,
            self.repository,
            now=self.now,
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(result["action_set_hash"], replayed["action_set_hash"])
        self.assertEqual(result["publication_hash"], replayed["publication_hash"])
        self.assertEqual(result["report_hash"], replayed["report_hash"])

    def test_replay_repairs_missing_immutable_report_files(self):
        proposal, _preview = self.propose(self.manifest, self.draft)
        finalize_daily_review(
            proposal,
            proposal.content_hash,
            self.repository,
            now=self.now,
        )
        revision_path, blob_path = (
            Path(item) for item in proposal.action_set.file_effects
        )
        revision_path.unlink()
        blob_path.unlink()

        replayed = finalize_daily_review(
            proposal,
            proposal.content_hash,
            self.repository,
            now=self.now,
        )

        self.assertTrue(replayed["replayed"])
        for path in (revision_path, blob_path):
            self.assertTrue(path.exists())
            self.assertEqual(
                proposal.action_set.report_hash,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )

    def test_finalize_independently_rejects_template_proposal_created_without_quality_gate(self):
        local = complete_local_review(
            self.manifest.run_id, self.manifest.trade_date, self.draft.review_revision_id,
            self.manifest.content_hash, "OP-unreviewed-finalize", self.repository, now=self.now,
        )
        # 模拟旧版本放行的提案，正式 finalize 必须独立重新执行当前质量门禁。
        with patch("stock_review.review_runs.finalize_daily_review.validate_revision_bundle") as validate, patch(
            "stock_review.review_runs.action_set.validate_revision_bundle"
        ):
            from stock_review.review_runs.review_revision import assess_revision_quality

            validate.return_value = assess_revision_quality(self.draft)
            proposal, _ = self.propose(self.manifest, local)
        with self.assertRaisesRegex(ReviewRunDomainError, "通用模板"):
            finalize_daily_review(proposal, proposal.content_hash, self.repository, now=self.now)
        self.assertIsNone(self.repository.get_current_review_publication(self.manifest.trade_date))

    def test_manual_revision_cannot_hide_real_gaps_at_proposal(self):
        manifest, sparse_draft = self.create_draft(2000, evidence_complete=False)
        revised = revise_daily_review(
            manifest.run_id, sparse_draft.review_revision_id, sparse_draft.review_revision_id,
            {"steps": [dict(step.to_dict(), evidence_gaps=[]) for step in sparse_draft.steps],
             "candidate_plan_items": [], "decision_note": "量能不足，暂不参与。"},
            "OP-cleared-gaps", self.repository, now=self.now,
        )
        with self.assertRaisesRegex(ReviewRunDomainError, "未保留实际证据缺口"):
            self.propose(manifest, revised)

    def test_expired_manifest_blocks_revision_without_changing_run(self):
        event_ids_before = [
            item["event_id"]
            for item in self.repository.list_review_run_events(self.manifest.run_id)
        ]

        with self.assertRaisesRegex(ReviewRunDomainError, "RunManifest 已过期"):
            revise_daily_review(
                self.manifest.run_id,
                self.draft.review_revision_id,
                self.draft.review_revision_id,
                {
                    "steps": [item.to_dict() for item in self.draft.steps],
                    "candidate_plan_items": [],
                    "decision_note": "过期授权不能继续追加修订。",
                },
                "OP-expired-manifest-revise",
                self.repository,
                now=self.now + timedelta(minutes=30),
            )

        self.assertEqual(
            event_ids_before,
            [
                item["event_id"]
                for item in self.repository.list_review_run_events(self.manifest.run_id)
            ],
        )
        self.assertEqual(
            self.draft.review_revision_id,
            load_run_projection(
                self.repository,
                self.manifest.run_id,
            ).current_review_revision_id,
        )

    def test_expired_manifest_blocks_proposal_generation(self):
        with self.assertRaisesRegex(ReviewRunDomainError, "RunManifest 已过期"):
            propose_daily_actions(
                self.manifest.run_id,
                self.draft.review_revision_id,
                {
                    "participation_decision": "non_participation",
                    "decision_note": "过期授权不能生成新的最终提案。",
                    "approved_plan_item_ids": [],
                    "rejected_plan_items": {},
                },
                self.repository,
                output_dir=self.root / "reports",
                now=self.now + timedelta(minutes=30),
            )

        self.assertIsNone(
            self.repository.get_current_review_publication(self.manifest.trade_date)
        )

    def test_expired_manifest_blocks_finalize_while_proposal_is_still_active(self):
        # 本用例隔离提案内容质量，只验证提案仍有效但 Manifest 已过期的授权边界。
        with patch(
            "stock_review.review_runs.finalize_daily_review.validate_revision_bundle"
        ) as validate, patch(
            "stock_review.review_runs.action_set.validate_revision_bundle"
        ):
            from stock_review.review_runs.review_revision import assess_revision_quality

            validate.return_value = assess_revision_quality(self.draft)
            proposal, _preview = propose_daily_actions(
                self.manifest.run_id,
                self.draft.review_revision_id,
                {
                    "participation_decision": "non_participation",
                    "decision_note": "有效授权窗口内生成等待确认的最终提案。",
                    "approved_plan_item_ids": [],
                    "rejected_plan_items": {},
                },
                self.repository,
                output_dir=self.root / "reports",
                now=self.now,
                proposal_lifetime=timedelta(hours=1),
            )

        with patch(
            "stock_review.review_runs.finalize_daily_review.validate_revision_bundle"
        ), patch(
            "stock_review.review_runs.action_set.validate_revision_bundle"
        ):
            with self.assertRaisesRegex(ReviewRunDomainError, "RunManifest 已过期"):
                finalize_daily_review(
                    proposal,
                    proposal.content_hash,
                    self.repository,
                    now=self.now + timedelta(minutes=30),
                )

        self.assertIsNone(
            self.repository.get_current_review_publication(self.manifest.trade_date)
        )
        self.assertFalse(any(Path(item).exists() for item in proposal.action_set.file_effects))
        self.assertEqual(
            "draft_ready",
            load_run_projection(self.repository, self.manifest.run_id).state,
        )

    def test_replay_rejects_tampered_immutable_report_file(self):
        proposal, _preview = self.propose(self.manifest, self.draft)
        finalize_daily_review(
            proposal,
            proposal.content_hash,
            self.repository,
            now=self.now,
        )
        blob_path = Path(proposal.action_set.file_effects[1])
        blob_path.write_text("被篡改的正式报告\n", encoding="utf-8")

        with self.assertRaisesRegex(ReviewRunDomainError, "不可变报告文件已存在且内容不同"):
            finalize_daily_review(
                proposal,
                proposal.content_hash,
                self.repository,
                now=self.now,
            )

    def test_required_model_blocks_proposal_finalize_and_invalid_publication(self):
        manifest, draft = self.create_draft(1000, model_required=True)
        with self.assertRaisesRegex(ReviewRunDomainError, "要求模型参与"):
            self.propose(manifest, draft)

        guard_path = (
            "stock_review.review_runs.finalize_daily_review."
            "_require_required_model_result"
        )
        with patch(guard_path):
            proposal, _preview = self.propose(manifest, draft)
        with self.assertRaisesRegex(ReviewRunDomainError, "要求模型参与"):
            finalize_daily_review(
                proposal,
                proposal.content_hash,
                self.repository,
                now=self.now,
            )

        # 绕过服务门禁构造损坏样本，verify 仍必须独立识别 required 合同被破坏。
        with patch(guard_path), patch(
            "stock_review.review_runs.finalize_daily_review.project_review_run"
        ):
            finalize_daily_review(
                proposal,
                proposal.content_hash,
                self.repository,
                now=self.now,
            )
        verification = verify_daily_review(manifest.run_id, self.repository)
        self.assertFalse(verification.valid)
        self.assertTrue(
            any(
                not check.ok
                and (
                    "required 模型约束" in check.detail
                    or "要求模型参与" in check.detail
                )
                for check in verification.checks
            )
        )

    def test_copied_database_cannot_receive_revision_or_finalization(self):
        proposal, _ = self.propose(self.manifest, self.draft)
        clone_path = self.root / "clone.sqlite"
        shutil.copy2(self.repository.database_path, clone_path)
        clone_repository = ReviewRunRepository(clone_path)

        with self.assertRaisesRegex(ReviewRunDomainError, "实际 SQLite 路径"):
            revise_daily_review(
                self.manifest.run_id,
                self.draft.review_revision_id,
                self.draft.review_revision_id,
                {
                    "steps": [item.to_dict() for item in self.draft.steps],
                    "candidate_plan_items": [
                        item.to_dict() for item in self.draft.candidate_plan_items
                    ],
                    "decision_note": "复制库不得追加修订。",
                },
                "OP-clone-revise",
                clone_repository,
                now=self.now,
            )
        with self.assertRaisesRegex(ReviewRunDomainError, "实际 SQLite 路径"):
            finalize_daily_review(
                proposal,
                proposal.content_hash,
                clone_repository,
                now=self.now,
            )
        self.assertIsNone(
            clone_repository.get_current_review_publication(self.manifest.trade_date)
        )

    def test_failure_after_blob_leaves_no_publication_and_retry_completes(self):
        proposal, _ = self.propose(self.manifest, self.draft)

        def fail_after_blob(stage):
            if stage == "after_blob_write":
                raise RuntimeError("模拟 blob 后故障")

        with self.assertRaisesRegex(RuntimeError, "模拟 blob 后故障"):
            finalize_daily_review(
                proposal,
                proposal.content_hash,
                self.repository,
                now=self.now,
                failure_injector=fail_after_blob,
            )
        self.assertTrue(Path(proposal.action_set.file_effects[1]).exists())
        self.assertIsNone(self.repository.get_current_review_publication(self.manifest.trade_date))
        self.assertEqual("draft_ready", load_run_projection(self.repository, self.manifest.run_id).state)

        result = finalize_daily_review(
            proposal,
            proposal.content_hash,
            self.repository,
            now=self.now,
        )
        self.assertIsNotNone(result["publication_id"])

    def test_expired_staged_proposal_can_be_replaced_with_new_identity(self):
        proposal, _ = self.propose(self.manifest, self.draft)

        def fail_before_apply(stage):
            if stage == "before_apply":
                raise RuntimeError("模拟 ActionSet 应用前故障")

        with self.assertRaisesRegex(RuntimeError, "模拟 ActionSet 应用前故障"):
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

        replacement, _ = propose_daily_actions(
            self.manifest.run_id,
            self.draft.review_revision_id,
            {
                "participation_decision": "non_participation",
                "decision_note": "证据只支持保守观察，用户确认次日不参与。",
                "approved_plan_item_ids": [],
                "rejected_plan_items": {},
            },
            self.repository,
            output_dir=self.root / "reports",
            now=self.now + timedelta(minutes=21),
        )
        self.assertNotEqual(proposal.proposal_id, replacement.proposal_id)
        self.assertNotEqual(
            proposal.action_set.action_set_id,
            replacement.action_set.action_set_id,
        )
        self.assertNotEqual(
            proposal.final_revision.review_revision_id,
            replacement.final_revision.review_revision_id,
        )

        result = finalize_daily_review(
            replacement,
            replacement.content_hash,
            self.repository,
            now=self.now + timedelta(minutes=21),
        )
        self.assertIsNotNone(result["publication_id"])
        current = self.repository.get_current_review_publication(
            self.manifest.trade_date
        )
        self.assertEqual(replacement.proposal_id, current["payload"]["proposal_id"])

    def test_failure_after_atomic_transaction_replays_published_state(self):
        proposal, _ = self.propose(self.manifest, self.draft)

        def fail_after_apply(stage):
            if stage == "after_apply":
                raise RuntimeError("模拟事务后故障")

        with self.assertRaisesRegex(RuntimeError, "模拟事务后故障"):
            finalize_daily_review(
                proposal,
                proposal.content_hash,
                self.repository,
                now=self.now,
                failure_injector=fail_after_apply,
            )
        self.assertIsNotNone(self.repository.get_current_review_publication(self.manifest.trade_date))
        self.assertEqual("published", load_run_projection(self.repository, self.manifest.run_id).state)

        replayed = finalize_daily_review(
            proposal,
            proposal.content_hash,
            self.repository,
            now=self.now,
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual("published", load_run_projection(self.repository, self.manifest.run_id).state)

    def test_failure_injection_recovers_file_and_transaction_boundaries(self):
        stages = (
            ("after_blob_temporary_write", False),
            ("after_blob_immutable_publish", False),
            ("after_transaction_begin", False),
            ("after_publication_update", False),
            ("after_business_actions", False),
            ("after_audit_outbox", False),
            ("after_action_set_application", False),
            ("before_transaction_commit", False),
            ("after_transaction_commit", True),
        )
        for position, (stage, committed_before_error) in enumerate(stages, start=1):
            with self.subTest(stage=stage):
                manifest, draft = self.create_draft(10_000 + position * 1_000)
                proposal, _preview = self.propose(manifest, draft)

                def fail_at_boundary(current_stage):
                    if current_stage == stage:
                        raise RuntimeError(stage)

                with self.assertRaisesRegex(RuntimeError, stage):
                    finalize_daily_review(
                        proposal,
                        proposal.content_hash,
                        self.repository,
                        now=self.now,
                        failure_injector=fail_at_boundary,
                    )

                current = self.repository.get_current_review_publication(
                    manifest.trade_date
                )
                self.assertEqual(
                    committed_before_error,
                    current is not None
                    and current["action_set_id"]
                    == proposal.action_set.action_set_id,
                )

                recovered = finalize_daily_review(
                    proposal,
                    proposal.content_hash,
                    self.repository,
                    now=self.now,
                )
                self.assertEqual(committed_before_error, recovered["replayed"])
                self.assertEqual(
                    "published",
                    load_run_projection(self.repository, manifest.run_id).state,
                )

    def test_published_atomic_transaction_rejects_later_revision(self):
        proposal, _ = self.propose(self.manifest, self.draft)

        def fail_after_apply(stage):
            if stage == "after_apply":
                raise RuntimeError("模拟事务后故障")

        with self.assertRaisesRegex(RuntimeError, "模拟事务后故障"):
            finalize_daily_review(
                proposal,
                proposal.content_hash,
                self.repository,
                now=self.now,
                failure_injector=fail_after_apply,
            )
        event_ids_before = [
            item["event_id"]
            for item in self.repository.list_review_run_events(self.manifest.run_id)
        ]

        with self.assertRaisesRegex(ReviewRunDomainError, "draft_ready"):
            revise_daily_review(
                self.manifest.run_id,
                proposal.final_revision.review_revision_id,
                proposal.final_revision.review_revision_id,
                {
                    "steps": [item.to_dict() for item in proposal.final_revision.steps],
                    "candidate_plan_items": [],
                    "decision_note": "正式发布后不能再追加草案。",
                },
                "OP-revise-after-finalize-apply",
                self.repository,
                now=self.now + timedelta(minutes=2),
            )

        self.assertEqual(
            event_ids_before,
            [
                item["event_id"]
                for item in self.repository.list_review_run_events(self.manifest.run_id)
            ],
        )
        projection = load_run_projection(self.repository, self.manifest.run_id)
        self.assertEqual("published", projection.state)
        self.assertEqual(
            proposal.final_revision.review_revision_id,
            projection.current_review_revision_id,
        )

    def test_replay_rejects_different_proposal_reusing_applied_action_set_id(self):
        applied, _ = self.propose(self.manifest, self.draft)
        different, _ = propose_daily_actions(
            self.manifest.run_id,
            self.draft.review_revision_id,
            {
                "participation_decision": "non_participation",
                "decision_note": "这是另一份内容不同的有效提案。",
                "approved_plan_item_ids": [],
                "rejected_plan_items": {},
            },
            self.repository,
            output_dir=self.root / "reports",
            now=self.now,
        )
        reused_action_set = replace(
            different.action_set,
            action_set_id=applied.action_set.action_set_id,
            content_hash="",
        )
        reused_action_set = replace(
            reused_action_set,
            content_hash=reused_action_set.calculate_hash(),
        )
        reused_proposal = replace(
            different,
            action_set=reused_action_set,
            content_hash="",
        )
        reused_proposal = replace(
            reused_proposal,
            content_hash=reused_proposal.calculate_hash(),
        )
        reused_proposal.validate()
        finalize_daily_review(
            applied,
            applied.content_hash,
            self.repository,
            now=self.now,
        )

        with self.assertRaisesRegex(ReviewRunDomainError, "已存 ActionSet.*当前提案"):
            finalize_daily_review(
                reused_proposal,
                reused_proposal.content_hash,
                self.repository,
                now=self.now,
            )

    def test_proposal_rejects_report_paths_outside_manifest_roots(self):
        with self.assertRaisesRegex(ReviewRunDomainError, "文件影响.*授权根"):
            propose_daily_actions(
                self.manifest.run_id,
                self.draft.review_revision_id,
                {
                    "participation_decision": "non_participation",
                    "decision_note": "尝试把正式报告写到授权目录之外。",
                    "approved_plan_item_ids": [],
                    "rejected_plan_items": {},
                },
                self.repository,
                output_dir=self.root / "outside-authorized",
                now=self.now,
            )

    def test_unknown_business_action_is_rejected_before_action_set_creation(self):
        with self.assertRaisesRegex(ReviewRunDomainError, "未知业务动作类型"):
            BusinessAction.create(
                action_id="ACT-UNKNOWN",
                action_type="unknown_pool_change",
                target_type="pool_item",
                target_id="watch:999999",
                payload={"status": "active"},
            )

    def test_proposal_rejects_invalid_observation_result_action(self):
        observation_id = "OBS-result-validation"
        self.repository.create_observation(
            observation_id,
            self.manifest.run_id,
            self.draft.review_revision_id,
            "PLAN-result-validation",
            {"hypothesis": "用于验证结果动作", "review_deadline": "2026-09-05"},
        )
        selections = {
            "participation_decision": "non_participation",
            "decision_note": "不参与，同时确认到期 Observation 的实际结果。",
            "approved_plan_item_ids": [],
            "rejected_plan_items": {},
            "observation_result_actions": [
                {
                    "observation_id": observation_id,
                    "status": "unknown",
                    "actual_result": "已观察到结果。",
                    "reason": "",
                    "evidence_references": ["manual:result"],
                }
            ],
        }

        with self.assertRaisesRegex(ReviewRunDomainError, "hit、miss 或 invalid"):
            propose_daily_actions(
                self.manifest.run_id,
                self.draft.review_revision_id,
                selections,
                self.repository,
                output_dir=self.root / "reports",
                now=self.now,
            )

    def test_finalize_rejects_rehashed_invalid_observation_result_action(self):
        observation_id = "OBS-crafted-result"
        self.repository.create_observation(
            observation_id,
            self.manifest.run_id,
            self.draft.review_revision_id,
            "PLAN-crafted-result",
            {"hypothesis": "用于验证最终提交仍会执行业务校验"},
        )
        proposal, _ = propose_daily_actions(
            self.manifest.run_id,
            self.draft.review_revision_id,
            {
                "participation_decision": "non_participation",
                "decision_note": "不参与，同时确认到期 Observation 的实际结果。",
                "approved_plan_item_ids": [],
                "rejected_plan_items": {},
                "observation_result_actions": [
                    {
                        "observation_id": observation_id,
                        "status": "hit",
                        "actual_result": "假设得到证据验证。",
                        "reason": "",
                        "evidence_references": ["manual:result"],
                    }
                ],
            },
            self.repository,
            output_dir=self.root / "reports",
            now=self.now,
        )
        invalid_actions = tuple(
            BusinessAction.create(
                action_id=action.action_id,
                action_type=action.action_type,
                target_type=action.target_type,
                target_id=action.target_id,
                payload={
                    **dict(action.payload),
                    "actual_result": "",
                    "evidence_references": [],
                },
            )
            if action.action_type == "record_observation_result"
            else action
            for action in proposal.action_set.actions
        )
        invalid_action_set = replace(
            proposal.action_set,
            actions=invalid_actions,
            content_hash="",
        )
        invalid_action_set = replace(
            invalid_action_set,
            content_hash=invalid_action_set.calculate_hash(),
        )
        invalid_proposal = replace(
            proposal,
            action_set=invalid_action_set,
            content_hash="",
        )
        invalid_proposal = replace(
            invalid_proposal,
            content_hash=invalid_proposal.calculate_hash(),
        )

        with self.assertRaisesRegex(ReviewRunDomainError, "实际结果不能为空"):
            finalize_daily_review(
                invalid_proposal,
                invalid_proposal.content_hash,
                self.repository,
                now=self.now,
            )

        self.assertIsNone(
            self.repository.get_current_review_publication(self.manifest.trade_date)
        )
        self.assertIsNone(
            self.repository.get_current_observation_result(observation_id)[
                "result_revision_id"
            ]
        )

    def test_failure_before_atomic_finalize_leaves_no_partial_state(self):
        proposal, _ = self.propose(self.manifest, self.draft)

        def fail_between_events(stage):
            if stage == "after_final_revision_event":
                raise RuntimeError("模拟最终修订事件后故障")

        with self.assertRaisesRegex(RuntimeError, "模拟最终修订事件后故障"):
            finalize_daily_review(
                proposal,
                proposal.content_hash,
                self.repository,
                now=self.now,
                failure_injector=fail_between_events,
            )
        partial = load_run_projection(self.repository, self.manifest.run_id)
        self.assertEqual("draft_ready", partial.state)
        self.assertEqual(
            self.draft.review_revision_id,
            partial.current_review_revision_id,
        )
        self.assertIsNone(
            self.repository.get_current_review_publication(self.manifest.trade_date)
        )

        recovered = finalize_daily_review(
            proposal,
            proposal.content_hash,
            self.repository,
            now=self.now + timedelta(minutes=10),
        )
        self.assertFalse(recovered["replayed"])
        self.assertEqual(
            "published", load_run_projection(self.repository, self.manifest.run_id).state
        )

    def test_interleaved_revision_prevents_events_and_action_set_application(self):
        proposal, _ = self.propose(self.manifest, self.draft)
        interleaved_revisions = []

        def add_revision_between_event_previews(stage):
            if stage != "after_final_revision_event":
                return
            interleaved_revisions.append(
                complete_local_review(
                    self.manifest.run_id,
                    self.manifest.trade_date,
                    self.draft.review_revision_id,
                    self.manifest.content_hash,
                    "OP-interleaved-finalize-revision",
                    self.repository,
                    framework_path=Path("stock-review.md"),
                    now=self.now + timedelta(minutes=1),
                )
            )

        with self.assertRaisesRegex(OptimisticConcurrencyError, "版本冲突"):
            finalize_daily_review(
                proposal,
                proposal.content_hash,
                self.repository,
                now=self.now,
                failure_injector=add_revision_between_event_previews,
            )

        projection = load_run_projection(self.repository, self.manifest.run_id)
        self.assertEqual(
            interleaved_revisions[0].review_revision_id,
            projection.current_review_revision_id,
        )
        self.assertIsNone(
            self.repository.get_current_review_publication(self.manifest.trade_date)
        )
        self.assertFalse(
            any(
                record["event_type"] == "publication_completed"
                for record in self.repository.list_review_run_events(
                    self.manifest.run_id
                )
            )
        )

    def test_two_runs_cannot_publish_from_the_same_stale_day_pointer(self):
        other_manifest, other_draft = self.create_draft(1000)
        first, _ = self.propose(self.manifest, self.draft)
        second, _ = self.propose(other_manifest, other_draft)
        finalize_daily_review(first, first.content_hash, self.repository, now=self.now)
        with self.assertRaisesRegex(ReviewRunDomainError, "期望版本"):
            finalize_daily_review(second, second.content_hash, self.repository, now=self.now)
        current = self.repository.get_current_review_publication(self.manifest.trade_date)
        self.assertEqual(self.manifest.run_id, current["run_id"])
        self.assertEqual("draft_ready", load_run_projection(self.repository, other_manifest.run_id).state)

    def test_verify_detects_a_tampered_official_report(self):
        proposal, _ = self.propose(self.manifest, self.draft)
        result = finalize_daily_review(
            proposal,
            proposal.content_hash,
            self.repository,
            now=self.now,
        )

        verified = verify_daily_review(self.manifest.run_id, self.repository)
        self.assertTrue(verified.valid, verified.to_dict())

        Path(result["report_path"]).write_text("被篡改的报告\n", encoding="utf-8")
        tampered = verify_daily_review(self.manifest.run_id, self.repository)
        self.assertFalse(tampered.valid)
        publication_check = next(
            item for item in tampered.checks if item.name == "review_publication"
        )
        self.assertIn("report_hash", publication_check.detail)

    def test_show_only_returns_observations_bound_to_requested_publication(self):
        first_proposal, _ = self.propose(self.manifest, self.draft)
        first = finalize_daily_review(
            first_proposal,
            first_proposal.content_hash,
            self.repository,
            now=self.now,
        )
        other_manifest, other_draft = self.create_draft(1000)
        second_proposal, _ = self.propose(other_manifest, other_draft)
        second = finalize_daily_review(
            second_proposal,
            second_proposal.content_hash,
            self.repository,
            now=self.now,
        )
        self.repository.create_observation(
            "OBS-first",
            self.manifest.run_id,
            first["review_revision_id"],
            "PLAN-first",
            {"review_deadline": "2026-09-07"},
            action_set_id=first["action_set_id"],
        )
        self.repository.create_observation(
            "OBS-second",
            other_manifest.run_id,
            second["review_revision_id"],
            "PLAN-second",
            {"review_deadline": "2026-09-04"},
            action_set_id=second["action_set_id"],
        )
        self.repository.create_observation(
            "OBS-second-completed",
            other_manifest.run_id,
            second["review_revision_id"],
            "PLAN-second-completed",
            {"review_deadline": "2026-09-04"},
            action_set_id=second["action_set_id"],
        )
        self.repository.append_observation_result_revision(
            "OBS-second-completed",
            "OBS-RESULT-second-completed",
            "hit",
            {"actual_result": "已完成回填"},
            expected_result_revision_id=None,
            action_set_id=second["action_set_id"],
        )

        shown = describe_daily_review(self.manifest.run_id, self.repository)

        self.assertEqual(
            ["OBS-first"],
            [item["observation_id"] for item in shown["observations"]],
        )
        self.assertEqual(
            ["OBS-second"],
            [item["observation_id"] for item in shown["due_observations"]],
        )

    def create_draft(self, id_start, *, model_required=False, evidence_complete=True):
        manifest = plan_daily_review(
            "2026-09-04",
            ("fake:snapshot",),
            evidence_dir=self.root / f"evidence-{id_start}",
            output_dir=self.root / "reports",
            database_path=self.root / "review.sqlite",
            model_ids=("review-model",) if model_required else (),
            model_required=model_required,
            now=self.now,
            manifest_lifetime=timedelta(minutes=30),
            id_factory=SequentialIds(id_start),
        )
        start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            collectors={
                "fake": (
                    self.fake_complete_collector
                    if evidence_complete
                    else self.fake_sparse_collector
                )
            },
            now=self.now,
            event_id_factory=SequentialIds(id_start + 100),
        )
        seal_daily_evidence(
            manifest.run_id,
            manifest.trade_date,
            manifest.content_hash,
            self.repository,
            now=self.now,
            event_id_factory=SequentialIds(id_start + 200),
        )
        draft = complete_local_review(
            manifest.run_id,
            manifest.trade_date,
            None,
            manifest.content_hash,
            f"OP-local-{id_start}",
            self.repository,
            framework_path=Path("stock-review.md"),
            now=self.now,
        )
        # 发布协议测试使用证据充分但无机会的空仓修订，避免用采集失败绕过门禁。
        reviewed_steps = []
        for step in draft.steps:
            value = dict(
                step.to_dict(),
                conclusion=(
                    f"{step.title}：核心证据已核对，未形成符合框架的行动机会。"
                    if evidence_complete
                    else f"{step.title}：" + "；".join(step.evidence_gaps) + "，暂不形成判断。"
                ),
                open_questions=[],
            )
            if evidence_complete and step.step_number in {1, 2, 3, 4, 5, 6, 9}:
                value["status"] = "complete"
            if evidence_complete and step.step_number == 9:
                value["pattern_assessments"] = [
                    dict(pattern.to_dict(), status="not_matched", object_ids=[])
                    for pattern in step.pattern_assessments
                ]
            reviewed_steps.append(value)
        draft = revise_daily_review(
            manifest.run_id, draft.review_revision_id, draft.review_revision_id,
            {"steps": reviewed_steps,
             "candidate_plan_items": [],
             "decision_note": (
                 "核心证据已核对，未发现符合框架的行动机会。"
                 if evidence_complete
                 else "只有指数收盘证据，量能不足，暂不形成判断。"
             )},
            f"OP-reviewed-{id_start}", self.repository, now=self.now,
        )
        return manifest, draft

    def propose(self, manifest, draft):
        return propose_daily_actions(
            manifest.run_id,
            draft.review_revision_id,
            {
                "participation_decision": "non_participation",
                "decision_note": "证据只支持保守观察，用户确认次日不参与。",
                "approved_plan_item_ids": [],
                "rejected_plan_items": {},
            },
            self.repository,
            output_dir=self.root / "reports",
            now=self.now,
        )

    @staticmethod
    def fake_complete_collector(trade_date, _slot):
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
            "sectors": [{
                "sector_id": "banking",
                "name": "银行",
                "rank": 1,
                "daily_bars": [
                    {"date": "2026-09-03", "change_percent": 0.8},
                    {"date": trade_date, "change_percent": 1.2},
                ],
            }],
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
                    "exchange": "SZSE",
                    "sector_id": "banking",
                    "role": "风格样本",
                    "role_source": "fake:stocks",
                    "daily_bars": [
                        {"date": "2026-09-03", "close": 8.3},
                        {"date": trade_date, "close": 8.5},
                    ],
                },
            ],
            "call_auction": [{"code": "000001", "change_percent": 0.5}],
            "intraday": [{"code": "000001", "change_percent": 1.0}],
            "previous_day_feedback": [{"code": "000001", "result": "positive"}],
            "next_day_feedback": [{"code": "000001", "result": "observed"}],
            "regulatory_event": [{"code": "000001", "event": "none"}],
        }

    @staticmethod
    def fake_sparse_collector(trade_date, _slot):
        return {
            "source": "fake",
            "sample_date": trade_date,
            "market": {"indices": [{"name": "上证指数", "close": 3800}]},
        }


if __name__ == "__main__":
    unittest.main()
