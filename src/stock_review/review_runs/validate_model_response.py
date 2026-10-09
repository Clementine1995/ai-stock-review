# 本文件校验 RevisionBundle 的证据引用、对象身份和模型响应字段边界，不调用模型。

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
import re
from typing import Any, Mapping

from stock_review.evidence.build_evidence_bundle import read_capture_payload
from stock_review.review_runs.canonical_hash import canonical_json
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.evidence_bundle import EvidenceBundle, EvidenceCapture
from stock_review.review_runs.evaluate_capture_predicate import merge_normalized_evidence
from stock_review.review_runs.review_revision import RevisionBundle
from stock_review.scoring.verify_step9_patterns import STEP9_PATTERN_DEFINITIONS
from stock_review.storage.review_run_repository import ReviewRunRepository


MODEL_RESPONSE_FIELDS = {"steps"}
MODEL_STEP_FIELDS = {
    "step_number",
    "conclusion",
    "counter_evidence",
    "open_questions",
}

# 可行动修订必须由本 STEP 自己引用的实际字段支撑；每个内层元组是一组可替代能力。
STEP_EVIDENCE_REQUIREMENTS = {
    1: (("market_indices",), ("market_turnover",)),
    2: (("sentiment_core",), ("stock_style_sample",)),
    3: (("sentiment_core",),),
    4: (("sectors",),),
    5: (("sectors",), ("sector_history",)),
    6: (("stock_identity_role",), ("sectors",)),
    7: (("stock_identity_role",),),
    8: (("market",), ("sectors",), ("stock_identity_role",)),
    10: (("market",), ("sentiment_core",), ("sectors",), ("stock_identity_role",)),
}
EVIDENCE_CAPABILITY_LABELS = {
    "market": "市场事实",
    "market_indices": "指数事实",
    "market_turnover": "两市量能",
    "sentiment": "情绪事实",
    "sentiment_core": "涨跌停、炸板率和连板高度",
    "sectors": "板块事实",
    "sector_history": "板块连续比较",
    "stocks": "个股事实",
    "stock_style_sample": "至少两个可比较个股的风格样本",
    "stock_identity_role": "个股身份、板块和角色来源",
    "candidate_history": "有限候选历史",
    "daily_kline": "日线",
    "call_auction": "竞价",
    "intraday": "分时",
    "previous_day_feedback": "前日反馈",
    "next_day_feedback": "次日反馈",
    "regulatory_event": "监管事实",
}
SENTIMENT_CORE_FIELDS = (
    "limit_up_count",
    "limit_down_count",
    "broken_board_rate",
    "highest_board",
)
PLACEHOLDER_TEXTS = {"待确认", "未知", "无", "n/a", "null", "-", "--"}
INDEX_PERFORMANCE_FIELDS = {"close", "change_percent", "open", "high", "low"}
SECTOR_PERFORMANCE_FIELDS = {
    "change_percent",
    "rank",
    "limit_up_count",
    "up_count",
    "down_count",
    "net_inflow",
    "net_inflow_yi",
    "turnover",
    "turnover_rate",
    "leading_stock_change_percent",
}
STOCK_PERFORMANCE_FIELDS = {
    "close",
    "change_percent",
    "return_5d_percent",
    "return_20d_percent",
    "amount",
    "average_5d_amount",
    "circulating_market_value",
    "turnover",
    "volume",
}
PATTERN_TRIGGER_FIELDS = {
    "daily_kline": ("daily_kline", "daily_bars"),
    "call_auction": ("call_auction",),
    "intraday": ("intraday",),
    "previous_day_feedback": ("previous_day_feedback",),
    "next_day_feedback": ("next_day_feedback",),
    "regulatory_event": ("regulatory_event",),
}
STEP_ALLOWED_NORMALIZED_FIELDS = {
    1: {
        "market",
        "index_history",
        "market_breadth",
        "us_market",
    },
    2: {"sentiment", "stocks", "candidate_history", "stock_history"},
    3: {"sentiment", "previous_day_feedback"},
    4: {"sectors"},
    5: {"sectors", "sector_history"},
    6: {"sectors", "stocks", "candidate_history", "stock_history"},
    7: {"sectors", "stocks", "candidate_history", "stock_history"},
    8: {"market", "sentiment", "sectors", "stocks"},
    9: {
        "market",
        "sentiment",
        "sectors",
        "stocks",
        "candidate_history",
        "stock_history",
        "call_auction",
        "intraday",
        "previous_day_feedback",
        "next_day_feedback",
        "regulatory_event",
    },
    10: {
        "market",
        "sentiment",
        "sectors",
        "stocks",
        "index_history",
        "market_breadth",
        "us_market",
        "sector_history",
        "candidate_history",
        "stock_history",
        "call_auction",
        "intraday",
        "previous_day_feedback",
        "next_day_feedback",
        "regulatory_event",
    },
}
NORMALIZED_METADATA_FIELDS = {
    "trade_date",
    "sample_date",
    "source",
}
FACT_ASSERTION_TERMS = (
    "唯一",
    "主线",
    "龙头",
    "核心",
    "领涨",
    "领跌",
    "涨停",
    "跌停",
    "高开",
    "低开",
    "炸板",
    "回封",
    "放量",
    "缩量",
    "强势",
    "弱势",
    "共识",
    "晋级",
    "退潮",
    "冰点",
    "主升",
    "轮动",
    "突破",
    "承接",
    "破位",
    "一字",
    "监管",
    "异动",
)
ENTITY_ASSERTION_PATTERN = re.compile(
    r"(?:^|[，。；！？\n])"
    r"(?P<subject>[A-Za-z\u4e00-\u9fff][A-Za-z0-9·\u4e00-\u9fff]{1,23}?)"
    r"(?:是|为|成为|属于|上涨|下跌|涨停|跌停|高开|低开|领涨|领跌|突破|回落|收于|报收|封板|炸板|回封|是否)"
)
ENTITY_PERFORMANCE_PATTERN = re.compile(
    r"(?:^|[，。；！？\n])"
    r"(?P<subject>[A-Za-z\u4e00-\u9fff][A-Za-z0-9·\u4e00-\u9fff]{1,23}?)"
    r"(?:今日|当日|盘中|开盘|收盘)"
    r"(?:表现|走势|涨幅|跌幅|量能|成交|价格|状态|反馈)"
)
ISO_DATE_PATTERN = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
GENERIC_ASSERTION_SUBJECTS = (
    "本步",
    "本次",
    "当前",
    "冻结证据",
    "证据",
    "数据",
    "结论",
    "结果",
    "情况",
    "市场",
    "情绪",
    "板块",
    "指数",
    "次日",
)


# 模型只能修改解释、反证和待确认项，不得新增对象、事实字段、阈值或计划结构。
def validate_model_response_shape(response: Mapping[str, Any]) -> None:
    unexpected = set(response) - MODEL_RESPONSE_FIELDS
    if unexpected:
        raise ReviewRunDomainError(
            f"模型响应包含未授权字段：{', '.join(sorted(unexpected))}"
        )
    steps = response.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ReviewRunDomainError("模型响应必须包含非空 steps 数组。")
    numbers: list[int] = []
    for item in steps:
        if not isinstance(item, Mapping):
            raise ReviewRunDomainError("模型 steps 中每一项必须是 JSON 对象。")
        extra_fields = set(item) - MODEL_STEP_FIELDS
        if extra_fields:
            raise ReviewRunDomainError(
                f"模型 STEP 包含未授权字段：{', '.join(sorted(extra_fields))}"
            )
        step_number = item.get("step_number")
        if type(step_number) is not int:
            raise ReviewRunDomainError("模型 STEP 编号必须是 JSON 整数。")
        if step_number not in range(1, 11):
            raise ReviewRunDomainError("模型 STEP 编号必须在 1 到 10 之间。")
        numbers.append(step_number)
        conclusion = item.get("conclusion")
        if not isinstance(conclusion, str) or not conclusion.strip():
            raise ReviewRunDomainError("模型 STEP conclusion 不能为空。")
        for field_name in ("counter_evidence", "open_questions"):
            value = item.get(field_name, [])
            if not isinstance(value, list) or any(
                not isinstance(entry, str) or not entry.strip() for entry in value
            ):
                raise ReviewRunDomainError(f"模型 STEP {field_name} 必须是非空文本数组或空数组。")
    if len(numbers) != len(set(numbers)):
        raise ReviewRunDomainError("模型响应不能重复修改同一个 STEP。")


# 完整日期独立校验；代码、百分比、小数和一至五位整数必须来自该 STEP 已绑定证据。
def validate_model_narrative_facts(
    response: Mapping[str, Any],
    base_revision: RevisionBundle,
    evidence_bundle: EvidenceBundle,
    repository: ReviewRunRepository,
) -> None:
    captures_by_reference: dict[str, EvidenceCapture] = {}
    normalized_by_reference: dict[str, Any] = {}
    for capture in evidence_bundle.captures:
        if capture.status != "available":
            continue
        record = repository.get_evidence_capture(f"CAPTURE-{capture.operation_id}")
        if record is None:
            raise ReviewRunDomainError(f"EvidenceCapture 不存在：{capture.slot_id}")
        payload = read_capture_payload(
            Path(record["payload"]["output_path"]), capture
        )
        reference = f"capture:{capture.slot_id}:{capture.normalized_evidence_hash}"
        captures_by_reference[reference] = capture
        normalized_by_reference[reference] = payload.get("normalized")
    steps = {step.step_number: step for step in base_revision.steps}
    for patch in response["steps"]:
        step_number = patch["step_number"]
        base_step = steps.get(step_number)
        if base_step is None:
            raise ReviewRunDomainError(f"模型响应包含未知 STEP：{step_number}")
        allowed_values = tuple(
            project_normalized_evidence_for_step(
                captures_by_reference[reference],
                normalized_by_reference[reference],
                step_number,
            )
            for reference in base_step.evidence_references
            if reference in captures_by_reference
        )
        allowed_text = "\n".join(canonical_json(value) for value in allowed_values)
        base_text = "\n".join(
            (
                base_step.conclusion,
                *base_step.counter_evidence,
                *base_step.open_questions,
            )
        )
        allowed_entities = {
            entity
            for value in allowed_values
            for entity in _collect_named_entities(value)
        }
        narrative = "\n".join(
            [patch["conclusion"]]
            + list(patch.get("counter_evidence", []))
            + list(patch.get("open_questions", []))
        )
        allowed_tokens = _fact_tokens(allowed_text)
        unsupported = sorted(_fact_tokens(narrative) - allowed_tokens)
        allowed_dates = _date_tokens(allowed_text) | {base_revision.trade_date}
        unsupported_dates = sorted(_date_tokens(narrative) - allowed_dates)
        if unsupported or unsupported_dates:
            raise ReviewRunDomainError(
                f"模型 STEP {step_number} 引用了本 STEP 冻结证据之外的日期、代码或数值："
                + "、".join((*unsupported_dates, *unsupported))
            )
        _validate_model_entity_and_claim_terms(
            step_number,
            patch,
            allowed_text,
            base_text,
            allowed_entities,
        )


def _fact_tokens(value: str) -> set[str]:
    value = ISO_DATE_PATTERN.sub(" ", value)
    tokens = {
        _normalize_fact_token(item)
        for item in re.findall(r"(?<!\d)\d{6}(?!\d)", value)
    }
    tokens.update(
        _normalize_fact_token(match.group(0))
        for match in re.finditer(r"(?<![\d.])\d+\.\d+%?|(?<!\d)\d{3,5}%?", value)
    )
    tokens.update(
        _normalize_fact_token(match.group(0))
        for match in re.finditer(r"(?<![\d.])\d{1,2}%", value)
    )
    tokens.update(
        _normalize_fact_token(match.group(0))
        for match in re.finditer(r"(?<![\d.])\d{1,2}(?![\d.])", value)
    )
    return tokens


def _date_tokens(value: str) -> set[str]:
    return set(ISO_DATE_PATTERN.findall(value))


def _normalize_fact_token(value: str) -> str:
    token = value.removesuffix("%")
    if len(token) == 6 and token.isdigit():
        return token
    try:
        number = Decimal(token)
    except InvalidOperation:
        return token
    return format(number.normalize(), "f")


# 同一 snapshot 在不同 STEP 只暴露职责允许的 normalized 根字段，避免其它域实体和数值串入模型白名单。
def project_normalized_evidence_for_step(
    capture: EvidenceCapture,
    normalized: Any,
    step_number: int,
) -> Any:
    allowed_fields = STEP_ALLOWED_NORMALIZED_FIELDS[step_number]
    normalized_scope = capture.scope.replace("-", "_")
    if isinstance(normalized, list):
        return normalized if normalized_scope in allowed_fields else []
    if not isinstance(normalized, Mapping):
        return {}
    return {
        field_name: value
        for field_name, value in normalized.items()
        if field_name in NORMALIZED_METADATA_FIELDS
        or field_name.replace("-", "_") in allowed_fields
    }


# 本地确定性边界只识别证据中的显式实体，以及结论性语句里的高风险角色和状态词。
def _validate_model_entity_and_claim_terms(
    step_number: int,
    patch: Mapping[str, Any],
    allowed_evidence_text: str,
    base_text: str,
    allowed_entities: set[str],
) -> None:
    claimed_texts = [str(patch["conclusion"]), *map(str, patch.get("counter_evidence", []))]
    all_texts = [*claimed_texts, *map(str, patch.get("open_questions", []))]
    unknown_subjects: set[str] = set()
    for text in all_texts:
        for pattern in (ENTITY_ASSERTION_PATTERN, ENTITY_PERFORMANCE_PATTERN):
            for match in pattern.finditer(text):
                subject = _normalize_assertion_subject(match.group("subject"))
                if _is_generic_assertion_subject(subject):
                    continue
                if any(
                    subject == entity or subject.startswith(entity)
                    for entity in allowed_entities
                ):
                    continue
                unknown_subjects.add(subject)

    grounded_text = allowed_evidence_text + "\n" + base_text
    unsupported_terms = {
        term
        for text in claimed_texts
        if not _is_explicitly_uncertain(text)
        for term in FACT_ASSERTION_TERMS
        if term in text and term not in grounded_text
    }
    if unknown_subjects or unsupported_terms:
        details: list[str] = []
        if unknown_subjects:
            details.append("未冻结实体=" + "、".join(sorted(unknown_subjects)))
        if unsupported_terms:
            details.append("无来源事实标签=" + "、".join(sorted(unsupported_terms)))
        raise ReviewRunDomainError(
            f"模型 STEP {step_number} 新增了本 STEP 冻结证据之外的实体或事实断言："
            + "；".join(details)
        )


def _is_explicitly_uncertain(value: str) -> bool:
    return any(
        marker in value
        for marker in ("如果", "若", "可能", "待确认", "未确认", "无法确认")
    )


def _normalize_assertion_subject(value: str) -> str:
    result = value
    for prefix in ("今日", "当日", "盘中", "开盘", "收盘"):
        if result.startswith(prefix) and len(result) > len(prefix):
            result = result.removeprefix(prefix)
            break
    return result


def _is_generic_assertion_subject(value: str) -> bool:
    if value in GENERIC_ASSERTION_SUBJECTS:
        return True
    return any(
        value == prefix + suffix
        for prefix in ("本步", "本次", "当前", "冻结")
        for suffix in ("证据", "数据", "结论", "结果", "情况", "市场", "情绪", "板块", "指数")
    )


def _collect_named_entities(value: Any) -> set[str]:
    entities: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {
                "name",
                "code",
                "symbol",
                "title",
                "sector",
                "sector_id",
                "sector_name",
                "object_name",
                "object_code",
                "leading_stock",
                "stock_name",
                "index_name",
                "core_stocks",
                "sectors",
            } and _has_usable_value(item):
                if isinstance(item, (list, tuple)):
                    entities.update(
                        str(entry).strip()
                        for entry in item
                        if isinstance(entry, (str, int, float))
                        and not isinstance(entry, bool)
                        and _has_usable_value(entry)
                    )
                else:
                    entities.add(str(item).strip())
            entities.update(_collect_named_entities(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            entities.update(_collect_named_entities(item))
    return entities


# 所有证据引用必须精确指向当前 EvidenceBundle；股票身份与角色要能在冻结正文中回查。
def validate_revision_evidence(
    revision: RevisionBundle,
    evidence_bundle: EvidenceBundle,
    repository: ReviewRunRepository,
) -> None:
    revision.validate_hash()
    evidence_bundle.validate_hash()
    if (
        revision.run_id != evidence_bundle.run_id
        or revision.trade_date != evidence_bundle.trade_date
        or revision.evidence_bundle_id != evidence_bundle.evidence_bundle_id
        or revision.evidence_bundle_hash != evidence_bundle.content_hash
    ):
        raise ReviewRunDomainError("RevisionBundle 未绑定当前 EvidenceBundle。")
    allowed_references = {
        f"capture:{capture.slot_id}:{capture.normalized_evidence_hash}"
        for capture in evidence_bundle.captures
        if capture.status == "available"
    }
    used_references = {
        reference for step in revision.steps for reference in step.evidence_references
    }
    for plan in revision.plan_items:
        used_references.update(plan.evidence_references)
        used_references.update(plan.role_evidence_references)
    unexpected = used_references - allowed_references
    if unexpected:
        raise ReviewRunDomainError(
            f"RevisionBundle 引用了当前 EvidenceBundle 之外的证据：{', '.join(sorted(unexpected))}"
        )

    payload_by_reference = _load_capture_payloads(evidence_bundle, repository)
    evidence = merge_normalized_evidence(tuple(payload_by_reference.values()))
    _validate_actionable_step_evidence(revision, evidence_bundle, payload_by_reference)
    stock_records = _index_stock_records(evidence.get("stocks"))
    for plan in revision.plan_items:
        if plan.object_type != "stock":
            continue
        record = stock_records.get(plan.object_code)
        if record is None:
            raise ReviewRunDomainError(f"计划股票不在冻结证据对象中：{plan.object_code}")
        for field_name, actual in (
            ("name", plan.object_name),
            ("exchange", plan.exchange),
        ):
            expected = str(record.get(field_name) or "")
            if not expected or expected != actual:
                raise ReviewRunDomainError(
                    f"计划股票 {plan.object_code} 的 {field_name} 与冻结证据不一致。"
                )
        evidence_sectors = _stock_sectors(record)
        if plan.sector_id not in evidence_sectors:
            raise ReviewRunDomainError(
                f"计划股票 {plan.object_code} 的板块 {plan.sector_id} 未被冻结证据确认。"
            )
        if str(record.get("role") or "") != plan.role:
            raise ReviewRunDomainError(f"计划股票 {plan.object_code} 的角色未被冻结证据确认。")
        if str(record.get("role_source") or "") != plan.role_source:
            raise ReviewRunDomainError(
                f"计划股票 {plan.object_code} 的角色来源与冻结证据不一致。"
            )


def _load_capture_payloads(
    evidence_bundle: EvidenceBundle,
    repository: ReviewRunRepository,
) -> dict[str, Mapping[str, Any]]:
    payloads: dict[str, Mapping[str, Any]] = {}
    for capture in evidence_bundle.captures:
        if capture.status != "available":
            continue
        record = repository.get_evidence_capture(f"CAPTURE-{capture.operation_id}")
        if record is None:
            raise ReviewRunDomainError(f"EvidenceCapture 不存在：{capture.slot_id}")
        reference = f"capture:{capture.slot_id}:{capture.normalized_evidence_hash}"
        payloads[reference] = read_capture_payload(
            Path(record["payload"]["output_path"]), capture
        )
    return payloads


# 草案引用和最终门禁共享同一字段能力识别，scope 名称本身不能代替正文。
def capture_supports_step(
    capture: EvidenceCapture,
    payload: Mapping[str, Any],
    step_number: int,
) -> bool:
    capabilities = capture_evidence_capabilities(capture, payload)
    if step_number == 9:
        relevant = {
            capability
            for definition in STEP9_PATTERN_DEFINITIONS
            for capability in (*definition.background_evidence, *definition.trigger_evidence)
        }
    else:
        relevant = {
            capability
            for alternatives in STEP_EVIDENCE_REQUIREMENTS.get(step_number, ())
            for capability in alternatives
        }
    return bool(capabilities & relevant)


# 本地缺口与定稿校验共享最低能力组，缺口文本只描述材料，不推断市场背景。
def step_evidence_gaps(step_number: int, capabilities: set[str]) -> tuple[str, ...]:
    # STEP 9 按模式分别披露缺口，其余步骤按最低能力组披露，替代能力只需满足一种。
    if step_number == 9:
        return tuple(
            f"模式 {definition.pattern_number} 缺少实际可用的"
            + "、".join(EVIDENCE_CAPABILITY_LABELS[item] for item in missing)
            for definition in STEP9_PATTERN_DEFINITIONS
            if (missing := tuple(dict.fromkeys(
                item for item in (*definition.background_evidence, *definition.trigger_evidence)
                if item not in capabilities
            )))
        )
    return tuple(
        "缺少实际可用的" + " 或 ".join(EVIDENCE_CAPABILITY_LABELS[item] for item in alternatives)
        for alternatives in STEP_EVIDENCE_REQUIREMENTS[step_number]
        if not capabilities.intersection(alternatives)
    )


# actionable 的每个 STEP 必须用自己的 refs 满足最低字段组；明确不参与不强求行动证据。
def _validate_actionable_step_evidence(
    revision: RevisionBundle,
    evidence_bundle: EvidenceBundle,
    payload_by_reference: Mapping[str, Mapping[str, Any]],
) -> None:
    if revision.participation_decision != "actionable" and revision.revision_kind != "final":
        return
    capture_by_reference = {
        f"capture:{capture.slot_id}:{capture.normalized_evidence_hash}": capture
        for capture in evidence_bundle.captures
        if capture.status == "available"
    }
    capabilities_by_reference = {
        reference: capture_evidence_capabilities(capture_by_reference[reference], payload)
        for reference, payload in payload_by_reference.items()
        if reference in capture_by_reference
    }
    object_ids_by_reference = {
        reference: _supported_stock_object_ids(capture_by_reference[reference], payload)
        for reference, payload in payload_by_reference.items()
        if reference in capture_by_reference
    }
    issues: list[str] = []
    for step in revision.steps:
        capabilities = {
            capability
            for reference in step.evidence_references
            for capability in capabilities_by_reference.get(reference, set())
        }
        # 非行动修订仍须按冻结证据保留具体缺口；发布门禁另行判断能否形成正式不参与结论。
        if revision.participation_decision != "actionable":
            missing = set(step_evidence_gaps(step.step_number, capabilities)) - set(step.evidence_gaps)
            if missing:
                issues.append(f"STEP {step.step_number} 未保留实际证据缺口：" + "；".join(sorted(missing)))
            if step.step_number == 9:
                issues.extend(
                    _step_nine_pattern_issues(
                        step,
                        capabilities_by_reference,
                        object_ids_by_reference,
                        allow_pending=True,
                    )
                )
            continue
        if step.step_number == 9:
            missing = set(step_evidence_gaps(9, capabilities)) - set(step.evidence_gaps)
            if missing:
                issues.append("STEP 9 未保留实际证据缺口：" + "；".join(sorted(missing)))
            issues.extend(
                _step_nine_pattern_issues(
                    step,
                    capabilities_by_reference,
                    object_ids_by_reference,
                )
            )
            continue
        for alternatives in STEP_EVIDENCE_REQUIREMENTS[step.step_number]:
            if capabilities.intersection(alternatives):
                continue
            labels = " 或 ".join(
                EVIDENCE_CAPABILITY_LABELS[capability] for capability in alternatives
            )
            issues.append(f"STEP {step.step_number} 缺少实际可用的{labels}引用")
    if issues:
        raise ReviewRunDomainError(
            "RevisionBundle 未通过 STEP 最小证据矩阵：" + "；".join(issues)
        )


# 每个模式只消费自己声明的 refs；状态必须与该模式实际具备的背景和触发能力一致。
def _step_nine_pattern_issues(
    step: Any,
    capabilities_by_reference: Mapping[str, set[str]],
    object_ids_by_reference: Mapping[str, set[str]],
    *,
    allow_pending: bool = False,
) -> list[str]:
    patterns = {
        pattern.pattern_number: pattern
        for pattern in step.pattern_assessments
    }
    if set(patterns) != set(range(1, 13)):
        return ["STEP 9 缺少完整的十二模式逐项结构化核验"]
    issues: list[str] = []
    for definition in STEP9_PATTERN_DEFINITIONS:
        pattern = patterns[definition.pattern_number]
        prefix = f"STEP 9 模式 {definition.pattern_number}"
        if set(pattern.evidence_references) - set(step.evidence_references):
            issues.append(f"{prefix} 引用了本 STEP 之外的证据")
        if pattern.pattern_name != definition.name:
            issues.append(f"{prefix} 名称与当前 stock-review.md 定义不一致")
        capabilities = {
            capability
            for reference in pattern.evidence_references
            for capability in capabilities_by_reference.get(reference, set())
        }
        background = set(definition.background_evidence)
        triggers = set(definition.trigger_evidence)
        required = background | triggers
        missing_background = background - capabilities
        missing_triggers = triggers - capabilities
        actual_missing = missing_background | missing_triggers
        declared_missing = set(pattern.missing_evidence)
        invented_missing = declared_missing - required
        undeclared_missing = actual_missing - declared_missing
        stale_missing = (declared_missing & required) - actual_missing
        if invented_missing:
            issues.append(
                f"{prefix} 声明了定义之外的证据类别："
                + "、".join(sorted(invented_missing))
            )
        if undeclared_missing:
            issues.append(
                f"{prefix} 未记录实际证据缺口："
                + "、".join(sorted(undeclared_missing))
            )
        if stale_missing:
            issues.append(
                f"{prefix} 把已经具备的证据错误标记为缺口："
                + "、".join(sorted(stale_missing))
            )
        if pattern.status == "confirmed" and actual_missing:
            issues.append(f"{prefix} 缺少背景或触发证据，不能标记 confirmed")
        elif pattern.status in {
            "base_evidence_available_trigger_pending",
            "background_matched_trigger_pending",
        }:
            if missing_background:
                issues.append(f"{prefix} 背景证据不足，不能标记触发待核验")
            if not missing_triggers:
                issues.append(f"{prefix} 已无触发缺口，不能继续标记触发待核验")
        elif pattern.status == "not_matched" and missing_background:
            issues.append(f"{prefix} 背景证据不足，不能标记 not_matched")
        elif pattern.status == "unavailable" and not missing_background:
            issues.append(f"{prefix} 背景证据已具备，不能标记 unavailable")
        elif pattern.status == "assessment_pending":
            if actual_missing:
                issues.append(f"{prefix} 仍有证据缺口，不能标记 assessment_pending")
            elif not allow_pending:
                issues.append(f"{prefix} 尚未完成逐项判断，不能发布 actionable")
        if pattern.status == "base_evidence_available_trigger_pending" and not allow_pending:
            issues.append(f"{prefix} 只有能力材料，必须通过联合修订确认背景判断后才能发布 actionable")
        if pattern.status not in {"confirmed", "background_matched_trigger_pending"} and pattern.object_ids:
            issues.append(f"{prefix} 尚未确认背景匹配，不能绑定对象")
        if pattern.status in {"confirmed", "background_matched_trigger_pending"} and not pattern.object_ids:
            issues.append(f"{prefix} 背景匹配必须绑定经联合修订验证的对象")
        if pattern.status in {"confirmed", "background_matched_trigger_pending"}:
            supported_object_ids = {
                object_id
                for reference in pattern.evidence_references
                for object_id in object_ids_by_reference.get(reference, set())
            }
            unsupported_object_ids = set(pattern.object_ids) - supported_object_ids
            if unsupported_object_ids:
                issues.append(
                    f"{prefix} 对象没有来自本模式引用的冻结股票或候选证据："
                    + "、".join(sorted(unsupported_object_ids))
                )
    return issues


def capture_evidence_capabilities(
    capture: EvidenceCapture,
    payload: Mapping[str, Any],
) -> set[str]:
    normalized = payload.get("normalized")
    capabilities: set[str] = set()
    market = _mapping_section(normalized, "market")
    indices = market.get("indices")
    index_history = _first_usable(
        market.get("index_history"),
        _named_section(normalized, capture.scope, ("index_history", "index-history")),
    )
    if _has_identified_performance_record(
        indices,
        ("name", "code", "symbol"),
        INDEX_PERFORMANCE_FIELDS,
    ) or _has_comparable_history(index_history, INDEX_PERFORMANCE_FIELDS):
        capabilities.update(("market", "market_indices"))
    if _is_finite_number(market.get("total_amount"), minimum=0, strictly_greater=True):
        capabilities.update(("market", "market_turnover"))
    if _is_nonnegative_count(market.get("up_count")) or _is_nonnegative_count(
        market.get("down_count")
    ):
        capabilities.add("market")

    sentiment = _mapping_section(normalized, "sentiment")
    valid_sentiment_fields = {
        "limit_up_count": _is_nonnegative_count(sentiment.get("limit_up_count")),
        "limit_down_count": _is_nonnegative_count(sentiment.get("limit_down_count")),
        "broken_board_rate": _is_finite_number(
            sentiment.get("broken_board_rate"), minimum=0, maximum=1
        ),
        "highest_board": _is_nonnegative_count(sentiment.get("highest_board")),
    }
    if any(valid_sentiment_fields.values()):
        capabilities.add("sentiment")
    if all(valid_sentiment_fields[field] for field in SENTIMENT_CORE_FIELDS):
        capabilities.add("sentiment_core")

    sectors = _record_section(normalized, capture.scope, "sectors")
    if _has_sector_comparison(sectors):
        capabilities.add("sectors")
    sector_history = _named_section(
        normalized,
        capture.scope,
        ("sector_history", "sector-history"),
    )
    if _has_comparable_history(sector_history, SECTOR_PERFORMANCE_FIELDS) or _records_have_history(
        sectors,
        ("daily_bars", "history", "ranking_history", "change_history"),
        SECTOR_PERFORMANCE_FIELDS,
    ):
        capabilities.add("sector_history")

    stocks = _record_section(normalized, capture.scope, "stocks")
    if _has_identified_performance_record(
        stocks,
        ("code", "name"),
        STOCK_PERFORMANCE_FIELDS,
    ):
        capabilities.add("stocks")
    if any(_stock_has_confirmed_identity_and_role(item) for item in _records(stocks)):
        capabilities.add("stock_identity_role")
    candidate_history = _named_section(
        normalized,
        capture.scope,
        ("candidate_history", "candidate-history", "stock_history", "stock-history"),
    )
    if _has_comparable_history(candidate_history, STOCK_PERFORMANCE_FIELDS) or _records_have_history(
        stocks,
        ("daily_bars", "history"),
        STOCK_PERFORMANCE_FIELDS,
    ):
        capabilities.add("candidate_history")

    style_objects = _identified_performance_objects(
        stocks,
        ("code", "name"),
        STOCK_PERFORMANCE_FIELDS,
    ) | _identified_performance_objects(
        candidate_history,
        ("code", "name"),
        STOCK_PERFORMANCE_FIELDS,
    )
    if len(style_objects) >= 2:
        capabilities.add("stock_style_sample")

    for capability, field_names in PATTERN_TRIGGER_FIELDS.items():
        if capability == "daily_kline":
            available = any(
                _contains_comparable_history_field(normalized, field_name)
                for field_name in field_names
            )
        else:
            available = any(
                _contains_usable_field(normalized, field_name)
                for field_name in field_names
            )
        if available:
            capabilities.add(capability)
    return capabilities


def _mapping_section(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    section = value.get(field_name)
    return section if isinstance(section, Mapping) else {}


def _record_section(value: Any, scope: str, field_name: str) -> Any:
    if isinstance(value, list) and scope.replace("-", "_") == field_name:
        return value
    if not isinstance(value, Mapping):
        return ()
    section = value.get(field_name)
    if isinstance(section, list):
        return section
    if scope.replace("-", "_") == field_name and isinstance(value.get("records"), list):
        return value["records"]
    return ()


def _named_section(value: Any, scope: str, field_names: tuple[str, ...]) -> Any:
    normalized_scope = scope.replace("-", "_")
    normalized_names = {field_name.replace("-", "_") for field_name in field_names}
    if isinstance(value, list) and normalized_scope in normalized_names:
        return value
    if not isinstance(value, Mapping):
        return None
    for field_name in field_names:
        if field_name in value and _has_usable_value(value[field_name]):
            return value[field_name]
    if normalized_scope in normalized_names and _has_usable_value(value.get("records")):
        return value["records"]
    return None


def _first_usable(*values: Any) -> Any:
    return next((value for value in values if _has_usable_value(value)), None)


def _records(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _has_identified_performance_record(
    value: Any,
    identity_fields: tuple[str, ...],
    performance_fields: set[str],
) -> bool:
    return any(
        any(_has_usable_value(record.get(field)) for field in identity_fields)
        and _contains_finite_field(record, performance_fields)
        for record in _records(value)
    )


def _has_sector_comparison(value: Any) -> bool:
    records = _records(value)
    # 显式排行本身已经包含板块间比较；否则至少需要两个有表现值的方向。
    if any(
        any(_has_usable_value(record.get(field)) for field in ("name", "code", "sector_id"))
        and _is_finite_number(record.get("rank"), minimum=1)
        for record in records
    ):
        return True
    return len(
        _identified_performance_objects(
            records,
            ("name", "code", "sector_id"),
            SECTOR_PERFORMANCE_FIELDS - {"rank"},
        )
    ) >= 2


def _identified_performance_objects(
    value: Any,
    identity_fields: tuple[str, ...],
    performance_fields: set[str],
) -> set[str]:
    identities: set[str] = set()
    for record in _records(value):
        identity = next(
            (
                str(record.get(field)).strip()
                for field in identity_fields
                if _has_usable_value(record.get(field))
            ),
            "",
        )
        if identity and _contains_finite_field(record, performance_fields):
            identities.add(identity)
    return identities


def _records_have_history(
    value: Any,
    field_names: tuple[str, ...],
    performance_fields: set[str],
) -> bool:
    return any(
        any(
            _has_comparable_history(record.get(field), performance_fields)
            for field in field_names
        )
        for record in _records(value)
    )


def _has_comparable_history(value: Any, performance_fields: set[str]) -> bool:
    return (
        _has_minimum_continuous_history(value)
        and _contains_finite_field(value, performance_fields)
    )


# 连续比较至少需要两个不同的规范样本日期；这里只证明存在跨日序列，不推导交易日历连续性。
def _has_minimum_continuous_history(value: Any) -> bool:
    return len(_collect_history_dates(value)) >= 2


def _collect_history_dates(value: Any) -> set[date]:
    dates: set[date] = set()
    if isinstance(value, Mapping):
        for field_name, item in value.items():
            if field_name in {"date", "sample_date", "trade_date"}:
                parsed = _parse_history_date(item)
                if parsed is not None:
                    dates.add(parsed)
            elif field_name == "sample_dates" and isinstance(item, (list, tuple)):
                dates.update(
                    parsed
                    for entry in item
                    if (parsed := _parse_history_date(entry)) is not None
                )
            dates.update(_collect_history_dates(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            dates.update(_collect_history_dates(item))
    return dates


def _parse_history_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _stock_has_confirmed_identity_and_role(record: Mapping[str, Any]) -> bool:
    values = (
        record.get("code"),
        record.get("name"),
        record.get("exchange"),
        record.get("sector_id") or record.get("sector") or record.get("sector_name"),
        record.get("role"),
        record.get("role_source"),
    )
    return all(_has_usable_value(value) and str(value).strip() != "待确认" for value in values)


def _supported_stock_object_ids(
    capture: EvidenceCapture,
    payload: Mapping[str, Any],
) -> set[str]:
    normalized = payload.get("normalized")
    records = [
        *_records(_record_section(normalized, capture.scope, "stocks")),
        *_records(
            _named_section(
                normalized,
                capture.scope,
                ("candidate_history", "candidate-history", "stock_history", "stock-history"),
            )
        ),
    ]
    supported: set[str] = set()
    for record in records:
        explicit_object_id = record.get("object_id")
        if _has_usable_value(explicit_object_id):
            supported.add(str(explicit_object_id).strip())
        code = str(record.get("code") or "").strip()
        if re.fullmatch(r"\d{6}", code):
            supported.update((code, f"stock-{code}", f"stock:{code}"))
    return supported


def _contains_finite_field(value: Any, field_names: set[str]) -> bool:
    if isinstance(value, Mapping):
        return any(
            (key in field_names and _is_finite_number(item))
            or _contains_finite_field(item, field_names)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_finite_field(item, field_names) for item in value)
    return False


def _contains_usable_field(value: Any, field_name: str) -> bool:
    if isinstance(value, Mapping):
        return any(
            (key == field_name and _has_usable_value(item))
            or _contains_usable_field(item, field_name)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_usable_field(item, field_name) for item in value)
    return False


def _contains_comparable_history_field(value: Any, field_name: str) -> bool:
    if isinstance(value, Mapping):
        return any(
            (
                key == field_name
                and _has_comparable_history(
                    item,
                    INDEX_PERFORMANCE_FIELDS
                    | SECTOR_PERFORMANCE_FIELDS
                    | STOCK_PERFORMANCE_FIELDS,
                )
            )
            or _contains_comparable_history_field(item, field_name)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(
            _contains_comparable_history_field(item, field_name)
            for item in value
        )
    return False


def _has_usable_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        normalized = value.strip()
        return bool(normalized) and normalized.lower() not in PLACEHOLDER_TEXTS
    if isinstance(value, (int, float, Decimal)):
        return _is_finite_number(value)
    if isinstance(value, Mapping):
        return any(_has_usable_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_usable_value(item) for item in value)
    return False


def _is_nonnegative_count(value: Any) -> bool:
    return (
        _is_finite_number(value, minimum=0)
        and float(value).is_integer()
    )


def _is_finite_number(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strictly_greater: bool = False,
) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(number):
        return False
    if minimum is not None and (
        number <= minimum if strictly_greater else number < minimum
    ):
        return False
    if maximum is not None and number > maximum:
        return False
    return True


def _index_stock_records(value: Any) -> dict[str, Mapping[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, list):
        raise ReviewRunDomainError("冻结证据的 stocks 必须是数组。")
    result: dict[str, Mapping[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ReviewRunDomainError("冻结证据 stocks 中每一项必须是 JSON 对象。")
        code = str(item.get("code") or "")
        if not code:
            continue
        if code in result and result[code] != item:
            raise ReviewRunDomainError(f"冻结证据中股票代码 {code} 对应了不同身份。")
        result[code] = item
    return result


def _stock_sectors(record: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for field_name in ("sector_id", "sector", "sector_name"):
        value = record.get(field_name)
        if value not in (None, ""):
            values.add(str(value))
    sectors = record.get("sectors")
    if isinstance(sectors, list):
        values.update(str(value) for value in sectors if value not in (None, ""))
    return values
