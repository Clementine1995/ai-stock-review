# 本文件定义 STEP 1-10 与计划项共同换版的 RevisionBundle，并执行发布质量门禁。

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping

from stock_review.review_runs.canonical_hash import canonical_hash, require_sha256
from stock_review.review_runs.domain_error import ReviewRunDomainError


STEP_NUMBERS = tuple(range(1, 11))
STEP_STATUSES = {"complete", "incomplete", "not_applicable"}
STEP9_PATTERN_STATUSES = {
    "unavailable",
    "assessment_pending",
    "not_matched",
    "base_evidence_available_trigger_pending",
    "background_matched_trigger_pending",
    "confirmed",
}
STEP9_FINAL_PATTERN_STATUSES = {"not_matched", "confirmed"}
REVISION_KINDS = {"draft", "final"}
PARTICIPATION_DECISIONS = {"undecided", "non_participation", "actionable"}
NON_PARTICIPATION_CORE_STEP_NUMBERS = (1, 2, 3, 4, 5, 6, 9)
SCENARIOS = {
    "expectation",
    "over_expectation",
    "under_expectation",
    "abandon",
}
OBJECT_TYPES = {"market", "sector", "stock"}
QUALITY_LEVELS = {"blocked", "non_participation", "actionable"}
GATE_IDS = ("G1", "G2", "G3", "G4", "G5", "G6")
GATE_STATUSES = {"passed", "failed", "not_applicable"}
STOCK_CODE_PATTERN = re.compile(r"^\d{6}$")
STOCK_EXCHANGES = {"SSE", "SZSE", "BSE", "SH", "SZ", "BJ"}
FINAL_PLACEHOLDER_TEXTS = {
    "已冻结本步相关事实，尚未形成经用户确认的业务判断",
    "本步缺少可用的直接证据，暂不形成业务判断",
    "需要用户结合证据确认本步结论",
    "仅在形成正式计划后按结构化条件观察",
    "等待完成当日复盘判断",
    "规则版草案未产生行动计划，等待用户定稿",
}


@dataclass(frozen=True)
class Step9PatternAssessment:
    pattern_number: int
    pattern_name: str
    status: str
    object_ids: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.pattern_number) is not int or self.pattern_number not in range(1, 13):
            raise ReviewRunDomainError("STEP 9 模式编号必须在 1 到 12 之间。")
        _require_text(self.pattern_name, "STEP 9 模式名称")
        if self.status not in STEP9_PATTERN_STATUSES:
            raise ReviewRunDomainError("STEP 9 模式状态无效。")
        for label, values in (
            ("STEP 9 模式对象 ID", self.object_ids),
            ("STEP 9 模式证据引用", self.evidence_references),
            ("STEP 9 模式证据缺口", self.missing_evidence),
        ):
            _require_unique_texts(values, label)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_number": self.pattern_number,
            "pattern_name": self.pattern_name,
            "status": self.status,
            "object_ids": list(self.object_ids),
            "evidence_references": list(self.evidence_references),
            "missing_evidence": list(self.missing_evidence),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Step9PatternAssessment:
        return cls(
            pattern_number=value.get("pattern_number", 0),
            pattern_name=str(value.get("pattern_name", "")),
            status=str(value.get("status", "")),
            object_ids=_text_tuple(value, "object_ids"),
            evidence_references=_text_tuple(value, "evidence_references"),
            missing_evidence=_text_tuple(value, "missing_evidence"),
        )


@dataclass(frozen=True)
class StepAssessment:
    step_number: int
    title: str
    status: str
    conclusion: str
    evidence_references: tuple[str, ...] = ()
    counter_evidence: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    next_day_checks: tuple[str, ...] = ()
    direction_ids: tuple[str, ...] = ()
    object_ids: tuple[str, ...] = ()
    plan_item_ids: tuple[str, ...] = ()
    integrity_issues: tuple[str, ...] = ()
    evidence_gaps: tuple[str, ...] = ()
    pattern_assessments: tuple[Step9PatternAssessment, ...] = ()

    def __post_init__(self) -> None:
        if type(self.step_number) is not int or self.step_number not in STEP_NUMBERS:
            raise ReviewRunDomainError("STEP 编号必须在 1 到 10 之间。")
        _require_text(self.title, "STEP 标题")
        if self.status not in STEP_STATUSES:
            raise ReviewRunDomainError("STEP 状态必须是 complete、incomplete 或 not_applicable。")
        if self.status in {"complete", "not_applicable"}:
            _require_text(self.conclusion, "STEP 结论")
        for label, values in (
            ("证据引用", self.evidence_references),
            ("反向证据", self.counter_evidence),
            ("待确认问题", self.open_questions),
            ("次日验证点", self.next_day_checks),
            ("方向 ID", self.direction_ids),
            ("对象 ID", self.object_ids),
            ("计划项 ID", self.plan_item_ids),
            ("完整性问题", self.integrity_issues),
            ("证据缺口", self.evidence_gaps),
        ):
            _require_unique_texts(values, label)
        if self.step_number != 9 and self.pattern_assessments:
            raise ReviewRunDomainError("只有 STEP 9 可以保存模式逐项核验。")
        pattern_numbers = tuple(
            pattern.pattern_number for pattern in self.pattern_assessments
        )
        if len(pattern_numbers) != len(set(pattern_numbers)):
            raise ReviewRunDomainError("STEP 9 模式逐项核验不能包含重复编号。")

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_number": self.step_number,
            "title": self.title,
            "status": self.status,
            "conclusion": self.conclusion,
            "evidence_references": list(self.evidence_references),
            "counter_evidence": list(self.counter_evidence),
            "open_questions": list(self.open_questions),
            "next_day_checks": list(self.next_day_checks),
            "direction_ids": list(self.direction_ids),
            "object_ids": list(self.object_ids),
            "plan_item_ids": list(self.plan_item_ids),
            "integrity_issues": list(self.integrity_issues),
            "evidence_gaps": list(self.evidence_gaps),
            "pattern_assessments": [
                pattern.to_dict() for pattern in self.pattern_assessments
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> StepAssessment:
        return cls(
            step_number=value.get("step_number", 0),
            title=str(value.get("title", "")),
            status=str(value.get("status", "")),
            conclusion=str(value.get("conclusion", "")),
            evidence_references=_text_tuple(value, "evidence_references"),
            counter_evidence=_text_tuple(value, "counter_evidence"),
            open_questions=_text_tuple(value, "open_questions"),
            next_day_checks=_text_tuple(value, "next_day_checks"),
            direction_ids=_text_tuple(value, "direction_ids"),
            object_ids=_text_tuple(value, "object_ids"),
            plan_item_ids=_text_tuple(value, "plan_item_ids"),
            integrity_issues=_text_tuple(value, "integrity_issues"),
            evidence_gaps=_text_tuple(value, "evidence_gaps"),
            pattern_assessments=tuple(
                Step9PatternAssessment.from_dict(
                    _require_mapping(item, "STEP 9 模式评估")
                )
                for item in value.get("pattern_assessments", ())
            ),
        )


@dataclass(frozen=True)
class ScenarioBand:
    scenario: str
    condition_text: str
    source: str
    metric: str
    unit: str
    observable_at: str
    action: str
    lower_bound: Decimal | None = None
    upper_bound: Decimal | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = False
    values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.scenario not in SCENARIOS:
            raise ReviewRunDomainError(f"未知四档情景：{self.scenario}")
        for label, value in (
            ("条件文字", self.condition_text),
            ("条件来源", self.source),
            ("主指标", self.metric),
            ("指标单位", self.unit),
            ("可观察时点", self.observable_at),
            ("对应行动", self.action),
        ):
            _require_text(value, label)
        if not isinstance(self.lower_inclusive, bool) or not isinstance(
            self.upper_inclusive,
            bool,
        ):
            raise ReviewRunDomainError("四档边界的包含标记必须是布尔值。")
        object.__setattr__(self, "lower_bound", _optional_decimal(self.lower_bound))
        object.__setattr__(self, "upper_bound", _optional_decimal(self.upper_bound))
        _require_unique_texts(self.values, "枚举条件值")
        if self.values and (self.lower_bound is not None or self.upper_bound is not None):
            raise ReviewRunDomainError("同一四档条件不能同时使用数值边界和枚举值。")
        if (
            self.lower_bound is not None
            and self.upper_bound is not None
            and self.lower_bound >= self.upper_bound
        ):
            raise ReviewRunDomainError("四档条件下界必须小于上界。")

    @property
    def is_categorical(self) -> bool:
        return bool(self.values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "condition_text": self.condition_text,
            "source": self.source,
            "metric": self.metric,
            "unit": self.unit,
            "observable_at": self.observable_at,
            "action": self.action,
            "lower_bound": _decimal_text(self.lower_bound),
            "upper_bound": _decimal_text(self.upper_bound),
            "lower_inclusive": self.lower_inclusive,
            "upper_inclusive": self.upper_inclusive,
            "values": list(self.values),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ScenarioBand:
        return cls(
            scenario=str(value.get("scenario", "")),
            condition_text=str(value.get("condition_text", "")),
            source=str(value.get("source", "")),
            metric=str(value.get("metric", "")),
            unit=str(value.get("unit", "")),
            observable_at=str(value.get("observable_at", "")),
            action=str(value.get("action", "")),
            lower_bound=_optional_decimal(value.get("lower_bound")),
            upper_bound=_optional_decimal(value.get("upper_bound")),
            lower_inclusive=value.get("lower_inclusive", True),
            upper_inclusive=value.get("upper_inclusive", False),
            values=_text_tuple(value, "values"),
        )


@dataclass(frozen=True)
class PlanItem:
    plan_item_id: str
    object_id: str
    object_type: str
    object_code: str
    object_name: str
    exchange: str
    sector_id: str
    role: str
    role_source: str
    role_confirmed: bool
    role_evidence_references: tuple[str, ...]
    direction_ids: tuple[str, ...]
    independent_reason: str
    hypothesis: str
    confirmation_condition: str
    invalidation_condition: str
    scenario_bands: tuple[ScenarioBand, ...]
    higher_is_better: bool | None
    condition_universe: tuple[str, ...]
    action_type: str
    max_risk: Decimal
    risk_unit: str
    evidence_references: tuple[str, ...]
    review_deadline: str

    def __post_init__(self) -> None:
        _require_text(self.plan_item_id, "计划项 ID")
        _require_text(self.object_id, "计划对象 ID")
        if self.object_type not in OBJECT_TYPES:
            raise ReviewRunDomainError("计划对象类型必须是 market、sector 或 stock。")
        if not isinstance(self.role_confirmed, bool):
            raise ReviewRunDomainError("role_confirmed 必须是布尔值。")
        if self.higher_is_better is not None and not isinstance(
            self.higher_is_better,
            bool,
        ):
            raise ReviewRunDomainError("higher_is_better 必须是布尔值或 null。")
        object.__setattr__(self, "max_risk", _decimal(self.max_risk, "最大风险"))
        _require_unique_texts(self.role_evidence_references, "角色证据引用")
        _require_unique_texts(self.direction_ids, "方向 ID")
        _require_unique_texts(self.condition_universe, "条件全集值")
        _require_unique_texts(self.evidence_references, "计划证据引用")
        _require_date(self.review_deadline, "最晚回填日期")

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_item_id": self.plan_item_id,
            "object_id": self.object_id,
            "object_type": self.object_type,
            "object_code": self.object_code,
            "object_name": self.object_name,
            "exchange": self.exchange,
            "sector_id": self.sector_id,
            "role": self.role,
            "role_source": self.role_source,
            "role_confirmed": self.role_confirmed,
            "role_evidence_references": list(self.role_evidence_references),
            "direction_ids": list(self.direction_ids),
            "independent_reason": self.independent_reason,
            "hypothesis": self.hypothesis,
            "confirmation_condition": self.confirmation_condition,
            "invalidation_condition": self.invalidation_condition,
            "scenario_bands": [band.to_dict() for band in self.scenario_bands],
            "higher_is_better": self.higher_is_better,
            "condition_universe": list(self.condition_universe),
            "action_type": self.action_type,
            "max_risk": _decimal_text(self.max_risk),
            "risk_unit": self.risk_unit,
            "evidence_references": list(self.evidence_references),
            "review_deadline": self.review_deadline,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlanItem:
        return cls(
            plan_item_id=str(value.get("plan_item_id", "")),
            object_id=str(value.get("object_id", "")),
            object_type=str(value.get("object_type", "")),
            object_code=str(value.get("object_code", "")),
            object_name=str(value.get("object_name", "")),
            exchange=str(value.get("exchange", "")),
            sector_id=str(value.get("sector_id", "")),
            role=str(value.get("role", "")),
            role_source=str(value.get("role_source", "")),
            role_confirmed=value.get("role_confirmed", False),
            role_evidence_references=_text_tuple(value, "role_evidence_references"),
            direction_ids=_text_tuple(value, "direction_ids"),
            independent_reason=str(value.get("independent_reason", "")),
            hypothesis=str(value.get("hypothesis", "")),
            confirmation_condition=str(value.get("confirmation_condition", "")),
            invalidation_condition=str(value.get("invalidation_condition", "")),
            scenario_bands=tuple(
                ScenarioBand.from_dict(_require_mapping(item, "四档条件"))
                for item in value.get("scenario_bands", ())
            ),
            higher_is_better=value.get("higher_is_better"),
            condition_universe=_text_tuple(value, "condition_universe"),
            action_type=str(value.get("action_type", "")),
            max_risk=_decimal(value.get("max_risk", "0"), "最大风险"),
            risk_unit=str(value.get("risk_unit", "")),
            evidence_references=_text_tuple(value, "evidence_references"),
            review_deadline=str(value.get("review_deadline", "")),
        )


@dataclass(frozen=True)
class RejectedPlanItem:
    plan_item_id: str
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.plan_item_id, "被拒绝计划项 ID")
        _require_text(self.reason, "计划项拒绝原因")

    def to_dict(self) -> dict[str, str]:
        return {"plan_item_id": self.plan_item_id, "reason": self.reason}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RejectedPlanItem:
        return cls(
            plan_item_id=str(value.get("plan_item_id", "")),
            reason=str(value.get("reason", "")),
        )


@dataclass(frozen=True)
class RevisionBundle:
    review_revision_id: str
    run_id: str
    trade_date: str
    evidence_bundle_id: str
    evidence_bundle_hash: str
    evidence_cutoff_at: str
    framework_version: str
    framework_hash: str
    program_version: str
    renderer_version: str
    created_at: str
    revision_kind: str
    participation_decision: str
    replaces_review_revision_id: str | None
    steps: tuple[StepAssessment, ...]
    candidate_plan_items: tuple[PlanItem, ...]
    approved_plan_items: tuple[PlanItem, ...]
    rejected_plan_items: tuple[RejectedPlanItem, ...]
    decision_note: str
    model_response_hashes: tuple[str, ...]
    content_hash: str

    def __post_init__(self) -> None:
        _require_text(self.review_revision_id, "复盘修订 ID")
        _require_text(self.run_id, "ReviewRun ID")
        _require_date(self.trade_date, "交易日期")
        _require_text(self.evidence_bundle_id, "EvidenceBundle ID")
        require_sha256(self.evidence_bundle_hash, "EvidenceBundle 哈希")
        _require_datetime(self.evidence_cutoff_at, "证据截点")
        _require_text(self.framework_version, "框架版本")
        require_sha256(self.framework_hash, "框架哈希")
        _require_text(self.program_version, "程序版本")
        _require_text(self.renderer_version, "渲染器版本")
        _require_datetime(self.created_at, "复盘修订创建时间")
        if self.revision_kind not in REVISION_KINDS:
            raise ReviewRunDomainError("修订类型必须是 draft 或 final。")
        if self.participation_decision not in PARTICIPATION_DECISIONS:
            raise ReviewRunDomainError(
                "参与决策必须是 undecided、non_participation 或 actionable。"
            )
        if self.replaces_review_revision_id is not None:
            _require_text(self.replaces_review_revision_id, "被替代修订 ID")
            if self.replaces_review_revision_id == self.review_revision_id:
                raise ReviewRunDomainError("复盘修订不能替代自身。")
        _require_unique_values(
            (item.plan_item_id for item in self.candidate_plan_items),
            "候选计划项 ID",
        )
        _require_unique_values(
            (item.plan_item_id for item in self.approved_plan_items),
            "批准计划项 ID",
        )
        _require_unique_values(
            (item.plan_item_id for item in self.rejected_plan_items),
            "被拒绝计划项 ID",
        )
        _require_unique_texts(self.model_response_hashes, "模型响应哈希")
        for response_hash in self.model_response_hashes:
            require_sha256(response_hash, "模型响应哈希")
        require_sha256(self.content_hash, "RevisionBundle 哈希", allow_empty=True)

    @classmethod
    def create(
        cls,
        *,
        review_revision_id: str,
        run_id: str,
        trade_date: str,
        evidence_bundle_id: str,
        evidence_bundle_hash: str,
        evidence_cutoff_at: str,
        framework_version: str,
        framework_hash: str,
        program_version: str,
        renderer_version: str,
        created_at: str,
        revision_kind: str,
        participation_decision: str,
        replaces_review_revision_id: str | None,
        steps: tuple[StepAssessment, ...],
        candidate_plan_items: tuple[PlanItem, ...] = (),
        approved_plan_items: tuple[PlanItem, ...] = (),
        rejected_plan_items: tuple[RejectedPlanItem, ...] = (),
        decision_note: str = "",
        model_response_hashes: tuple[str, ...] = (),
    ) -> RevisionBundle:
        bundle = cls(
            review_revision_id=review_revision_id,
            run_id=run_id,
            trade_date=trade_date,
            evidence_bundle_id=evidence_bundle_id,
            evidence_bundle_hash=evidence_bundle_hash,
            evidence_cutoff_at=evidence_cutoff_at,
            framework_version=framework_version,
            framework_hash=framework_hash,
            program_version=program_version,
            renderer_version=renderer_version,
            created_at=created_at,
            revision_kind=revision_kind,
            participation_decision=participation_decision,
            replaces_review_revision_id=replaces_review_revision_id,
            steps=steps,
            candidate_plan_items=candidate_plan_items,
            approved_plan_items=approved_plan_items,
            rejected_plan_items=rejected_plan_items,
            decision_note=decision_note,
            model_response_hashes=model_response_hashes,
            content_hash="",
        )
        return replace(bundle, content_hash=bundle.calculate_hash())

    @property
    def plan_items(self) -> tuple[PlanItem, ...]:
        if self.revision_kind == "final":
            return self.approved_plan_items
        return self.candidate_plan_items

    def calculate_hash(self) -> str:
        return canonical_hash(self._hash_payload())

    def validate_hash(self) -> None:
        require_sha256(self.content_hash, "RevisionBundle 哈希")
        if self.content_hash != self.calculate_hash():
            raise ReviewRunDomainError("RevisionBundle 内容与哈希不一致。")

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RevisionBundle:
        replaced = value.get("replaces_review_revision_id")
        bundle = cls(
            review_revision_id=str(value.get("review_revision_id", "")),
            run_id=str(value.get("run_id", "")),
            trade_date=str(value.get("trade_date", "")),
            evidence_bundle_id=str(value.get("evidence_bundle_id", "")),
            evidence_bundle_hash=str(value.get("evidence_bundle_hash", "")),
            evidence_cutoff_at=str(value.get("evidence_cutoff_at", "")),
            framework_version=str(value.get("framework_version", "")),
            framework_hash=str(value.get("framework_hash", "")),
            program_version=str(value.get("program_version", "")),
            renderer_version=str(value.get("renderer_version", "")),
            created_at=str(value.get("created_at", "")),
            revision_kind=str(value.get("revision_kind", "")),
            participation_decision=str(value.get("participation_decision", "")),
            replaces_review_revision_id=None if replaced is None else str(replaced),
            steps=tuple(
                StepAssessment.from_dict(_require_mapping(item, "STEP 评估"))
                for item in value.get("steps", ())
            ),
            candidate_plan_items=tuple(
                PlanItem.from_dict(_require_mapping(item, "候选计划项"))
                for item in value.get("candidate_plan_items", ())
            ),
            approved_plan_items=tuple(
                PlanItem.from_dict(_require_mapping(item, "批准计划项"))
                for item in value.get("approved_plan_items", ())
            ),
            rejected_plan_items=tuple(
                RejectedPlanItem.from_dict(_require_mapping(item, "被拒绝计划项"))
                for item in value.get("rejected_plan_items", ())
            ),
            decision_note=str(value.get("decision_note", "")),
            model_response_hashes=_text_tuple(value, "model_response_hashes"),
            content_hash=str(value.get("content_hash", "")),
        )
        bundle.validate_hash()
        return bundle

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "review_revision_id": self.review_revision_id,
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "evidence_bundle_id": self.evidence_bundle_id,
            "evidence_bundle_hash": self.evidence_bundle_hash,
            "evidence_cutoff_at": self.evidence_cutoff_at,
            "framework_version": self.framework_version,
            "framework_hash": self.framework_hash,
            "program_version": self.program_version,
            "renderer_version": self.renderer_version,
            "created_at": self.created_at,
            "revision_kind": self.revision_kind,
            "participation_decision": self.participation_decision,
            "replaces_review_revision_id": self.replaces_review_revision_id,
            "steps": [step.to_dict() for step in self.steps],
            "candidate_plan_items": [item.to_dict() for item in self.candidate_plan_items],
            "approved_plan_items": [item.to_dict() for item in self.approved_plan_items],
            "rejected_plan_items": [item.to_dict() for item in self.rejected_plan_items],
            "decision_note": self.decision_note,
            "model_response_hashes": list(self.model_response_hashes),
        }


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    status: str
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.gate_id not in GATE_IDS:
            raise ReviewRunDomainError(f"未知质量门禁：{self.gate_id}")
        if self.status not in GATE_STATUSES:
            raise ReviewRunDomainError(f"未知质量门禁状态：{self.status}")
        _require_unique_texts(self.issues, "质量问题")
        if self.status == "failed" and not self.issues:
            raise ReviewRunDomainError("失败的质量门禁必须说明原因。")
        if self.status != "failed" and self.issues:
            raise ReviewRunDomainError("通过或不适用的质量门禁不能携带失败原因。")

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "status": self.status,
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class QualityAssessment:
    level: str
    publishable: bool
    gates: tuple[GateResult, ...]

    def __post_init__(self) -> None:
        if self.level not in QUALITY_LEVELS:
            raise ReviewRunDomainError(f"未知复盘质量等级：{self.level}")
        if tuple(gate.gate_id for gate in self.gates) != GATE_IDS:
            raise ReviewRunDomainError("质量结果必须按 G1-G6 完整排列。")

    @property
    def issues(self) -> tuple[str, ...]:
        return tuple(issue for gate in self.gates for issue in gate.issues)

    def gate_status(self, gate_id: str) -> str:
        for gate in self.gates:
            if gate.gate_id == gate_id:
                return gate.status
        raise ReviewRunDomainError(f"未知质量门禁：{gate_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "publishable": self.publishable,
            "gates": [gate.to_dict() for gate in self.gates],
        }


# 质量判定先收集事实与哈希类 P0，再判断无行动的明确不参与，不能用决策标签绕过冲突。
def assess_revision_quality(bundle: RevisionBundle) -> QualityAssessment:
    issues: dict[str, list[str]] = {gate_id: [] for gate_id in GATE_IDS}

    try:
        bundle.validate_hash()
    except ReviewRunDomainError as error:
        issues["G5"].append(str(error))

    _collect_step_integrity_issues(bundle, issues)
    _collect_step_nine_pattern_issues(bundle, issues["G1"])
    _collect_revision_identity_issues(bundle, issues)
    _collect_finality_issues(bundle, issues)

    action_structure = _has_action_structure(bundle)
    if not action_structure and _is_explicit_non_participation(bundle):
        _collect_non_participation_core_issues(bundle, issues["G1"])
        steps = {step.step_number: step for step in bundle.steps}
        for step_number in (7, 8, 10):
            step = steps.get(step_number)
            if step is not None and step.status != "not_applicable":
                issues["G2"].append(
                    f"non_participation 的 STEP {step_number} 必须标记为 not_applicable。"
                )
        for gate_id in ("G2", "G3", "G6"):
            if not issues[gate_id]:
                issues[gate_id] = []
        gates = tuple(
            GateResult(
                gate_id=gate_id,
                status=(
                    "failed"
                    if issues[gate_id]
                    else "not_applicable"
                    if gate_id in {"G2", "G3", "G6"}
                    else "passed"
                ),
                issues=tuple(issues[gate_id]),
            )
            for gate_id in GATE_IDS
        )
        blocked = any(gate.status == "failed" for gate in gates)
        return QualityAssessment(
            level="blocked" if blocked else "non_participation",
            publishable=not blocked,
            gates=gates,
        )

    _collect_object_chain_issues(bundle, issues["G2"])
    _collect_scenario_issues(bundle, issues["G3"])
    _collect_observation_readiness_issues(bundle, issues["G6"])
    if bundle.participation_decision == "non_participation" and action_structure:
        issues["G4"].append("non_participation 修订仍包含行动对象、条件或计划项。")

    gates = tuple(
        GateResult(
            gate_id=gate_id,
            status="failed" if issues[gate_id] else "passed",
            issues=tuple(issues[gate_id]),
        )
        for gate_id in GATE_IDS
    )
    blocked = any(gate.status == "failed" for gate in gates)
    return QualityAssessment(
        level="blocked" if blocked else "actionable",
        publishable=not blocked,
        gates=gates,
    )


# 发布校验返回完整门禁结果；调用方可直接展示全部失败原因。
def validate_revision_bundle(bundle: RevisionBundle) -> QualityAssessment:
    assessment = assess_revision_quality(bundle)
    if not assessment.publishable:
        raise ReviewRunDomainError("RevisionBundle 不可发布：" + "；".join(assessment.issues))
    return assessment


def _collect_step_integrity_issues(
    bundle: RevisionBundle,
    issues: dict[str, list[str]],
) -> None:
    step_numbers = tuple(step.step_number for step in bundle.steps)
    if len(step_numbers) != len(set(step_numbers)):
        issues["G1"].append("RevisionBundle 包含重复 STEP。")
    if set(step_numbers) != set(STEP_NUMBERS):
        issues["G1"].append("RevisionBundle 必须完整包含 STEP 1-10。")
    for step in bundle.steps:
        for issue in step.integrity_issues:
            issues["G1"].append(f"STEP {step.step_number}：{issue}")
        # 最终发布不能把本地占位当作业务结论；不参与与行动修订遵守相同内容门禁。
        if bundle.revision_kind == "final":
            texts = (step.conclusion, *step.counter_evidence, *step.open_questions,
                     *step.next_day_checks, *step.evidence_gaps)
            if any(_is_final_placeholder(text) for text in texts):
                issues["G1"].append(f"STEP {step.step_number} 仍含未处理通用模板，必须完成业务修订。")
            if step.status != "not_applicable" and (
                not step.conclusion.strip()
                or not (step.evidence_references or step.evidence_gaps)
            ):
                issues["G1"].append(f"STEP {step.step_number} 必须有具体证据结论或具体不足说明。")
    if bundle.revision_kind == "final" and bundle.candidate_plan_items:
        issues["G4"].append("最终 RevisionBundle 不能保留 candidate_plan_items。")
    if bundle.revision_kind == "draft" and bundle.approved_plan_items:
        issues["G4"].append("草案 RevisionBundle 不能保存 approved_plan_items。")
    candidate_ids = {item.plan_item_id for item in bundle.candidate_plan_items}
    approved_ids = {item.plan_item_id for item in bundle.approved_plan_items}
    rejected_ids = {item.plan_item_id for item in bundle.rejected_plan_items}
    if (candidate_ids | approved_ids) & rejected_ids:
        issues["G4"].append("同一计划项不能同时出现在保留集合和拒绝集合。")


# 最终修订先核对 STEP 9 对象投影；完成态还必须携带十二项结构化结果。
def _collect_step_nine_pattern_issues(
    bundle: RevisionBundle,
    issues: list[str],
) -> None:
    step_nine = next(
        (step for step in bundle.steps if step.step_number == 9),
        None,
    )
    if step_nine is None:
        return
    patterns = step_nine.pattern_assessments
    if bundle.revision_kind == "final" and patterns:
        pattern_objects = {
            object_id
            for pattern in patterns
            for object_id in pattern.object_ids
        }
        if pattern_objects != set(step_nine.object_ids):
            issues.append("STEP 9 对象必须与十二模式逐项核验中的对象集合一致。")
        step_references = set(step_nine.evidence_references)
        for pattern in patterns:
            if set(pattern.evidence_references) - step_references:
                issues.append(
                    f"STEP 9 模式 {pattern.pattern_number} 引用了本 STEP 之外的证据。"
                )
    if step_nine.status != "complete":
        return
    if tuple(sorted(pattern.pattern_number for pattern in patterns)) != tuple(range(1, 13)):
        issues.append("STEP 9 完成态必须按编号完整保存十二模式逐项核验。")
        return
    for pattern in patterns:
        prefix = f"STEP 9 模式 {pattern.pattern_number}"
        if pattern.status == "confirmed":
            if not pattern.object_ids:
                issues.append(f"{prefix} 确认成立时必须绑定具体对象。")
            if not pattern.evidence_references:
                issues.append(f"{prefix} 确认成立时必须绑定证据引用。")
            if pattern.missing_evidence:
                issues.append(f"{prefix} 确认成立时不能保留证据缺口。")
        elif pattern.status in {
            "unavailable",
            "base_evidence_available_trigger_pending",
            "background_matched_trigger_pending",
        } and not pattern.missing_evidence:
            issues.append(f"{prefix} 的待核验状态必须明确记录证据缺口。")
        elif pattern.status == "assessment_pending":
            issues.append(f"{prefix} 尚未形成逐项核验结果，不能标记 STEP 9 完成。")


def _is_final_placeholder(value: str) -> bool:
    normalized = re.sub(r"[\s，,。；;！？!?：:]+", "", value)
    return normalized in {
        re.sub(r"[\s，,。；;！？!?：:]+", "", template)
        for template in FINAL_PLACEHOLDER_TEXTS
    }


def _collect_revision_identity_issues(
    bundle: RevisionBundle,
    issues: dict[str, list[str]],
) -> None:
    for item in bundle.plan_items:
        prefix = f"计划项 {item.plan_item_id}"
        if not item.object_name.strip():
            issues["G1"].append(f"{prefix} 缺少已确认对象名称。")
        if not item.role_confirmed or not item.role.strip() or not item.role_source.strip():
            issues["G1"].append(f"{prefix} 的市场角色没有来源确认。")
        if not item.role_evidence_references:
            issues["G1"].append(f"{prefix} 缺少角色证据引用。")
        if item.object_type == "stock":
            if not STOCK_CODE_PATTERN.fullmatch(item.object_code):
                issues["G1"].append(f"{prefix} 的股票代码不是六位数字。")
            if item.exchange not in STOCK_EXCHANGES:
                issues["G1"].append(f"{prefix} 缺少合法交易所。")
            if not item.sector_id.strip():
                issues["G1"].append(f"{prefix} 缺少板块归属。")


def _collect_finality_issues(
    bundle: RevisionBundle,
    issues: dict[str, list[str]],
) -> None:
    if bundle.revision_kind != "final":
        issues["G4"].append("只有 final RevisionBundle 可以发布。")
    if bundle.participation_decision == "undecided":
        issues["G4"].append("最终发布前必须形成明确参与决策。")
    if not bundle.decision_note.strip():
        issues["G4"].append("最终参与决策必须记录确认说明。")
    elif bundle.revision_kind == "final" and _is_final_placeholder(bundle.decision_note):
        issues["G4"].append("最终参与决策仍含未处理通用模板，必须记录具体确认说明。")


# 不参与是基于完整复盘形成的业务结论，不能把核心证据不足包装成正式空仓报告。
def _collect_non_participation_core_issues(
    bundle: RevisionBundle,
    issues: list[str],
) -> None:
    steps = {step.step_number: step for step in bundle.steps}
    for step_number in NON_PARTICIPATION_CORE_STEP_NUMBERS:
        step = steps.get(step_number)
        if step is None:
            continue
        missing_conditions: list[str] = []
        if step.status != "complete":
            missing_conditions.append(f"状态为 {step.status}")
        if not step.evidence_references:
            missing_conditions.append("缺少证据引用")
        # STEP 9 在背景已判定不匹配后不再要求补齐该模式的触发证据，
        # 其余核心步骤的最低证据缺口必须在正式不参与结论前清零。
        if step_number != 9 and step.evidence_gaps:
            missing_conditions.append(f"仍有 {len(step.evidence_gaps)} 项证据缺口")
        if step_number == 9:
            unresolved_patterns = tuple(
                pattern.pattern_number
                for pattern in step.pattern_assessments
                if pattern.status not in STEP9_FINAL_PATTERN_STATUSES
            )
            if unresolved_patterns:
                missing_conditions.append(
                    "模式尚未完成核验："
                    + "、".join(str(number) for number in unresolved_patterns)
                )
        if missing_conditions:
            issues.append(
                f"non_participation 的核心分析 STEP {step_number} 未达到可发布条件（"
                + "；".join(missing_conditions)
                + "），证据不足或分析未完成时不能正式发布。"
            )


def _collect_object_chain_issues(bundle: RevisionBundle, issues: list[str]) -> None:
    steps = {step.step_number: step for step in bundle.steps}
    if set(steps) != set(STEP_NUMBERS):
        return
    direction_ids = set(steps[4].direction_ids) | set(steps[5].direction_ids)
    step_six_objects = set(steps[6].object_ids)
    step_seven_objects = set(steps[7].object_ids)
    plan_items = bundle.plan_items
    plan_ids = {item.plan_item_id for item in plan_items}
    plan_objects = {item.object_id for item in plan_items}

    if not step_seven_objects.issubset(step_six_objects):
        issues.append("STEP 7 对象必须来自 STEP 6 的角色与身份集合。")
    if step_seven_objects != plan_objects:
        issues.append("STEP 7 对象必须与当前修订的计划对象完全一致。")
    for step_number in (8, 10):
        if set(steps[step_number].object_ids) != plan_objects:
            issues.append(f"STEP {step_number} 对象必须与 PlanItem 完全一致。")
        if set(steps[step_number].plan_item_ids) != plan_ids:
            issues.append(f"STEP {step_number} 必须投影当前修订的全部 PlanItem。")
    for item in plan_items:
        unknown_directions = set(item.direction_ids) - direction_ids
        if unknown_directions:
            issues.append(f"计划项 {item.plan_item_id} 引用了 STEP 4/5 之外的方向。")
        if not item.direction_ids and not item.independent_reason.strip():
            issues.append(
                f"计划项 {item.plan_item_id} 未关联 STEP 4/5 方向，也未说明独立原因。"
            )


def _collect_scenario_issues(bundle: RevisionBundle, issues: list[str]) -> None:
    for item in bundle.plan_items:
        item_issues = _validate_scenario_bands(item)
        issues.extend(f"计划项 {item.plan_item_id}：{issue}" for issue in item_issues)


def _validate_scenario_bands(item: PlanItem) -> tuple[str, ...]:
    issues: list[str] = []
    scenarios = tuple(band.scenario for band in item.scenario_bands)
    if len(scenarios) != len(set(scenarios)) or set(scenarios) != SCENARIOS:
        return ("必须各有一档符合、超预期、不及预期和放弃条件。",)
    signatures = {
        (band.source, band.metric, band.unit, band.observable_at)
        for band in item.scenario_bands
    }
    if len(signatures) != 1:
        issues.append("四档条件必须使用同一来源、主指标、单位和可观察时点。")
    categorical = [band.is_categorical for band in item.scenario_bands]
    if any(categorical) and not all(categorical):
        issues.append("四档条件不能混用数值边界和枚举值。")
    elif all(categorical):
        if item.higher_is_better is not None:
            issues.append("枚举四档不应声明 higher_is_better。")
        universe = set(item.condition_universe)
        if not universe:
            issues.append("枚举四档必须声明完整条件全集。")
        seen: set[str] = set()
        for band in item.scenario_bands:
            values = set(band.values)
            if seen & values:
                issues.append("枚举四档条件存在重叠。")
                break
            seen.update(values)
        if seen != universe:
            issues.append("枚举四档条件没有完整覆盖声明的条件全集。")
    else:
        if item.higher_is_better is None:
            issues.append("数值四档必须声明指标方向。")
        if item.condition_universe:
            issues.append("数值四档不能声明枚举条件全集。")
        issues.extend(_validate_numeric_partition(item.scenario_bands, item.higher_is_better))
    return tuple(issues)


# 数值四档必须连续覆盖负无穷到正无穷，且相邻边界只能归属其中一档。
def _validate_numeric_partition(
    bands: tuple[ScenarioBand, ...],
    higher_is_better: bool | None,
) -> tuple[str, ...]:
    if higher_is_better is None:
        return ()
    ordered_scenarios = (
        ("abandon", "under_expectation", "expectation", "over_expectation")
        if higher_is_better
        else ("over_expectation", "expectation", "under_expectation", "abandon")
    )
    by_scenario = {band.scenario: band for band in bands}
    ordered = [by_scenario[name] for name in ordered_scenarios]
    issues: list[str] = []
    if ordered[0].lower_bound is not None or ordered[-1].upper_bound is not None:
        issues.append("数值四档必须覆盖无界的最低端和最高端。")
    for left, right in zip(ordered, ordered[1:]):
        if left.upper_bound is None or right.lower_bound is None:
            issues.append("数值四档中间边界不能为空。")
            continue
        if left.upper_bound != right.lower_bound:
            issues.append("数值四档之间存在重叠或空档。")
        elif left.upper_inclusive == right.lower_inclusive:
            issues.append("数值四档相邻边界必须且只能归属其中一档。")
    return tuple(dict.fromkeys(issues))


def _collect_observation_readiness_issues(
    bundle: RevisionBundle,
    issues: list[str],
) -> None:
    if bundle.participation_decision != "actionable":
        issues.append("包含行动结构的修订必须明确标记为 actionable。")
    if not bundle.plan_items:
        issues.append("actionable 修订至少需要一个 PlanItem。")
    for step in bundle.steps:
        if step.status != "complete":
            issues.append(f"actionable 修订的 STEP {step.step_number} 必须完整完成。")
    for item in bundle.plan_items:
        prefix = f"计划项 {item.plan_item_id}"
        for label, value in (
            ("可验证假设", item.hypothesis),
            ("成立条件", item.confirmation_condition),
            ("失效条件", item.invalidation_condition),
            ("行动类型", item.action_type),
            ("风险单位", item.risk_unit),
        ):
            if not value.strip():
                issues.append(f"{prefix} 缺少{label}。")
        if item.max_risk <= 0:
            issues.append(f"{prefix} 的最大风险必须大于 0。")
        if not item.evidence_references:
            issues.append(f"{prefix} 缺少 Observation 所需证据引用。")


def _has_action_structure(bundle: RevisionBundle) -> bool:
    if bundle.candidate_plan_items or bundle.approved_plan_items:
        return True
    for step in bundle.steps:
        if step.step_number in {7, 8, 10} and (step.object_ids or step.plan_item_ids):
            return True
    return False


def _is_explicit_non_participation(bundle: RevisionBundle) -> bool:
    return (
        bundle.revision_kind == "final"
        and bundle.participation_decision == "non_participation"
        and bool(bundle.decision_note.strip())
    )


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ReviewRunDomainError(f"{label}必须是十进制数。") from error
    if not result.is_finite():
        raise ReviewRunDomainError(f"{label}必须是有限十进制数。")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, "四档边界")


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReviewRunDomainError(f"{label}不能为空。")


def _require_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ReviewRunDomainError(f"{label}必须是 YYYY-MM-DD。") from error


def _require_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ReviewRunDomainError(f"{label}必须是 ISO 8601 时间。") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewRunDomainError(f"{label}必须包含时区。")
    return parsed


def _require_unique_texts(values: tuple[str, ...], label: str) -> None:
    for value in values:
        _require_text(value, label)
    _require_unique_values(values, label)


def _require_unique_values(values: Any, label: str) -> None:
    normalized = tuple(values)
    if len(normalized) != len(set(normalized)):
        raise ReviewRunDomainError(f"{label}不能重复。")


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewRunDomainError(f"{label}必须是 JSON 对象。")
    return value


def _text_tuple(value: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw = value.get(key, ())
    if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) for item in raw):
        raise ReviewRunDomainError(f"{key} 必须是字符串数组。")
    return tuple(raw)
