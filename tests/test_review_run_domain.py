# 本文件验证 ReviewRun 领域对象的不可变哈希、事件投影、质量门禁与联合定稿约束。

from dataclasses import replace
from decimal import Decimal
import unittest

from stock_review.review_runs import (
    ActionSet,
    BusinessAction,
    CaptureSlot,
    EvidenceBundle,
    EvidenceCapture,
    FinalizationProposal,
    ModelUse,
    PlanItem,
    RejectedPlanItem,
    ReviewRunDomainError,
    ReviewRunEvent,
    RevisionBundle,
    RunManifest,
    ScenarioBand,
    Step9PatternAssessment,
    StepAssessment,
    VersionExpectation,
    assess_revision_quality,
    canonical_hash,
    canonical_json,
    project_review_run,
    validate_revision_bundle,
)
from stock_review.review_runs.action_set import FINALIZATION_DATABASE_EFFECTS
from stock_review.review_runs.run_manifest import DAILY_REVIEW_DATABASE_TABLES
from stock_review.scoring.verify_step9_patterns import STEP9_PATTERN_DEFINITIONS


TRADE_DATE = "2026-09-04"
CREATED_AT = "2026-09-05T08:00:00+00:00"
EVIDENCE_HASH = canonical_hash({"evidence": "sealed"})
FRAMEWORK_HASH = canonical_hash({"framework": "stock-review.md"})
REPORT_HASH = canonical_hash({"report": "final"})
REQUEST_HASH = canonical_hash({"request": "model"})
RESPONSE_HASH = canonical_hash({"response": "model"})


class ReviewRunDomainTest(unittest.TestCase):
    def test_manifest_has_stable_json_round_trip_and_rejects_tampering(self):
        manifest = make_manifest()

        self.assertEqual(
            canonical_json({"b": 2, "a": [Decimal("1.20")]}),
            canonical_json({"a": ["1.20"], "b": 2}),
        )
        self.assertEqual(manifest, RunManifest.from_dict(manifest.to_dict()))

        tampered = manifest.to_dict()
        tampered["captures"][0]["scope"] = "stocks"
        with self.assertRaisesRegex(ReviewRunDomainError, "哈希不一致"):
            RunManifest.from_dict(tampered)

    def test_evidence_bundle_seals_every_slot_once_and_round_trips(self):
        manifest = make_manifest()
        captures = make_captures(manifest)

        with self.assertRaisesRegex(ReviewRunDomainError, "每个 capture slot"):
            EvidenceBundle.seal(
                evidence_bundle_id="EVIDENCE-missing",
                manifest=manifest,
                captures=captures[:1],
                sealed_at="2026-09-05T08:05:00+00:00",
            )

        bundle = EvidenceBundle.seal(
            evidence_bundle_id="EVIDENCE-main",
            manifest=manifest,
            captures=captures,
            sealed_at="2026-09-05T08:05:00+00:00",
        )
        bundle.validate_against_manifest(manifest)
        self.assertEqual(bundle, EvidenceBundle.from_dict(bundle.to_dict()))
        self.assertEqual("skipped", bundle.captures[1].status)

    def test_event_projection_reaches_publication_and_is_idempotent(self):
        manifest = make_manifest()
        events = make_events(manifest)

        run = project_review_run(manifest, (*events, events[0]))

        self.assertEqual("published", run.state)
        self.assertEqual("completed", run.model_state)
        self.assertEqual("REV-final", run.current_review_revision_id)
        self.assertEqual("PUB-main", run.publication_id)
        self.assertEqual(("available", "skipped"), tuple(
            slot.status for slot in run.capture_slots
        ))
        self.assertEqual(len(events), len(run.applied_event_ids))

    def test_required_model_manifest_rejects_publication_without_completed_request(self):
        base = make_manifest()
        manifest = RunManifest.create(
            manifest_id=base.manifest_id,
            run_id=base.run_id,
            trade_date=base.trade_date,
            created_at=base.created_at,
            expires_at=base.expires_at,
            captures=base.captures,
            model_use=ModelUse(
                mode="required",
                model_ids=base.model_use.model_ids,
                max_requests=base.model_use.max_requests,
            ),
            file_effects=base.file_effects,
            database_effects=base.database_effects,
        )
        events = make_events(manifest)

        with self.assertRaisesRegex(ReviewRunDomainError, "要求模型参与"):
            project_review_run(manifest, (*events[:4], *events[6:]))

    def test_event_projection_rejects_attach_after_seal_and_stale_model_response(self):
        manifest = make_manifest()
        events = make_events(manifest)
        late_capture = ReviewRunEvent.create(
            event_id="EVENT-late-capture",
            run_id=manifest.run_id,
            event_type="capture_completed",
            occurred_at="2026-09-05T08:30:00+00:00",
            operation_id=manifest.captures[0].operation_id,
            capture_slot_id=manifest.captures[0].slot_id,
            capture_status="available",
        )
        with self.assertRaisesRegex(ReviewRunDomainError, "封存后"):
            project_review_run(manifest, (*events[:4], late_capture))

        request = events[4]
        revision = ReviewRunEvent.create(
            event_id="EVENT-human-revision",
            run_id=manifest.run_id,
            event_type="revision_added",
            occurred_at="2026-09-05T08:11:00+00:00",
            operation_id="OP-human-revision",
            review_revision_id="REV-human",
            expected_current_revision_id=None,
        )
        stale_response = events[5]
        with self.assertRaisesRegex(ReviewRunDomainError, "基础修订已经变化"):
            project_review_run(manifest, (*events[:4], request, revision, stale_response))

    def test_actionable_revision_passes_object_scenario_role_and_hash_gates(self):
        bundle = make_actionable_revision()

        assessment = validate_revision_bundle(bundle)

        self.assertEqual("actionable", assessment.level)
        self.assertTrue(assessment.publishable)
        self.assertEqual(bundle, RevisionBundle.from_dict(bundle.to_dict()))
        self.assertTrue(all(gate.status == "passed" for gate in assessment.gates))

    def test_revision_json_rejects_coerced_step_numbers_and_text_arrays(self):
        bundle = make_actionable_revision()
        boolean_step = bundle.to_dict()
        boolean_step["steps"][0]["step_number"] = True
        boolean_step["content_hash"] = canonical_hash(
            {key: value for key, value in boolean_step.items() if key != "content_hash"}
        )
        with self.assertRaisesRegex(ReviewRunDomainError, "STEP 编号"):
            RevisionBundle.from_dict(boolean_step)

        string_references = bundle.to_dict()
        string_references["steps"][0]["evidence_references"] = "evidence://invalid"
        string_references["content_hash"] = canonical_hash(
            {
                key: value
                for key, value in string_references.items()
                if key != "content_hash"
            }
        )
        with self.assertRaisesRegex(ReviewRunDomainError, "字符串数组"):
            RevisionBundle.from_dict(string_references)

    def test_actionable_revision_rejects_any_incomplete_step(self):
        bundle = make_actionable_revision()
        steps = tuple(
            replace(step, status="incomplete", conclusion="")
            if step.step_number == 3
            else step
            for step in bundle.steps
        )
        incomplete = rehash_revision(replace(bundle, steps=steps, content_hash=""))

        assessment = assess_revision_quality(incomplete)

        self.assertEqual("blocked", assessment.level)
        self.assertIn("STEP 3 必须完整完成", "；".join(assessment.issues))

    def test_p0_chain_role_scenario_and_hash_failures_cannot_be_downgraded(self):
        bundle = make_actionable_revision()

        broken_step = replace(
            bundle.steps[6],
            object_ids=("stock-outside",),
        )
        broken_chain = rehash_revision(
            replace(
                bundle,
                steps=(*bundle.steps[:6], broken_step, *bundle.steps[7:]),
                content_hash="",
            )
        )
        self.assertEqual("blocked", assess_revision_quality(broken_chain).level)
        self.assertEqual("failed", assess_revision_quality(broken_chain).gate_status("G2"))

        unconfirmed_plan = replace(bundle.approved_plan_items[0], role_confirmed=False)
        unconfirmed_role = replace_approved_plan(bundle, unconfirmed_plan)
        self.assertEqual("failed", assess_revision_quality(unconfirmed_role).gate_status("G1"))

        bands = list(bundle.approved_plan_items[0].scenario_bands)
        bands[1] = replace(bands[1], upper_bound=Decimal("1.5"))
        overlapping_plan = replace(bundle.approved_plan_items[0], scenario_bands=tuple(bands))
        overlapping = replace_approved_plan(bundle, overlapping_plan)
        self.assertEqual("failed", assess_revision_quality(overlapping).gate_status("G3"))

        tampered = replace(bundle, decision_note="未经重新哈希的改动")
        tampered_assessment = assess_revision_quality(tampered)
        self.assertEqual("blocked", tampered_assessment.level)
        self.assertEqual("failed", tampered_assessment.gate_status("G5"))

    def test_non_participation_requires_explicit_not_applicable_action_steps(self):
        bundle = make_non_participation_revision()

        assessment = validate_revision_bundle(bundle)

        self.assertEqual("non_participation", assessment.level)
        self.assertEqual("not_applicable", assessment.gate_status("G2"))
        self.assertEqual("not_applicable", assessment.gate_status("G3"))
        self.assertEqual("not_applicable", assessment.gate_status("G6"))

        step_seven = replace(bundle.steps[6], status="incomplete", conclusion="")
        invalid = rehash_revision(
            replace(
                bundle,
                steps=(*bundle.steps[:6], step_seven, *bundle.steps[7:]),
                content_hash="",
            )
        )
        invalid_assessment = assess_revision_quality(invalid)
        self.assertEqual("blocked", invalid_assessment.level)
        self.assertIn("STEP 7", "；".join(invalid_assessment.issues))

    def test_non_participation_rejects_incomplete_core_analysis(self):
        base = make_non_participation_revision()
        for step_number in (1, 2, 3, 4, 5, 6, 9):
            with self.subTest(step_number=step_number):
                steps = tuple(
                    replace(
                        step,
                        status="incomplete",
                        conclusion="现有材料不足以完成本步分析。",
                        evidence_gaps=("关键证据不足",),
                    )
                    if step.step_number == step_number
                    else step
                    for step in base.steps
                )
                invalid = rehash_revision(replace(base, steps=steps, content_hash=""))

                assessment = assess_revision_quality(invalid)

                self.assertEqual("blocked", assessment.level)
                self.assertEqual("failed", assessment.gate_status("G1"))
                self.assertIn(
                    f"核心分析 STEP {step_number} 未达到可发布条件",
                    "；".join(assessment.issues),
                )

    def test_non_participation_rejects_core_evidence_gap_even_when_step_is_complete(self):
        base = make_non_participation_revision()
        steps = tuple(
            replace(step, evidence_gaps=("板块连续比较不足",))
            if step.step_number == 5
            else step
            for step in base.steps
        )
        invalid = rehash_revision(replace(base, steps=steps, content_hash=""))

        assessment = assess_revision_quality(invalid)

        self.assertEqual("blocked", assessment.level)
        self.assertIn("项证据缺口", "；".join(assessment.issues))

    def test_non_participation_rejects_unresolved_step_nine_pattern(self):
        base = make_non_participation_revision()
        step_nine = replace(
            base.steps[8],
            pattern_assessments=(
                replace(
                    base.steps[8].pattern_assessments[0],
                    status="base_evidence_available_trigger_pending",
                    missing_evidence=("call_auction",),
                ),
                *base.steps[8].pattern_assessments[1:],
            ),
        )
        invalid = rehash_revision(replace(
            base,
            steps=(*base.steps[:8], step_nine, *base.steps[9:]),
            content_hash="",
        ))

        assessment = assess_revision_quality(invalid)

        self.assertEqual("blocked", assessment.level)
        self.assertIn("模式尚未完成核验：1", "；".join(assessment.issues))

    def test_non_participation_allows_unused_trigger_gap_after_background_not_matched(self):
        base = make_non_participation_revision()
        step_nine = replace(
            base.steps[8],
            evidence_gaps=("模式 1 缺少竞价触发材料",),
            pattern_assessments=(
                replace(
                    base.steps[8].pattern_assessments[0],
                    missing_evidence=("call_auction",),
                ),
                *base.steps[8].pattern_assessments[1:],
            ),
        )
        revised = rehash_revision(replace(
            base,
            steps=(*base.steps[:8], step_nine, *base.steps[9:]),
            content_hash="",
        ))

        assessment = validate_revision_bundle(revised)

        self.assertTrue(assessment.publishable)
        self.assertEqual("non_participation", assessment.level)

    def test_non_participation_rejects_punctuated_placeholder_conclusions(self):
        base = make_non_participation_revision()
        for conclusion in ("已冻结本步相关事实, 尚未形成经用户确认的业务判断。", "等待完成当日复盘判断。"):
            invalid = rehash_revision(replace(base, steps=tuple(
                replace(step, conclusion=conclusion) if step.step_number in {1, 2} else step
                for step in base.steps
            ), content_hash=""))
            self.assertIn("通用模板", "；".join(assess_revision_quality(invalid).issues))

    def test_non_participation_allows_equal_specific_conclusions(self):
        base = make_non_participation_revision()
        revised = rehash_revision(replace(base, steps=tuple(
            replace(step, conclusion="冻结材料均显示当日无合格行动对象。")
            if step.step_number in {1, 2} else step
            for step in base.steps
        ), content_hash=""))

        self.assertTrue(assess_revision_quality(revised).publishable)

    def test_specific_conclusion_containing_negated_placeholder_text_is_not_rejected(self):
        base = make_non_participation_revision()
        revised = rehash_revision(replace(base, steps=tuple(
            replace(
                step,
                conclusion="冻结量能与指数证据已经核对，不再需要用户结合证据确认本步结论。",
            ) if step.step_number == 1 else step
            for step in base.steps
        ), content_hash=""))

        self.assertTrue(assess_revision_quality(revised).publishable)

    def test_non_participation_rejects_local_decision_placeholder(self):
        base = make_non_participation_revision()
        invalid = rehash_revision(replace(
            base,
            decision_note="规则版草案未产生行动计划，等待用户定稿。",
            content_hash="",
        ))

        self.assertIn("通用模板", "；".join(assess_revision_quality(invalid).issues))

    def test_final_incomplete_step_nine_rejects_pattern_object_projection_mismatch(self):
        base = make_non_participation_revision()
        step_nine = replace(
            base.steps[8],
            status="incomplete",
            pattern_assessments=(
                replace(base.steps[8].pattern_assessments[0], object_ids=("stock-unknown",)),
                *base.steps[8].pattern_assessments[1:],
            ),
        )
        invalid = rehash_revision(replace(
            base,
            steps=(*base.steps[:8], step_nine, *base.steps[9:]),
            content_hash="",
        ))

        self.assertIn("STEP 9 对象", "；".join(assess_revision_quality(invalid).issues))

    def test_finalization_proposal_round_trips_and_checks_all_expected_versions(self):
        manifest = make_manifest()
        proposal = make_proposal(manifest)

        self.assertEqual(
            proposal,
            FinalizationProposal.from_dict(proposal.to_dict()),
        )
        current_versions = {
            ("review_publication", TRADE_DATE): "PUB-old",
            ("observation", "OBS-main"): None,
        }
        proposal.validate_against_manifest(manifest)
        proposal.validate_current_state(
            current_review_revision_id="REV-draft",
            current_versions=current_versions,
            checked_at="2026-09-05T08:25:00+00:00",
        )

        current_versions[("review_publication", TRADE_DATE)] = "PUB-newer"
        with self.assertRaisesRegex(ReviewRunDomainError, "期望版本已经过期"):
            proposal.validate_current_state(
                current_review_revision_id="REV-draft",
                current_versions=current_versions,
                checked_at="2026-09-05T08:25:00+00:00",
            )

        current_versions[("review_publication", TRADE_DATE)] = "PUB-old"
        current_versions[("observation", "OBS-main")] = "OBS-existing-hash"
        with self.assertRaisesRegex(ReviewRunDomainError, "期望版本已经过期"):
            proposal.validate_current_state(
                current_review_revision_id="REV-draft",
                current_versions=current_versions,
                checked_at="2026-09-05T08:25:00+00:00",
            )

    def test_action_set_rejects_database_effect_outside_manifest(self):
        manifest = make_manifest()
        action_set = make_proposal(manifest).action_set
        unauthorized = replace(
            action_set,
            database_effects=(*action_set.database_effects, "legacy_imports"),
            content_hash="",
        )
        unauthorized = replace(
            unauthorized,
            content_hash=unauthorized.calculate_hash(),
        )

        with self.assertRaisesRegex(ReviewRunDomainError, "数据库影响超出"):
            unauthorized.validate_against_manifest(manifest)

    def test_finalization_rejects_rehashed_incomplete_effect_declarations(self):
        proposal = make_proposal(make_manifest())
        missing_database_effects = replace(
            proposal.action_set,
            database_effects=(),
            content_hash="",
        )
        missing_database_effects = replace(
            missing_database_effects,
            content_hash=missing_database_effects.calculate_hash(),
        )
        invalid_database_proposal = replace(
            proposal,
            action_set=missing_database_effects,
            content_hash="",
        )
        invalid_database_proposal = replace(
            invalid_database_proposal,
            content_hash=invalid_database_proposal.calculate_hash(),
        )
        with self.assertRaisesRegex(ReviewRunDomainError, "数据库影响必须完整声明"):
            invalid_database_proposal.validate()

        arbitrary_files = replace(
            proposal.action_set,
            file_effects=(
                "reports/daily/revisions/arbitrary.md",
                "reports/daily/blobs/arbitrary.md",
            ),
            content_hash="",
        )
        arbitrary_files = replace(
            arbitrary_files,
            content_hash=arbitrary_files.calculate_hash(),
        )
        invalid_file_proposal = replace(
            proposal,
            action_set=arbitrary_files,
            content_hash="",
        )
        invalid_file_proposal = replace(
            invalid_file_proposal,
            content_hash=invalid_file_proposal.calculate_hash(),
        )
        with self.assertRaisesRegex(ReviewRunDomainError, "报告哈希作为文件名"):
            invalid_file_proposal.validate()

    def test_finalization_rejects_missing_observation_or_tampered_combined_hash(self):
        manifest = make_manifest()
        proposal = make_proposal(manifest)
        publication = proposal.action_set.actions[0]
        observation = proposal.action_set.actions[1]
        with self.assertRaisesRegex(ReviewRunDomainError, "版本期望"):
            ActionSet.create(
                action_set_id="ACTIONSET-missing-version",
                run_id=proposal.run_id,
                trade_date=proposal.trade_date,
                manifest_id=proposal.manifest_id,
                manifest_hash=proposal.manifest_hash,
                review_revision_id=proposal.final_revision.review_revision_id,
                review_revision_hash=proposal.final_revision.content_hash,
                report_hash=REPORT_HASH,
                created_at=proposal.created_at,
                expires_at=proposal.expires_at,
                actions=(publication, observation),
                version_expectations=(
                    VersionExpectation("review_publication", TRADE_DATE, "PUB-old"),
                ),
            )
        incomplete_action_set = ActionSet.create(
            action_set_id="ACTIONSET-incomplete",
            run_id=proposal.run_id,
            trade_date=proposal.trade_date,
            manifest_id=proposal.manifest_id,
            manifest_hash=proposal.manifest_hash,
            review_revision_id=proposal.final_revision.review_revision_id,
            review_revision_hash=proposal.final_revision.content_hash,
            report_hash=REPORT_HASH,
            created_at=proposal.created_at,
            expires_at=proposal.expires_at,
            actions=(publication,),
            version_expectations=(
                VersionExpectation("review_publication", TRADE_DATE, "PUB-old"),
            ),
            file_effects=proposal.action_set.file_effects,
            database_effects=proposal.action_set.database_effects,
        )
        with self.assertRaisesRegex(ReviewRunDomainError, "approved PlanItem"):
            FinalizationProposal.create(
                proposal_id="PROPOSAL-incomplete",
                run_id=proposal.run_id,
                trade_date=proposal.trade_date,
                manifest_id=proposal.manifest_id,
                manifest_hash=proposal.manifest_hash,
                base_review_revision_id="REV-draft",
                expected_current_review_revision_id="REV-draft",
                final_revision=proposal.final_revision,
                action_set=incomplete_action_set,
                created_at=proposal.created_at,
                expires_at=proposal.expires_at,
            )

        payload = proposal.to_dict()
        payload["base_review_revision_id"] = "REV-other"
        with self.assertRaisesRegex(ReviewRunDomainError, "组合哈希"):
            FinalizationProposal.from_dict(payload)


# 固定清单同时覆盖普通与条件采集，并授权一次模型请求。
def make_manifest() -> RunManifest:
    return RunManifest.create(
        manifest_id="MANIFEST-main",
        run_id="RUN-main",
        trade_date=TRADE_DATE,
        created_at=CREATED_AT,
        expires_at="2026-09-05T09:00:00+00:00",
        captures=(
            CaptureSlot(
                slot_id="capture-market",
                operation_id="OP-capture-market",
                source="manual",
                scope="market",
                input_location="base64-stdin",
                output_location="data/evidence/market.json",
                network_required=False,
                write_effects=("data/evidence/market.json",),
                allowed_dates=(TRADE_DATE,),
                allowed_fields=("market",),
            ),
            CaptureSlot(
                slot_id="capture-history",
                operation_id="OP-capture-history",
                source="workbuddy",
                scope="index-history",
                input_location="connector",
                output_location="data/evidence/history.json",
                network_required=True,
                write_effects=("data/evidence/history.json",),
                predicate="missing:market.index_history",
                allowed_objects=("000001.SH",),
                allowed_dates=(TRADE_DATE,),
                allowed_fields=("market.index_history",),
                max_records=5,
            ),
        ),
        model_use=ModelUse(
            mode="optional",
            model_ids=("review-model",),
            max_requests=1,
        ),
        file_effects=("reports/daily/revisions", "reports/daily/blobs"),
        database_effects=tuple(
            f"review.sqlite:{table_name}"
            for table_name in DAILY_REVIEW_DATABASE_TABLES
        ),
    )


# 证据封存样例包含一个成功采集和一个谓词不成立的显式跳过。
def make_captures(manifest: RunManifest) -> tuple[EvidenceCapture, ...]:
    return (
        EvidenceCapture(
            slot_id=manifest.captures[0].slot_id,
            operation_id=manifest.captures[0].operation_id,
            source="manual",
            scope="market",
            status="available",
            captured_at="2026-09-05T08:02:00+00:00",
            sample_date=TRADE_DATE,
            raw_response_hash=canonical_hash({"raw": "market"}),
            normalized_evidence_hash=canonical_hash({"market": "normalized"}),
        ),
        EvidenceCapture(
            slot_id=manifest.captures[1].slot_id,
            operation_id=manifest.captures[1].operation_id,
            source="workbuddy",
            scope="index-history",
            status="skipped",
            captured_at="2026-09-05T08:03:00+00:00",
            failure_reason="缺口谓词不成立",
        ),
    )


# 事件样例覆盖采集、封存、模型响应、修订追加和正式发布。
def make_events(manifest: RunManifest) -> tuple[ReviewRunEvent, ...]:
    return (
        ReviewRunEvent.create(
            event_id="EVENT-start",
            run_id=manifest.run_id,
            event_type="run_started",
            occurred_at="2026-09-05T08:00:01+00:00",
            manifest_hash=manifest.content_hash,
        ),
        ReviewRunEvent.create(
            event_id="EVENT-market",
            run_id=manifest.run_id,
            event_type="capture_completed",
            occurred_at="2026-09-05T08:02:00+00:00",
            operation_id="OP-capture-market",
            capture_slot_id="capture-market",
            capture_status="available",
        ),
        ReviewRunEvent.create(
            event_id="EVENT-history",
            run_id=manifest.run_id,
            event_type="capture_completed",
            occurred_at="2026-09-05T08:03:00+00:00",
            operation_id="OP-capture-history",
            capture_slot_id="capture-history",
            capture_status="skipped",
        ),
        ReviewRunEvent.create(
            event_id="EVENT-seal",
            run_id=manifest.run_id,
            event_type="evidence_bundle_sealed",
            occurred_at="2026-09-05T08:05:00+00:00",
            evidence_bundle_id="EVIDENCE-main",
            evidence_bundle_hash=EVIDENCE_HASH,
        ),
        ReviewRunEvent.create(
            event_id="EVENT-model-request",
            run_id=manifest.run_id,
            event_type="model_requested",
            occurred_at="2026-09-05T08:06:00+00:00",
            operation_id="OP-model-request",
            subject_id="MODELREQUEST-main",
            evidence_bundle_id="EVIDENCE-main",
            evidence_bundle_hash=EVIDENCE_HASH,
            base_review_revision_id=None,
            expected_current_revision_id=None,
            model_id="review-model",
            model_request_hash=REQUEST_HASH,
        ),
        ReviewRunEvent.create(
            event_id="EVENT-model-response",
            run_id=manifest.run_id,
            event_type="model_completed",
            occurred_at="2026-09-05T08:08:00+00:00",
            operation_id="OP-model-response",
            subject_id="MODELREQUEST-main",
            evidence_bundle_id="EVIDENCE-main",
            evidence_bundle_hash=EVIDENCE_HASH,
            base_review_revision_id=None,
            expected_current_revision_id=None,
            model_id="review-model",
            model_request_hash=REQUEST_HASH,
            model_response_hash=RESPONSE_HASH,
        ),
        ReviewRunEvent.create(
            event_id="EVENT-revision",
            run_id=manifest.run_id,
            event_type="revision_added",
            occurred_at="2026-09-05T08:10:00+00:00",
            operation_id="OP-revision",
            review_revision_id="REV-final",
            expected_current_revision_id=None,
        ),
        ReviewRunEvent.create(
            event_id="EVENT-publication",
            run_id=manifest.run_id,
            event_type="publication_completed",
            occurred_at="2026-09-05T08:20:00+00:00",
            operation_id="OP-publication",
            review_revision_id="REV-final",
            expected_current_revision_id="REV-final",
            publication_id="PUB-main",
        ),
    )


# 数值四档连续覆盖全域，边界各自只归属一个情景。
def make_plan_item() -> PlanItem:
    common = {
        "source": "evidence://stock-main",
        "metric": "auction_change_pct",
        "unit": "percent",
        "observable_at": "next_trade_day_09:25",
    }
    return PlanItem(
        plan_item_id="PLAN-main",
        object_id="stock-main",
        object_type="stock",
        object_code="000001",
        object_name="平安银行",
        exchange="SZSE",
        sector_id="banking",
        role="板块容量核心",
        role_source="standardized_evidence",
        role_confirmed=True,
        role_evidence_references=("evidence://stock-main/role",),
        direction_ids=("direction-banking",),
        independent_reason="",
        hypothesis="银行方向次日保持承接，容量核心强于板块中位数。",
        confirmation_condition="竞价涨幅不低于 1%，且板块量能未明显收缩。",
        invalidation_condition="竞价涨幅低于 0%，或板块量能显著收缩。",
        scenario_bands=(
            ScenarioBand(
                scenario="abandon",
                condition_text="竞价涨幅低于 0%",
                upper_bound=Decimal("0"),
                lower_inclusive=False,
                upper_inclusive=False,
                action="放弃",
                **common,
            ),
            ScenarioBand(
                scenario="under_expectation",
                condition_text="竞价涨幅在 0% 到 1% 之间",
                lower_bound=Decimal("0"),
                upper_bound=Decimal("1"),
                lower_inclusive=True,
                upper_inclusive=False,
                action="观察不参与",
                **common,
            ),
            ScenarioBand(
                scenario="expectation",
                condition_text="竞价涨幅在 1% 到 2% 之间",
                lower_bound=Decimal("1"),
                upper_bound=Decimal("2"),
                lower_inclusive=True,
                upper_inclusive=False,
                action="按计划低风险参与",
                **common,
            ),
            ScenarioBand(
                scenario="over_expectation",
                condition_text="竞价涨幅不低于 2%",
                lower_bound=Decimal("2"),
                lower_inclusive=True,
                upper_inclusive=False,
                action="等待分歧后确认",
                **common,
            ),
        ),
        higher_is_better=True,
        condition_universe=(),
        action_type="conditional_watch",
        max_risk=Decimal("0.5"),
        risk_unit="portfolio_percent",
        evidence_references=("evidence://stock-main", "evidence://sector-banking"),
        review_deadline="2026-09-07",
    )


# STEP 8 与 STEP 10 只投影同一 PlanItem，不复制另一套条件。
def make_steps(*, non_participation: bool = False) -> tuple[StepAssessment, ...]:
    steps: list[StepAssessment] = []
    for step_number in range(1, 11):
        not_applicable = non_participation and step_number in {7, 8, 10}
        steps.append(
            StepAssessment(
                step_number=step_number,
                title=f"STEP {step_number}",
                status="not_applicable" if not_applicable else "complete",
                conclusion=(
                    "用户确认当日没有合格行动对象。"
                    if not_applicable
                    else f"STEP {step_number} 已完成。"
                ),
                evidence_references=(f"evidence://step-{step_number}",),
                direction_ids=("direction-banking",) if step_number == 4 else (),
                object_ids=(
                    ("stock-main",)
                    if not non_participation and step_number in {6, 7, 8, 10}
                    else ()
                ),
                plan_item_ids=(
                    ("PLAN-main",)
                    if not non_participation and step_number in {8, 10}
                    else ()
                ),
                pattern_assessments=(
                    tuple(
                        Step9PatternAssessment(
                            pattern_number=definition.pattern_number,
                            pattern_name=definition.name,
                            status="not_matched",
                            evidence_references=("evidence://step-9",),
                        )
                        for definition in STEP9_PATTERN_DEFINITIONS
                    )
                    if step_number == 9
                    else ()
                ),
            )
        )
    return tuple(steps)


# 正式可行动修订含唯一批准计划，候选集合必须为空。
def make_actionable_revision(
    *,
    created_at: str = "2026-09-05T08:15:00+00:00",
) -> RevisionBundle:
    return RevisionBundle.create(
        review_revision_id="REV-final",
        run_id="RUN-main",
        trade_date=TRADE_DATE,
        evidence_bundle_id="EVIDENCE-main",
        evidence_bundle_hash=EVIDENCE_HASH,
        evidence_cutoff_at="2026-09-05T08:05:00+00:00",
        framework_version="1",
        framework_hash=FRAMEWORK_HASH,
        program_version="0.2.0",
        renderer_version="2",
        created_at=created_at,
        revision_kind="final",
        participation_decision="actionable",
        replaces_review_revision_id="REV-draft",
        steps=make_steps(),
        approved_plan_items=(make_plan_item(),),
        rejected_plan_items=(RejectedPlanItem("PLAN-rejected", "角色证据不足"),),
        decision_note="用户确认批准唯一计划项。",
        model_response_hashes=(RESPONSE_HASH,),
    )


# 明确不参与修订必须先完成核心证据分析，行动步骤则显式不适用。
def make_non_participation_revision() -> RevisionBundle:
    return RevisionBundle.create(
        review_revision_id="REV-no-action",
        run_id="RUN-main",
        trade_date=TRADE_DATE,
        evidence_bundle_id="EVIDENCE-main",
        evidence_bundle_hash=EVIDENCE_HASH,
        evidence_cutoff_at="2026-09-05T08:05:00+00:00",
        framework_version="1",
        framework_hash=FRAMEWORK_HASH,
        program_version="0.2.0",
        renderer_version="2",
        created_at="2026-09-05T08:15:00+00:00",
        revision_kind="final",
        participation_decision="non_participation",
        replaces_review_revision_id="REV-draft",
        steps=make_steps(non_participation=True),
        decision_note="核心证据均已核对，用户确认次日没有合格行动对象。",
    )


# 替换计划项后必须重算 RevisionBundle 哈希，模拟一次完整联合换版。
def replace_approved_plan(bundle: RevisionBundle, plan: PlanItem) -> RevisionBundle:
    return rehash_revision(
        replace(bundle, approved_plan_items=(plan,), content_hash="")
    )


def rehash_revision(bundle: RevisionBundle) -> RevisionBundle:
    return replace(bundle, content_hash=bundle.calculate_hash())


# 联合提案冻结 publication 和 Observation 两个目标的预览版本。
def make_proposal(manifest: RunManifest) -> FinalizationProposal:
    created_at = "2026-09-05T08:20:00+00:00"
    expires_at = "2026-09-05T08:40:00+00:00"
    revision = make_actionable_revision(created_at=created_at)
    publication = BusinessAction.create(
        action_id="ACTION-publish",
        action_type="publish_review",
        target_type="review_publication",
        target_id=TRADE_DATE,
        payload={
            "trade_date": TRADE_DATE,
            "run_id": revision.run_id,
            "review_revision_id": revision.review_revision_id,
            "review_revision_hash": revision.content_hash,
            "report_hash": REPORT_HASH,
        },
    )
    plan = revision.approved_plan_items[0]
    observation = BusinessAction.create(
        action_id="ACTION-observation",
        action_type="create_observation",
        target_type="observation",
        target_id="OBS-main",
        payload={
            "observation_id": "OBS-main",
            "review_revision_id": revision.review_revision_id,
            "review_revision_hash": revision.content_hash,
            "plan_item_id": plan.plan_item_id,
            "hypothesis": plan.hypothesis,
            "confirmation_condition": plan.confirmation_condition,
            "invalidation_condition": plan.invalidation_condition,
            "evidence_references": list(plan.evidence_references),
            "review_deadline": plan.review_deadline,
            "created_at": created_at,
        },
    )
    action_set = ActionSet.create(
        action_set_id="ACTIONSET-main",
        run_id=revision.run_id,
        trade_date=revision.trade_date,
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.content_hash,
        review_revision_id=revision.review_revision_id,
        review_revision_hash=revision.content_hash,
        report_hash=REPORT_HASH,
        created_at=created_at,
        expires_at=expires_at,
        actions=(publication, observation),
        version_expectations=(
            VersionExpectation("review_publication", TRADE_DATE, "PUB-old"),
            VersionExpectation("observation", "OBS-main", None),
        ),
        file_effects=(
            f"reports/daily/revisions/{revision.review_revision_id}.md",
            f"reports/daily/blobs/{REPORT_HASH}.md",
        ),
        database_effects=FINALIZATION_DATABASE_EFFECTS,
    )
    return FinalizationProposal.create(
        proposal_id="PROPOSAL-main",
        run_id=revision.run_id,
        trade_date=revision.trade_date,
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.content_hash,
        base_review_revision_id="REV-draft",
        expected_current_review_revision_id="REV-draft",
        final_revision=revision,
        action_set=action_set,
        created_at=created_at,
        expires_at=expires_at,
    )


if __name__ == "__main__":
    unittest.main()
