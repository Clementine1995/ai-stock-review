from dataclasses import asdict, dataclass
from decimal import Decimal
import unittest

from stock_review.reports.render_review_run import render_review_run
from stock_review.review_runs.review_revision import (
    GateResult,
    PlanItem,
    QualityAssessment,
    RevisionBundle,
    ScenarioBand,
    Step9PatternAssessment,
    StepAssessment,
)
from stock_review.scoring.verify_step9_patterns import STEP9_PATTERN_DEFINITIONS


@dataclass(frozen=True)
class StepInput:
    number: int
    title: str
    status: str
    conclusion: str
    key_evidence: tuple[str, ...]
    counter_evidence: tuple[str, ...]
    hard_gaps: tuple[str, ...]
    next_day_validation: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class FrameworkInput:
    version: str
    content_hash: str
    steps: tuple[StepInput, ...]


@dataclass(frozen=True)
class ReviewRunInput:
    run_id: str
    trade_date: str
    publication_id: str
    publication_status: str
    decision: dict[str, object]
    quality: dict[str, object]
    provenance: dict[str, str]
    framework: FrameworkInput
    step_assessments: tuple[StepInput, ...]
    plan_items: tuple[dict[str, object], ...]
    observations: tuple[dict[str, object], ...]
    appendix: dict[str, object]


class ReviewRunRendererTest(unittest.TestCase):
    def test_missing_quality_keeps_blockers_pending(self):
        content = render_review_run({"trade_date": "2026-09-05", "steps": []})

        self.assertIn("- 待确认（上游未提供）：阻断项。", content)
        self.assertNotIn("- 无阻断项。", content)

    def test_explicit_empty_collections_use_field_specific_text(self):
        content = render_review_run(
            {
                "trade_date": "2026-09-05",
                "decision": {"focus_targets": [], "risk_boundary": []},
                "quality": {"gates": [], "blocking_items": []},
                "steps": [{"step_number": 9, "pattern_assessments": []}],
                "approved_plan_items": [],
                "observations": [],
                "appendix": {},
                "revision_kind": "final",
            }
        )

        for expected in (
            "- 重点对象：未生成重点对象。",
            "- 放弃与失效边界：未生成放弃与失效边界。",
            "- 未生成 G1-G6 门禁结果。",
            "- 未生成 STEP 9 模式逐项核验。",
            "- 无阻断项。",
            "- 未生成附录。",
        ):
            self.assertIn(expected, content)

    def test_empty_step_fields_do_not_claim_all_upstream_input_is_empty(self):
        content = render_review_run({"steps": [{"step_number": 1,
            "counter_evidence": [], "evidence_references": ["capture:available"],
            "evidence_gaps": ["缺少实际可用的两市量能"]}], "approved_plan_items": [],
            "revision_kind": "final"})
        self.assertNotIn("上游已验证输入为空", content)
        self.assertIn("无反向证据", content)
        self.assertIn("未生成正式交易计划", content)

    def test_renderer_directly_accepts_revision_bundle_and_quality_assessment(self):
        revision = build_domain_revision()
        quality = QualityAssessment(
            level="actionable",
            publishable=True,
            gates=tuple(GateResult(gate_id, "passed") for gate_id in ("G1", "G2", "G3", "G4", "G5", "G6")),
        )
        observations = (
            {
                "observation_id": "OBS-DOMAIN-001",
                "plan_item_revision_id": "PLAN-DOMAIN-001",
                "status": "pending",
                "hypothesis": "次日强度保持",
                "confirmation_condition": "符合预期条件出现",
                "invalidation_condition": "放弃条件出现",
                "review_deadline": "2026-09-08",
                "evidence_references": ("evidence://domain/stock",),
            },
        )
        appendix = {"完整模式状态": ("模式 1：待触发",)}

        dataclass_content = render_review_run(
            revision,
            quality=quality,
            observations=observations,
            appendix=appendix,
        )
        mapping_content = render_review_run(
            revision.to_dict(),
            quality=quality.to_dict(),
            observations=observations,
            appendix=appendix,
        )

        self.assertEqual(dataclass_content, mapping_content)
        self.assertIn("- 次日是否参与：actionable", dataclass_content)
        self.assertIn("- 次日总纲：只执行上游批准的条件计划", dataclass_content)
        self.assertIn("- EvidenceBundle ID：EB-DOMAIN-001", dataclass_content)
        self.assertIn("- ReviewRevision 哈希：", dataclass_content)
        self.assertIn(
            "- 关键证据引用：evidence://domain/step/1",
            dataclass_content,
        )
        self.assertNotIn(
            "### 关键证据\n\n- 待确认（上游未提供）",
            dataclass_content,
        )
        self.assertIn("- 待确认问题：核心角色仍需确认", dataclass_content)
        self.assertIn("- 完整性问题：对象链已经由上游校验", dataclass_content)
        self.assertIn("- 证据缺口：竞价数据待次日补充", dataclass_content)
        self.assertIn("- 证据引用：evidence://domain/step/1", dataclass_content)
        self.assertIn("- 最大仓位或风险：0.1 ratio", dataclass_content)
        self.assertIn("- 条件：主指标处于预期区间", dataclass_content)
        self.assertIn("- 来源与主指标：validated-source / strength / percent", dataclass_content)
        self.assertIn("- 可观察时点：next_open", dataclass_content)
        self.assertIn("### 十二模式逐项核验", dataclass_content)
        self.assertIn(
            "#### 模式 12：情绪指数双低迷尾盘炸板",
            dataclass_content,
        )

    def test_renderer_is_deterministic_and_accepts_dataclass_or_mapping(self):
        report = build_complete_report()

        dataclass_content = render_review_run(report)
        mapping_content = render_review_run(asdict(report))

        self.assertEqual(dataclass_content, render_review_run(report))
        self.assertEqual(dataclass_content, mapping_content)
        self.assertTrue(dataclass_content.endswith("\n"))
        self.assertIn("## 顶部决策区", dataclass_content)
        self.assertIn("## 质量处置及阻断项", dataclass_content)
        self.assertIn("- 发布等级：actionable", dataclass_content)
        self.assertIn("## 证据与版本追溯", dataclass_content)
        self.assertIn("- Publication ID：PUB-001", dataclass_content)
        self.assertIn(f"- Publication 内容哈希：{'f' * 64}", dataclass_content)
        self.assertIn(f"- 正式报告 SHA-256：{'9' * 64}", dataclass_content)
        self.assertIn("- ActionSet ID：ACTIONSET-001", dataclass_content)
        self.assertIn(f"- ActionSet 内容哈希：{'8' * 64}", dataclass_content)
        self.assertIn("- EvidenceBundle ID：EB-20260905-001", dataclass_content)
        self.assertIn("- 框架版本：framework-v1", dataclass_content)
        self.assertIn("## 正式交易计划", dataclass_content)
        self.assertIn("#### 符合预期", dataclass_content)
        self.assertIn("- 条件：竞价符合预设范围", dataclass_content)
        self.assertIn("#### 超预期", dataclass_content)
        self.assertIn("#### 不及预期", dataclass_content)
        self.assertIn("#### 放弃条件", dataclass_content)
        self.assertIn("- 证据引用：evidence://stock/000001", dataclass_content)
        self.assertIn("## Observation 映射", dataclass_content)
        self.assertIn("- PlanItemRevision ID：PLAN-001", dataclass_content)
        self.assertIn("## 附录", dataclass_content)
        self.assertIn("模式 1：background_matched_trigger_pending", dataclass_content)
        self.assertLess(
            dataclass_content.index("## 正式交易计划"),
            dataclass_content.index("## 证据与版本追溯"),
        )

    def test_non_participation_report_is_compact_and_states_empty_position(self):
        steps = [
            {
                "step_number": number,
                "title": f"空仓复盘步骤 {number}",
                "status": "not_applicable" if number in {7, 8, 10} else "complete",
                "conclusion": f"步骤 {number} 不形成次日行动。",
                "evidence_references": [f"capture:slot-{number}:hash-{number}"],
                "counter_evidence": [],
                "open_questions": [],
                "integrity_issues": [],
                "evidence_gaps": ["缺少行动所需证据"] if number == 6 else [],
                "next_day_checks": [],
                "pattern_assessments": (
                    [
                        {
                            "pattern_number": pattern_number,
                            "pattern_name": f"模式 {pattern_number}",
                            "status": "unavailable",
                        }
                        for pattern_number in range(1, 13)
                    ]
                    if number == 9
                    else []
                ),
            }
            for number in range(1, 11)
        ]
        report = {
            "run_id": "RUN-NO-ACTION",
            "trade_date": "2026-09-04",
            "revision_kind": "final",
            "decision": {
                "publication_status": "finalized",
                "participation_decision": "non_participation",
                "total_risk": "0",
                "summary": "证据不足以支持行动，次日空仓。",
                "focus_targets": [],
                "risk_boundary": [],
            },
            "quality": {
                "publication_level": "non_participation",
                "publishable": True,
                "disposition": "允许正式发布",
                "gates": [
                    {"gate_id": f"G{number}", "status": "passed"}
                    for number in range(1, 7)
                ],
                "blocking_items": [],
                "notes": [],
            },
            "steps": steps,
            "approved_plan_items": [],
            "observations": [],
            "appendix": {
                "candidate_plan_items": [],
                "evidence_references": ["capture:slot-1:hash-1"],
            },
            "provenance": {
                "publication_id": "PUB-NO-ACTION",
                "supersedes_publication_id": None,
                "model_response_hash": [],
            },
        }

        content = render_review_run(report)
        expanded_report = {
            **report,
            "decision": {
                **report["decision"],
                "participation_decision": "actionable",
            },
        }
        expanded_content = render_review_run(expanded_report)

        positions = [content.index(f"## STEP {number}:") for number in range(1, 11)]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(len(content.splitlines()), len(expanded_content.splitlines()) / 2)
        self.assertIn("- 总仓位与风险边界：0%（空仓，不参与）", content)
        self.assertIn("- 重点对象：无（空仓）", content)
        self.assertIn("- 空仓，不生成交易计划。", content)
        self.assertIn("- 空仓，不生成 Observation。", content)
        self.assertIn("- 十二模式状态：unavailable × 12", content)
        self.assertIn("- 取代的 Publication ID：无", content)
        self.assertIn("- 模型标识：未使用正式模型通道", content)
        self.assertNotIn("### 本步结论", content)
        self.assertNotIn("### 十二模式逐项核验", content)
        self.assertLess(
            content.index("## 附录"),
            content.index("## 证据与版本追溯"),
        )

    def test_renderer_outputs_all_steps_in_framework_order(self):
        report = build_complete_report()

        content = render_review_run(report)

        positions = [content.index(f"## STEP {number}:") for number in range(1, 11)]
        self.assertEqual(positions, sorted(positions))
        for number in range(1, 11):
            self.assertEqual(content.count(f"## STEP {number}:"), 1)
        self.assertIn("## STEP 10: 最终输出复盘文档", content)

    def test_missing_support_stays_blocked_and_does_not_invent_actions(self):
        framework = {
            "steps": [
                {"number": number, "title": f"上游框架步骤 {number}"}
                for number in range(1, 11)
            ]
        }
        report = {
            "run_id": "RUN-20260905-BLOCKED",
            "trade_date": "2026-09-05",
            "publication_status": "draft",
            "decision": {"participation": "待确认输入"},
            "quality": {
                "publication_level": "blocked",
                "disposition": "保留草案",
                "blocking_items": [
                    {
                        "severity": "P0",
                        "code": "missing_evidence_bundle",
                        "message": "EvidenceBundle 未提供",
                    }
                ],
            },
            "framework": framework,
        }

        content = render_review_run(report)

        self.assertIn("- 发布等级：blocked", content)
        self.assertIn("[P0 / missing_evidence_bundle] EvidenceBundle 未提供", content)
        self.assertEqual(content.count("- 结论：待确认（上游未提供）"), 10)
        self.assertIn("待确认（上游未提供）：正式交易计划", content)
        self.assertIn("待确认（上游未提供）：Observation 映射", content)
        self.assertNotIn("条件买入", content)
        self.assertNotIn("情绪修复", content)
        self.assertNotIn("总仓位 30%", content)
        self.assertNotIn("龙头", content)


def build_complete_report() -> ReviewRunInput:
    steps = tuple(
        StepInput(
            number=number,
            title=("最终输出复盘文档" if number == 10 else f"框架步骤 {number}"),
            status="confirmed",
            conclusion=f"上游已验证结论 {number}",
            key_evidence=(f"证据事实 {number}",),
            counter_evidence=(f"反向证据 {number}",),
            hard_gaps=(),
            next_day_validation=f"验证点 {number}",
            evidence_refs=(f"evidence://step/{number}",),
        )
        for number in range(1, 11)
    )
    plan = {
        "plan_item_revision_id": "PLAN-001",
        "code": "000001",
        "name": "平安银行",
        "role": "用户确认关注对象",
        "status": "approved",
        "action_type": "watch",
        "hypothesis": "上游确认的可验证假设",
        "position_limit": "不超过一成",
        "trigger_time": "次日竞价结束后",
        "scenarios": {
            "expected": {"condition": "竞价符合预设范围", "action": "继续观察"},
            "above_expected": {"condition": "强度超过预设上界", "action": "按批准方案执行"},
            "below_expected": {"condition": "强度低于预设下界", "action": "降低风险"},
            "abandon": {"condition": "失效条件触发", "action": "放弃"},
        },
        "evidence_refs": ("evidence://stock/000001",),
        "observation_ids": ("OBS-001",),
    }
    observation = {
        "observation_id": "OBS-001",
        "plan_item_revision_id": "PLAN-001",
        "status": "pending",
        "hypothesis": "上游确认的可验证假设",
        "confirmation_condition": "符合预期条件出现",
        "invalidation_condition": "放弃条件出现",
        "review_deadline": "2026-09-08",
        "evidence_refs": ("evidence://stock/000001",),
    }
    return ReviewRunInput(
        run_id="RUN-20260905-001",
        trade_date="2026-09-05",
        publication_id="PUB-001",
        publication_status="published",
        decision={
            "participation": "conditional",
            "position_guidance": "总风险不超过一成",
            "summary": "上游确认的次日总纲",
            "focus_targets": ("000001 平安银行",),
            "risk_boundary": "放弃条件触发后不参与",
        },
        quality={
            "publication_level": "actionable",
            "disposition": "允许发布",
            "blocking_items": (),
            "notes": ("G1-G6 已由上游校验",),
        },
        provenance={
            "publication_hash": "f" * 64,
            "report_hash": "9" * 64,
            "action_set_id": "ACTIONSET-001",
            "action_set_hash": "8" * 64,
            "manifest_id": "MANIFEST-001",
            "manifest_hash": "a" * 64,
            "evidence_bundle_id": "EB-20260905-001",
            "evidence_bundle_hash": "b" * 64,
            "evidence_cutoff": "2026-09-05T15:30:00+08:00",
            "review_revision_id": "REV-001",
            "review_revision_hash": "c" * 64,
            "framework_version": "framework-v1",
            "framework_hash": "d" * 64,
            "program_version": "0.1.0",
            "renderer_version": "review-run-markdown-v1",
            "model_id": "test-model",
            "model_response_hash": "e" * 64,
        },
        framework=FrameworkInput(
            version="framework-v1",
            content_hash="d" * 64,
            steps=steps,
        ),
        step_assessments=steps,
        plan_items=(plan,),
        observations=(observation,),
        appendix={
            "候选全集": ("000001 平安银行",),
            "模式核验": ("模式 1：background_matched_trigger_pending",),
        },
    )


def build_domain_revision() -> RevisionBundle:
    steps = tuple(
        StepAssessment(
            step_number=number,
            title=f"域模型步骤 {number}",
            status="complete",
            conclusion=f"域模型结论 {number}",
            evidence_references=(f"evidence://domain/step/{number}",),
            counter_evidence=("反向证据由上游确认",),
            open_questions=("核心角色仍需确认",) if number == 1 else (),
            next_day_checks=("检查日验证点",),
            integrity_issues=("对象链已经由上游校验",) if number == 1 else (),
            evidence_gaps=("竞价数据待次日补充",) if number == 1 else (),
            plan_item_ids=("PLAN-DOMAIN-001",) if number in (8, 10) else (),
            pattern_assessments=(
                tuple(
                    Step9PatternAssessment(
                        pattern_number=definition.pattern_number,
                        pattern_name=definition.name,
                        status="unavailable",
                        missing_evidence=definition.background_evidence
                        + definition.trigger_evidence,
                    )
                    for definition in STEP9_PATTERN_DEFINITIONS
                )
                if number == 9
                else ()
            ),
        )
        for number in range(1, 11)
    )
    scenario_names = (
        ("expectation", "主指标处于预期区间", "保持观察"),
        ("over_expectation", "主指标超过预期上界", "执行批准动作"),
        ("under_expectation", "主指标低于预期下界", "降低风险"),
        ("abandon", "失效条件出现", "放弃"),
    )
    scenarios = tuple(
        ScenarioBand(
            scenario=scenario,
            condition_text=condition,
            source="validated-source",
            metric="strength",
            unit="percent",
            observable_at="next_open",
            action=action,
            values=(scenario,),
        )
        for scenario, condition, action in scenario_names
    )
    plan = PlanItem(
        plan_item_id="PLAN-DOMAIN-001",
        object_id="stock:000001",
        object_type="stock",
        object_code="000001",
        object_name="平安银行",
        exchange="SZSE",
        sector_id="sector:bank",
        role="用户确认关注对象",
        role_source="manual-confirmation",
        role_confirmed=True,
        role_evidence_references=("evidence://domain/role",),
        direction_ids=("direction:bank",),
        independent_reason="",
        hypothesis="次日强度保持",
        confirmation_condition="符合预期条件出现",
        invalidation_condition="放弃条件出现",
        scenario_bands=scenarios,
        higher_is_better=None,
        condition_universe=tuple(item[0] for item in scenario_names),
        action_type="watch",
        max_risk=Decimal("0.1"),
        risk_unit="ratio",
        evidence_references=("evidence://domain/stock",),
        review_deadline="2026-09-08",
    )
    return RevisionBundle.create(
        review_revision_id="REV-DOMAIN-001",
        run_id="RUN-DOMAIN-001",
        trade_date="2026-09-05",
        evidence_bundle_id="EB-DOMAIN-001",
        evidence_bundle_hash="a" * 64,
        evidence_cutoff_at="2026-09-05T15:30:00+08:00",
        framework_version="framework-domain-v1",
        framework_hash="b" * 64,
        program_version="0.1.0",
        renderer_version="review-run-markdown-v1",
        created_at="2026-09-05T16:00:00+08:00",
        revision_kind="final",
        participation_decision="actionable",
        replaces_review_revision_id=None,
        steps=steps,
        approved_plan_items=(plan,),
        decision_note="只执行上游批准的条件计划",
        model_response_hashes=("c" * 64,),
    )


if __name__ == "__main__":
    unittest.main()
