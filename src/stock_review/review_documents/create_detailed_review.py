# 本文件负责按 stock-review.md 的 STEP 1-10 编排详细复盘，模型只生成受本地证据约束的解释性草案。

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable

from stock_review.evidence.summarize_review_facts import (
    HARD_GAP_LABELS,
    StepEvidence,
    build_step_evidence,
    summarize_review_facts,
)
from stock_review.llm.openai_compatible_client import OpenAICompatibleSettings, request_json_completion
from stock_review.review_documents.build_review_context import ReviewContext, build_review_context
from stock_review.review_documents.create_final_draft import build_evidence_facts
from stock_review.review_documents.manage_manual_review import ManualFinalResponse, list_manual_final_responses
from stock_review.review_framework.parse_framework import ReviewFramework, ReviewStep, parse_framework_file
from stock_review.scoring.verify_step9_patterns import Step9PatternCheck, verify_step9_patterns


DEFAULT_DETAILED_REPORT_DIR = Path("reports") / "daily"
DEFAULT_LOG_PATH = Path("logs") / "stock_review.log"
REQUIRED_STEP_NUMBERS = tuple(range(1, 11))
MODEL_STEP_NUMBERS = tuple(range(1, 8))
MAX_PROSE_LENGTH = 800
MAX_FOCUS_CANDIDATES = 5
# 中文紧邻数字时也必须识别；\w 会把中文视为单词字符，曾漏过“价格75元”一类模型新造数字。
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?%?")
STOCK_CODE_PATTERN = re.compile(r"(?<!\d)\d{6}(?!\d)")
PROSE_SENTENCE_PATTERN = re.compile(r"[^。！？!?]+[。！？!?]?")
NUMERIC_PROSE_FALLBACK = "具体数值与对象代码以本步证据链为准。"
STEP_COUNTER_EVIDENCE = {
    1: "即使连续指数历史已满足，缺少涨跌家数和按需外围事实时仍不能确认市场阶段。",
    2: "缺少大市值趋势、容量中军、完整连板梯队和活跃资金事实时，不能确认市场风格。",
    3: "缺少晋级率、前日反馈和竞价事实，且情绪来源不连续时，不能确认情绪周期。",
    4: "重复排行只说明板块反复出现；缺少可比量能和内部表现时，不能认定核心板块。",
    5: "缺少连续 K 线、量能、启动或回调节点和内部扩散事实时，不能判断主升阶段。",
    6: "角色来源和区间表现只能形成候选；缺少身份、逆势表现和连续比较时，不能认定核心票。",
    7: "真实池和系统发现候选都不等于次日可参与对象；仍需结合预演、触发条件和放弃条件确认。",
}

STEP_MODEL_GUIDANCE = {
    2: "最高连板只代表高度仍在，不能单独证明连板生态健康；必须同时观察跌停、炸板、多日反馈以及有限候选的趋势和成交容量，趋势与连板可以同时走弱。",
    4: "重复上榜不等于核心板块；必须区分此前主流转弱、防御或超跌轮动和真正持续走强的核心方向。",
    6: "必须分别检查系统发现候选与用户真实池；真实池不是完整市场候选范围，缺少身份和相对强度时只能保留候选。",
    7: "必须从系统发现候选和用户真实池的合并范围中形成少量条件关注对象，不能把真实池清单直接当作重点关注结论。",
}

UNAVAILABLE_EVIDENCE_MARKERS = ("不存在", "为空", "没有可确认代码", "没有来源可确认")

PREVIEW_OUTPUT_FIELDS = (
    ("market_preview", "大盘", (1, 2, 3)),
    ("sector_preview", "板块", (4, 5)),
    ("stock_preview", "个股", (6, 7)),
)

STEP9_EVIDENCE_LABELS = {
    "market": "市场事实",
    "sentiment": "情绪事实",
    "sectors": "板块事实",
    "stocks": "个股事实",
    "candidate_history": "候选趋势、成交容量与流通市值",
    "daily_kline": "日线形态",
    "call_auction": "竞价事实",
    "intraday": "分时事实",
    "previous_day_feedback": "前日反馈",
    "next_day_feedback": "次日反馈",
    "regulatory_event": "监管事件",
}

PARTICIPATION_DECISION_LABELS = {
    "yes": "可以参与",
    "conditional": "条件满足后参与",
    "no": "不参与",
}

CANDIDATE_ACTION_LABELS = {
    "buy": "条件买入",
    "watch": "观察",
    "avoid": "回避",
}

UNVERIFIED_IDENTITY_REPLACEMENTS = {
    "具备连板龙头特征": "具备连板候选特征",
    "作为连板龙头": "作为连板候选",
    "具备容量中军特征": "具备容量候选特征",
    "作为算力中军": "作为算力容量候选",
    "走势独立": "相对表现仍待确认",
}


class DetailedReviewError(ValueError):
    pass


@dataclass(frozen=True)
class DetailedStepAnalysis:
    step_number: int
    conclusion: str
    interpretation: str
    evidence_field_keys: tuple[str, ...]
    next_day_validation: str


@dataclass(frozen=True)
class DetailedCandidate:
    code: str
    name: str
    sector: str
    role: str
    origin: str
    close: float | None
    return_5d_percent: float | None
    return_20d_percent: float | None
    average_5d_amount: float | None
    circulating_market_value: float | None


@dataclass(frozen=True)
class DetailedPreviewOutlook:
    primary_judgment: str
    odds_direction: str
    evidence_field_keys: tuple[str, ...]


@dataclass(frozen=True)
class DetailedPreviewDraft:
    target: str
    expectation: str
    over_expectation: str
    under_expectation: str
    abandon_condition: str
    evidence_field_keys: tuple[str, ...]


@dataclass(frozen=True)
class DetailedCandidateAction:
    code: str
    action: str
    rationale: str
    trigger: str
    position_guidance: str
    abandon_condition: str


@dataclass(frozen=True)
class DetailedFinalPlanDraft:
    participation_decision: str
    position_guidance: str
    summary: str
    participation_condition: str
    risk_boundary: str
    focus_codes: tuple[str, ...]
    candidate_actions: tuple[DetailedCandidateAction, ...]
    evidence_field_keys: tuple[str, ...]


@dataclass(frozen=True)
class DetailedModelOutput:
    analyses: tuple[DetailedStepAnalysis, ...]
    preview_outlook: DetailedPreviewOutlook
    preview_drafts: tuple[DetailedPreviewDraft, ...]
    final_plan_draft: DetailedFinalPlanDraft


@dataclass(frozen=True)
class DetailedReview:
    review_date: str
    framework: ReviewFramework
    context: ReviewContext
    analyses: tuple[DetailedStepAnalysis, ...]
    candidates: tuple[DetailedCandidate, ...]
    preview_outlook: DetailedPreviewOutlook
    preview_drafts: tuple[DetailedPreviewDraft, ...]
    final_plan_draft: DetailedFinalPlanDraft
    evidence_facts: dict[str, str]
    step_evidence: dict[int, StepEvidence]
    step9_checks: tuple[Step9PatternCheck, ...]
    final_responses: tuple[ManualFinalResponse, ...]
    snapshot_reference: str


# 详细复盘构建只读取本地输入并调用模型生成受约束解释，不写报告、数据库或业务对象。
def build_detailed_review(
    review_date: str,
    framework_path: Path,
    snapshot_dir: Path,
    database_path: Path,
    request_completion: Callable[[OpenAICompatibleSettings, str, str], dict[str, Any]] = request_json_completion,
    settings: OpenAICompatibleSettings | None = None,
) -> DetailedReview:
    framework = parse_framework_file(framework_path)
    validate_framework(framework)
    context = build_review_context(review_date, snapshot_dir=snapshot_dir, database_path=database_path)
    facts_summary = summarize_review_facts(review_date, snapshot_dir, snapshot_dir)
    step_evidence = build_step_evidence(facts_summary)
    evidence_facts = build_evidence_facts(context, facts_summary)
    local_evidence_fields = build_local_evidence_fields(step_evidence, evidence_facts)
    candidates = build_detailed_candidates(context, facts_summary.candidate_history_records)
    final_responses = tuple(list_manual_final_responses(review_date, database_path=database_path))
    resolved_settings = settings or OpenAICompatibleSettings.from_environment()
    response = request_completion(
        resolved_settings,
        build_system_prompt(),
        build_user_prompt(
            framework,
            context,
            evidence_facts,
            step_evidence,
            local_evidence_fields,
            candidates,
        ),
    )
    model_output = validate_model_response(
        response,
        local_evidence_fields,
        candidates,
    )
    return DetailedReview(
        review_date=review_date,
        framework=framework,
        context=context,
        analyses=model_output.analyses,
        candidates=candidates,
        preview_outlook=model_output.preview_outlook,
        preview_drafts=model_output.preview_drafts,
        final_plan_draft=model_output.final_plan_draft,
        evidence_facts=evidence_facts,
        step_evidence=step_evidence,
        step9_checks=verify_step9_patterns(
            context.snapshot, facts_summary.candidate_history_records
        ),
        final_responses=final_responses,
        snapshot_reference=str(snapshot_dir / f"{review_date}_snapshot.json"),
    )


# 正式入口只把已校验草案写入 Markdown 和本地日志，不修改 SQLite、池子、计划或 Observation。
def create_detailed_review_report(
    review_date: str,
    framework_path: Path,
    snapshot_dir: Path,
    database_path: Path,
    output_dir: Path = DEFAULT_DETAILED_REPORT_DIR,
    log_path: Path = DEFAULT_LOG_PATH,
    request_completion: Callable[[OpenAICompatibleSettings, str, str], dict[str, Any]] = request_json_completion,
    settings: OpenAICompatibleSettings | None = None,
) -> Path:
    review = build_detailed_review(
        review_date,
        framework_path,
        snapshot_dir,
        database_path,
        request_completion=request_completion,
        settings=settings,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{review_date}_detailed_review.md"
    output_path.write_text(render_detailed_review(review), encoding="utf-8")
    write_detailed_review_log(review, framework_path, output_path, log_path)
    return output_path


def validate_framework(framework: ReviewFramework) -> None:
    numbers = tuple(step.number for step in framework.steps)
    if numbers != REQUIRED_STEP_NUMBERS:
        raise DetailedReviewError("详细复盘要求框架按顺序完整包含 STEP 1 至 STEP 10。")


# 候选范围只合并当前快照的可确认个股事实和有效真实池，不扫描全市场、不自动认定核心票。
def build_detailed_candidates(
    context: ReviewContext,
    candidate_history_records: tuple[dict[str, Any], ...],
) -> tuple[DetailedCandidate, ...]:
    candidates: dict[str, DetailedCandidate] = {}
    history_by_code = {
        str(record.get("code")): record
        for record in candidate_history_records
        if record.get("code") not in (None, "")
    }
    for stock in context.snapshot.stocks:
        code = str(stock.get("code") or "").strip()
        name = str(stock.get("name") or "").strip()
        if not code.isdigit() or len(code) != 6 or not name:
            continue
        history = history_by_code.get(code, {})
        candidates[code] = DetailedCandidate(
            code=code,
            name=name,
            sector=str(stock.get("sector") or "待确认"),
            role=str(stock.get("role") or "市场事实候选"),
            origin="系统发现候选",
            close=history.get("close"),
            return_5d_percent=history.get("return_5d_percent"),
            return_20d_percent=history.get("return_20d_percent"),
            average_5d_amount=history.get("average_5d_amount"),
            circulating_market_value=history.get("circulating_market_value"),
        )
    for item in context.active_real_pool_items:
        existing = candidates.get(item.code)
        history = history_by_code.get(item.code, {})
        candidates[item.code] = DetailedCandidate(
            code=item.code,
            name=item.name,
            sector="、".join(item.sectors) or (existing.sector if existing else "待确认"),
            role=existing.role if existing else "用户真实池对象",
            origin="系统发现候选 + 用户真实池" if existing else "用户真实池",
            close=history.get("close") if history else (existing.close if existing else None),
            return_5d_percent=history.get("return_5d_percent") if history else (existing.return_5d_percent if existing else None),
            return_20d_percent=history.get("return_20d_percent") if history else (existing.return_20d_percent if existing else None),
            average_5d_amount=history.get("average_5d_amount") if history else (existing.average_5d_amount if existing else None),
            circulating_market_value=history.get("circulating_market_value") if history else (existing.circulating_market_value if existing else None),
        )
    return tuple(candidates.values())


# 本地白名单是证据路由的唯一权威；缺失字段不传给模型，也不进入报告证据链。
def build_local_evidence_fields(
    step_evidence: dict[int, StepEvidence],
    evidence_facts: dict[str, str],
) -> dict[int, tuple[str, ...]]:
    return {
        step_number: tuple(
            field_name
            for field_name in evidence.allowed_field_keys
            if is_local_evidence_available(evidence_facts[field_name])
        )
        for step_number, evidence in step_evidence.items()
    }


def is_local_evidence_available(value: str) -> bool:
    if not value.strip() or any(marker in value for marker in UNAVAILABLE_EVIDENCE_MARKERS):
        return False
    primary_clause = value.split("；", maxsplit=1)[0].strip()
    return not primary_clause.endswith(("：待确认", "：无"))


def merge_local_evidence_fields(
    local_evidence_fields: dict[int, tuple[str, ...]],
    step_numbers: tuple[int, ...],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            field_name
            for step_number in step_numbers
            for field_name in local_evidence_fields[step_number]
        )
    )


def build_system_prompt() -> str:
    return (
        "你是 A 股短线盘后复盘的证据解释助手。只能返回用户要求的 JSON 对象。"
        "必须严格遵循每个 STEP 的 framework_rules，并且只能使用该 STEP 的 local_evidence_fields。"
        "结论必须写成基于证据的推断，不得把缺失证据补成事实，不得跨 STEP 搬运字段。"
        "STEP 1 至 STEP 7 不得创造历史数字、消息、股票代码、股票名称、板块归属或行情走势；具体历史数值统一由本地证据链展示。"
        "各 STEP 可以结合框架输出买入、卖出、低吸、打板和仓位等条件应对，但必须写清触发与失效边界，不得声称已经成交。"
        "缺少身份验证时只能称为候选，不得把候选直接认定为龙头、核心票或中军。"
        "STEP 8 和 STEP 10 是明确标记的 AI 条件计划，可以提出数字阈值、买卖方向和仓位方案，但不得把计划参数写成已发生事实。"
        "计划中的股票代码只能来自 candidate_universe，逐只计划必须同时包含触发、仓位和放弃条件。"
        "必须完整输出 STEP 1 至 STEP 7，并生成 STEP 8 的主判断、三类四情景草案和 STEP 10 的条件化行动计划。"
        "证据字段由本地系统固定，不得在输出中选择、修改或返回 evidence_field_keys。"
        "所有计划都必须写成尚未执行的条件方案，不得声称已经成交或自动执行。"
    )


# 提示词提供框架规则、同 STEP 事实和人工判断，模型没有修改硬缺口或业务对象的权限。
def build_user_prompt(
    framework: ReviewFramework,
    context: ReviewContext,
    evidence_facts: dict[str, str],
    step_evidence: dict[int, StepEvidence],
    local_evidence_fields: dict[int, tuple[str, ...]],
    candidates: tuple[DetailedCandidate, ...],
) -> str:
    records = {record.step_number: record for record in context.step_records}
    steps = []
    for step in framework.steps[:7]:
        evidence = step_evidence[step.number]
        record = records.get(step.number)
        steps.append(
            {
                "step_number": step.number,
                "title": step.title,
                "framework_rules": step.body,
                "local_evidence_fields": {
                    field_name: evidence_facts[field_name]
                    for field_name in local_evidence_fields[step.number]
                },
                "hard_missing_fields": list(evidence.hard_missing_fields),
                "analysis_guidance": STEP_MODEL_GUIDANCE.get(step.number, "按框架规则和本步证据归纳，不补写缺失事实。"),
                "manual_record": (
                    {
                        "judgment": record.judgment,
                        "hypothesis": record.hypothesis,
                        "note": record.note,
                    }
                    if record
                    else None
                ),
            }
        )
    payload = {
        "review_date": context.review_date,
        "steps": steps,
        "active_real_pool_items": [
            {"code": item.code, "name": item.name, "sectors": list(item.sectors)}
            for item in context.active_real_pool_items
        ],
        "candidate_universe": [candidate.__dict__ for candidate in candidates],
        "required_output": {
            "step_analyses": [
                {
                    "step_number": "1 至 7 的单个编号",
                    "conclusion": "本步基于证据的简明推断，不含交易指令",
                    "interpretation": "结合框架规则解释证据与限制",
                    "next_day_validation": "下一交易日需要观察什么来确认或推翻当前推断",
                }
            ],
            "preview_outlook": {
                "primary_judgment": "次日最可能的主情景；连续弱情绪时可写修复博弈价值相对上升，但不得写成修复已经发生",
                "odds_direction": "rising、neutral、falling 或 unavailable",
            },
            "market_preview": {
                "expectation": "大盘符合预期时需要观察到的条件",
                "over_expectation": "大盘超预期时需要观察到的条件",
                "under_expectation": "大盘不及预期时需要观察到的条件",
                "abandon_condition": "大盘预演失效或应停止关注的条件",
            },
            "sector_preview": {
                "expectation": "板块符合预期时需要观察到的条件",
                "over_expectation": "板块超预期时需要观察到的条件",
                "under_expectation": "板块不及预期时需要观察到的条件",
                "abandon_condition": "板块预演失效或应停止关注的条件",
            },
            "stock_preview": {
                "expectation": "个股符合预期时需要观察到的条件",
                "over_expectation": "个股超预期时需要观察到的条件",
                "under_expectation": "个股不及预期时需要观察到的条件",
                "abandon_condition": "个股预演失效或应停止关注的条件",
            },
            "final_plan_draft": {
                "participation_decision": "yes、conditional 或 no，直接回答次日能否参与",
                "position_guidance": "建议总仓位；可使用百分比或成数，必须与风险边界一致",
                "summary": "次日条件化行动总纲，区分事实、推断和计划",
                "participation_condition": "哪些盘面条件满足后执行计划",
                "risk_boundary": "哪些事实出现后停止或降低计划",
                "focus_codes": ["candidate_universe 中最多五个代码；有候选时不得为空"],
                "candidate_actions": [
                    {
                        "code": "必须来自 focus_codes",
                        "action": "buy、watch 或 avoid",
                        "rationale": "只基于候选本地事实说明理由；身份未确认时只能写候选",
                        "trigger": "次日满足什么条件后执行；允许数字阈值",
                        "position_guidance": "该对象建议仓位；允许百分比或成数",
                        "abandon_condition": "什么情况下取消该对象计划",
                    }
                ],
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False)


# 模型输出必须保持固定结构，所有字段、数字和代码均需能在同 STEP 本地输入中找到来源。
def validate_model_response(
    response: dict[str, Any],
    local_evidence_fields: dict[int, tuple[str, ...]],
    candidates: tuple[DetailedCandidate, ...],
) -> DetailedModelOutput:
    required_root_fields = {
        "step_analyses",
        "preview_outlook",
        "market_preview",
        "sector_preview",
        "stock_preview",
        "final_plan_draft",
    }
    if not required_root_fields.issubset(response):
        raise DetailedReviewError("详细复盘模型输出缺少 STEP 分析、预演草案或最终计划草案。")
    raw_analyses = response.get("step_analyses")
    if not isinstance(raw_analyses, list) or not all(isinstance(item, dict) for item in raw_analyses):
        raise DetailedReviewError("详细复盘模型输出 step_analyses 必须是对象列表。")

    analyses: list[DetailedStepAnalysis] = []
    numbers: set[int] = set()
    required_fields = {"step_number", "conclusion", "interpretation", "next_day_validation"}
    for raw_analysis in raw_analyses:
        if not required_fields.issubset(raw_analysis):
            raise DetailedReviewError("每个 STEP 分析缺少规定字段。")
        number = normalize_model_step_number(raw_analysis.get("step_number"))
        if number in numbers:
            raise DetailedReviewError("详细复盘模型输出不能重复 STEP。")
        numbers.add(number)
        selected_fields = local_evidence_fields[number]
        conclusion = validate_analysis_prose(raw_analysis.get("conclusion"), "本步结论")
        interpretation = validate_analysis_prose(raw_analysis.get("interpretation"), "证据解释")
        next_day_validation = validate_analysis_prose(raw_analysis.get("next_day_validation"), "次日验证点")
        if number in {6, 7}:
            conclusion = normalize_unverified_identity_claims(conclusion)
            interpretation = normalize_unverified_identity_claims(interpretation)
            next_day_validation = normalize_unverified_identity_claims(next_day_validation)
        analyses.append(
            DetailedStepAnalysis(number, conclusion, interpretation, selected_fields, next_day_validation)
        )
    if numbers != set(MODEL_STEP_NUMBERS):
        raise DetailedReviewError("详细复盘模型输出必须完整覆盖 STEP 1 至 STEP 7。")
    preview_outlook = validate_preview_outlook(
        response.get("preview_outlook"),
        local_evidence_fields,
    )
    preview_drafts = validate_preview_drafts(
        response,
        local_evidence_fields,
        {candidate.code for candidate in candidates},
    )
    final_plan_draft = validate_final_plan_draft(
        response.get("final_plan_draft"),
        local_evidence_fields,
        candidates,
    )
    return DetailedModelOutput(
        analyses=tuple(sorted(analyses, key=lambda item: item.step_number)),
        preview_outlook=preview_outlook,
        preview_drafts=preview_drafts,
        final_plan_draft=final_plan_draft,
    )


def validate_preview_outlook(
    value: Any,
    local_evidence_fields: dict[int, tuple[str, ...]],
) -> DetailedPreviewOutlook:
    required_fields = {"primary_judgment", "odds_direction"}
    if not isinstance(value, dict) or not required_fields.issubset(value):
        raise DetailedReviewError("详细复盘模型 STEP 8 主判断缺少规定字段。")
    odds_direction = value.get("odds_direction")
    if odds_direction not in {"rising", "neutral", "falling", "unavailable"}:
        raise DetailedReviewError("STEP 8 主判断的值博率方向无效。")
    selected_fields = merge_local_evidence_fields(local_evidence_fields, MODEL_STEP_NUMBERS)
    primary_judgment = validate_analysis_prose(value.get("primary_judgment"), "STEP 8 主判断")
    return DetailedPreviewOutlook(primary_judgment, odds_direction, selected_fields)


# STEP 8 的三类名称和证据路由由本地固定，模型只填写各类四种情景的内容。
def validate_preview_drafts(
    response: dict[str, Any],
    local_evidence_fields: dict[int, tuple[str, ...]],
    allowed_codes: set[str],
) -> tuple[DetailedPreviewDraft, ...]:
    required_fields = {
        "expectation",
        "over_expectation",
        "under_expectation",
        "abandon_condition",
    }
    drafts: list[DetailedPreviewDraft] = []
    for output_field, target, step_numbers in PREVIEW_OUTPUT_FIELDS:
        item = response.get(output_field)
        if not isinstance(item, dict) or not required_fields.issubset(item):
            raise DetailedReviewError(f"详细复盘模型 STEP 8 {target}预演草案缺少规定字段。")
        selected_fields = merge_local_evidence_fields(
            local_evidence_fields,
            step_numbers,
        )
        prose_values = (
            validate_plan_prose(
                item.get("expectation"), f"STEP 8 {target}符合预期", allowed_codes
            ),
            validate_plan_prose(
                item.get("over_expectation"), f"STEP 8 {target}超预期", allowed_codes
            ),
            validate_plan_prose(
                item.get("under_expectation"), f"STEP 8 {target}不及预期", allowed_codes
            ),
            validate_plan_prose(
                item.get("abandon_condition"), f"STEP 8 {target}放弃条件", allowed_codes
            ),
        )
        drafts.append(DetailedPreviewDraft(target, *prose_values, selected_fields))
    return tuple(drafts)


# STEP 10 只允许从有限候选中生成少量条件行动，并要求总仓位与逐候选风险边界完整。
def validate_final_plan_draft(
    value: Any,
    local_evidence_fields: dict[int, tuple[str, ...]],
    candidates: tuple[DetailedCandidate, ...],
) -> DetailedFinalPlanDraft:
    required_fields = {
        "participation_decision",
        "position_guidance",
        "summary",
        "participation_condition",
        "risk_boundary",
        "focus_codes",
        "candidate_actions",
    }
    if not isinstance(value, dict) or not required_fields.issubset(value):
        raise DetailedReviewError("详细复盘模型 STEP 10 最终计划草案缺少规定字段。")
    candidate_codes = {candidate.code for candidate in candidates}
    focus_codes = value.get("focus_codes")
    if not isinstance(focus_codes, list) or not all(isinstance(code, str) for code in focus_codes):
        raise DetailedReviewError("STEP 10 关注代码必须是列表。")
    normalized_codes = tuple(dict.fromkeys(focus_codes))
    if len(normalized_codes) != len(focus_codes) or len(normalized_codes) > MAX_FOCUS_CANDIDATES:
        raise DetailedReviewError("STEP 10 只能选择不重复的少量关注代码。")
    if any(code not in candidate_codes for code in normalized_codes):
        raise DetailedReviewError("STEP 10 包含有限候选范围之外的股票代码。")
    if candidate_codes and not normalized_codes:
        raise DetailedReviewError("存在有限候选时，STEP 10 必须选择至少一个行动对象。")
    participation_decision = value.get("participation_decision")
    if participation_decision not in PARTICIPATION_DECISION_LABELS:
        raise DetailedReviewError("STEP 10 次日参与结论无效。")
    candidate_actions = validate_candidate_actions(
        value.get("candidate_actions"),
        normalized_codes,
        candidates,
    )
    selected_fields = merge_local_evidence_fields(local_evidence_fields, MODEL_STEP_NUMBERS)
    position_guidance = validate_plan_prose(
        value.get("position_guidance"), "STEP 10总仓位", candidate_codes
    )
    summary = validate_plan_prose(value.get("summary"), "STEP 10总纲", candidate_codes)
    participation_condition = validate_plan_prose(
        value.get("participation_condition"), "STEP 10参与条件", candidate_codes
    )
    risk_boundary = validate_plan_prose(
        value.get("risk_boundary"), "STEP 10风险边界", candidate_codes
    )
    return DetailedFinalPlanDraft(
        participation_decision,
        position_guidance,
        summary,
        participation_condition,
        risk_boundary,
        normalized_codes,
        candidate_actions,
        selected_fields,
    )


def validate_candidate_actions(
    value: Any,
    focus_codes: tuple[str, ...],
    candidates: tuple[DetailedCandidate, ...],
) -> tuple[DetailedCandidateAction, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise DetailedReviewError("STEP 10 逐候选计划必须是对象列表。")
    required_fields = {
        "code",
        "action",
        "rationale",
        "trigger",
        "position_guidance",
        "abandon_condition",
    }
    candidate_codes = {candidate.code for candidate in candidates}
    actions: list[DetailedCandidateAction] = []
    seen_codes: set[str] = set()
    for item in value:
        if not required_fields.issubset(item):
            raise DetailedReviewError("STEP 10 逐候选计划缺少规定字段。")
        code = item.get("code")
        action = item.get("action")
        if not isinstance(code, str) or code not in candidate_codes or code not in focus_codes:
            raise DetailedReviewError("STEP 10 逐候选计划包含有限关注范围之外的股票代码。")
        if code in seen_codes:
            raise DetailedReviewError("STEP 10 逐候选计划不能重复股票代码。")
        if action not in CANDIDATE_ACTION_LABELS:
            raise DetailedReviewError("STEP 10 逐候选行动类型无效。")
        seen_codes.add(code)
        actions.append(
            DetailedCandidateAction(
                code=code,
                action=action,
                rationale=normalize_unverified_identity_claims(
                    validate_plan_prose(
                        item.get("rationale"), "STEP 10候选理由", candidate_codes
                    )
                ),
                trigger=validate_plan_prose(
                    item.get("trigger"), "STEP 10候选触发条件", candidate_codes
                ),
                position_guidance=validate_plan_prose(
                    item.get("position_guidance"), "STEP 10候选仓位", candidate_codes
                ),
                abandon_condition=validate_plan_prose(
                    item.get("abandon_condition"), "STEP 10候选放弃条件", candidate_codes
                ),
            )
        )
    if seen_codes != set(focus_codes):
        raise DetailedReviewError("STEP 10 每个关注代码都必须有且只有一条逐候选计划。")
    return tuple(actions)


def normalize_model_step_number(value: Any) -> int:
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise DetailedReviewError("详细复盘模型 STEP 编号必须是 1 至 7。")
    if number not in MODEL_STEP_NUMBERS:
        raise DetailedReviewError("详细复盘模型 STEP 编号必须是 1 至 7。")
    return number


def validate_prose_shape(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DetailedReviewError(f"详细复盘{label}不能为空。")
    prose = value.strip()
    if len(prose) > MAX_PROSE_LENGTH:
        raise DetailedReviewError(f"详细复盘{label}超过允许长度。")
    if "#" in prose:
        raise DetailedReviewError(f"详细复盘{label}不得创建 Markdown 标题。")
    return prose


def validate_analysis_prose(value: Any, label: str) -> str:
    prose = validate_prose_shape(value, label)
    return remove_numeric_prose_sentences(prose)


# STEP 8/10 是明确标记的条件计划，允许数字、买卖方向和仓位表达，但仍禁止 Markdown 注入。
def validate_plan_prose(
    value: Any,
    label: str,
    allowed_codes: set[str],
) -> str:
    prose = validate_prose_shape(value, label)
    unknown_codes = set(STOCK_CODE_PATTERN.findall(prose)) - allowed_codes
    if unknown_codes:
        raise DetailedReviewError(
            f"详细复盘{label}包含有限候选范围之外的股票代码："
            f"{'、'.join(sorted(unknown_codes))}。"
        )
    return prose


# 模型只负责定性解释；含阿拉伯数字或代码的整句统一丢弃，避免清理后留下不完整残句。
def remove_numeric_prose_sentences(prose: str) -> str:
    retained_sentences = [
        sentence
        for sentence in PROSE_SENTENCE_PATTERN.findall(prose)
        if not NUMBER_PATTERN.search(sentence)
    ]
    sanitized = "".join(retained_sentences).strip()
    return sanitized or NUMERIC_PROSE_FALLBACK


def normalize_unverified_identity_claims(prose: str) -> str:
    normalized = prose
    for unsupported_claim, candidate_claim in UNVERIFIED_IDENTITY_REPLACEMENTS.items():
        normalized = normalized.replace(unsupported_claim, candidate_claim)
    return normalized


# 渲染严格遍历框架 STEP；模型草案经过本地字段、对象和文字边界校验后才能进入待确认区域。
def render_detailed_review(review: DetailedReview) -> str:
    analyses = {item.step_number: item for item in review.analyses}
    steps = {step.number: step for step in review.framework.steps}
    records = {record.step_number: record for record in review.context.step_records}
    lines = [
        f"# {review.review_date} A 股详细复盘",
        "",
        f"- 复盘框架：{review.framework.source_path}",
        f"- Evidence Snapshot：{review.snapshot_reference}",
        f"- 样本日期：{review.context.snapshot.sample_date}",
        "- 内容边界：历史事实由本地证据固定渲染；模型判断与仓位方案是 AI 条件计划，不保证市场结果，也不会自动交易或写入业务数据。",
    ]
    for number in MODEL_STEP_NUMBERS:
        lines.extend(render_analysis_step(steps[number], analyses[number], review, records.get(number)))
    lines.extend(render_preview_step(steps[8], review))
    lines.extend(render_pattern_step(steps[9], review))
    lines.extend(render_final_step(steps[10], review))
    return "\n".join(lines).rstrip() + "\n"


def render_analysis_step(
    step: ReviewStep,
    analysis: DetailedStepAnalysis,
    review: DetailedReview,
    manual_record: Any,
) -> list[str]:
    evidence = review.step_evidence[step.number]
    lines = [
        "",
        f"## STEP {step.number}: {step.title}",
        "",
        "### 本步结论",
        "",
        f"- 状态：{evidence.status}。",
        f"- 待核查推断：{analysis.conclusion}",
    ]
    if step.number in {6, 7}:
        lines.extend(render_candidate_scope(step.number, review))
    lines.extend(["", "### 证据链", ""])
    for field_name in analysis.evidence_field_keys:
        lines.append(f"- `{field_name}`：{review.evidence_facts[field_name]}")
    lines.append(f"- 证据引用：{review.snapshot_reference}")
    if manual_record:
        lines.append(f"- 用户判断：{manual_record.judgment}")
        if manual_record.hypothesis:
            lines.append(f"- 待验证假设：{manual_record.hypothesis}")
        if manual_record.note:
            lines.append(f"- 人工备注：{manual_record.note}")
    lines.extend(
        [
            "",
            "### 证据解释",
            "",
            f"- {analysis.interpretation}",
            "",
            "### 反向证据与硬缺口",
            "",
            f"- 反向证据：{STEP_COUNTER_EVIDENCE[step.number]}",
            f"- 硬缺口：{render_hard_gap_labels(evidence)}",
            "",
            "### 次日验证点",
            "",
            f"- {analysis.next_day_validation}",
        ]
    )
    return lines


def render_candidate_scope(step_number: int, review: DetailedReview) -> list[str]:
    market_candidates = [candidate for candidate in review.candidates if "系统发现候选" in candidate.origin]
    real_pool_candidates = [candidate for candidate in review.candidates if "用户真实池" in candidate.origin]
    lines = ["", "### 逐候选结论", ""]
    if market_candidates:
        lines.append("- 系统发现候选（逐只展示本地事实，不代表核心票）：")
        for candidate in market_candidates:
            lines.append(f"  - {render_candidate_fact(candidate)}")
    else:
        lines.append("- 系统发现候选：当前快照没有可确认代码的市场个股事实。")
    if real_pool_candidates:
        lines.append("- 用户真实池（不自动等同重点关注）：")
        for candidate in real_pool_candidates:
            lines.append(f"  - {render_candidate_fact(candidate)}")
    else:
        lines.append("- 用户真实池：无有效对象。")
    if step_number == 7:
        candidate_by_code = {candidate.code: candidate for candidate in review.candidates}
        lines.extend(["", "### 次日逐候选行动计划", ""])
        for action in review.final_plan_draft.candidate_actions:
            candidate = candidate_by_code[action.code]
            lines.append(
                f"- {candidate.code} {candidate.name}｜来源：{candidate.origin}｜"
                f"计划：{CANDIDATE_ACTION_LABELS[action.action]}｜仓位：{action.position_guidance}"
            )
            lines.append(f"  - 理由：{action.rationale}")
            lines.append(f"  - 执行触发：{action.trigger}")
            lines.append(f"  - 放弃条件：{action.abandon_condition}")
        if not review.final_plan_draft.candidate_actions:
            lines.append("- 当前没有可确认代码的候选对象。")
        lines.append("- 边界：以上是 AI 条件计划；只有触发条件实际出现时才成立，不会自动交易。")
    return lines


def render_candidate_fact(candidate: DetailedCandidate) -> str:
    return (
        f"{candidate.code} {candidate.name}｜板块：{candidate.sector}｜角色事实：{candidate.role}｜"
        f"5 日：{render_optional_percent(candidate.return_5d_percent)}｜"
        f"20 日：{render_optional_percent(candidate.return_20d_percent)}｜"
        f"5 日均成交额：{render_optional_amount(candidate.average_5d_amount)}｜"
        f"流通市值：{render_optional_amount(candidate.circulating_market_value)}"
    )


def render_optional_percent(value: float | None) -> str:
    return f"{value}%" if value is not None else "待确认"


def render_optional_amount(value: float | None) -> str:
    return f"{value} 元" if value is not None else "待确认"


def render_preview_step(step: ReviewStep, review: DetailedReview) -> list[str]:
    lines = ["", f"## STEP {step.number}: {step.title}", "", "### 本步结论", ""]
    if not review.context.previews:
        lines.extend([
            "- 状态：ai_conditional_plan。",
            f"- 主情景推断：{review.preview_outlook.primary_judgment}",
            f"- 值博率方向：{review.preview_outlook.odds_direction}。该方向只表示相对变化，不表示修复已发生。",
            f"- 主情景证据字段：{'、'.join(review.preview_outlook.evidence_field_keys)}",
            "- 以下四类条件是相对主情景的执行分支和否决线；其中数字是计划阈值，不是已发生事实，也不会自动写入 SQLite。",
        ])
        for preview in review.preview_drafts:
            lines.extend(
                [
                    "",
                    f"### {preview.target}",
                    "",
                    f"- 符合预期：{preview.expectation}",
                    f"- 超预期：{preview.over_expectation}",
                    f"- 不及预期：{preview.under_expectation}",
                    f"- 放弃条件：{preview.abandon_condition}",
                    f"- 证据字段：{'、'.join(preview.evidence_field_keys)}",
                    f"- 证据引用：{review.snapshot_reference}",
                ]
            )
        return lines
    lines.append("- 状态：confirmed_by_user。以下内容来自已保存的用户确认记录。")
    for preview in review.context.previews:
        lines.extend(
            [
                "",
                f"### {preview.target}",
                "",
                f"- 预演 ID：{preview.preview_id}",
                f"- 符合预期：{preview.expectation}",
                f"- 超预期：{preview.over_expectation}",
                f"- 不及预期：{preview.under_expectation}",
                f"- 放弃条件：{preview.abandon_condition}",
                f"- 证据引用：{preview.evidence_reference}",
            ]
        )
    return lines


def render_pattern_step(step: ReviewStep, review: DetailedReview) -> list[str]:
    priority_numbers, selection_reason = select_priority_pattern_numbers(review)
    priority_number_set = set(priority_numbers)
    lines = [
        "",
        f"## STEP {step.number}: {step.title}",
        "",
        "- 使用边界：完整逐项核验十二种模式；缺少竞价、分时或日线形态时不确认模式和买点。",
        f"- 当日优先级依据：{selection_reason}",
    ]
    for check in sorted(review.step9_checks, key=lambda item: item.pattern_number):
        available = "、".join(STEP9_EVIDENCE_LABELS[name] for name in check.available_evidence) or "无"
        missing = "、".join(STEP9_EVIDENCE_LABELS[name] for name in check.missing_evidence) or "无"
        lines.extend(
            [
                "",
                f"### 模式 {check.pattern_number}：{check.name}",
                "",
                f"- 当日优先级：{'优先核验' if check.pattern_number in priority_number_set else '常规核验'}",
                f"- 当前核验状态：{check.status}",
                f"- 已有证据：{available}",
                f"- 次日需要补看的触发事实：{missing}",
                f"- 失效边界：{check.invalidation_boundary}",
            ]
        )
    return lines


def select_priority_pattern_numbers(review: DetailedReview) -> tuple[tuple[int, ...], str]:
    sentiment = review.context.snapshot.sentiment
    limit_up_count = sentiment.get("limit_up_count")
    limit_down_count = sentiment.get("limit_down_count")
    if isinstance(limit_up_count, (int, float)) and isinstance(limit_down_count, (int, float)):
        if limit_down_count > limit_up_count:
            return (2, 6, 12), "跌停数高于涨停数，优先核验退潮转折、弱情绪抗跌和情绪指数共振模式。"
    ordered = sorted(
        review.step9_checks,
        key=lambda check: (len(check.missing_evidence), check.pattern_number),
    )
    return tuple(check.pattern_number for check in ordered[:3]), "当前没有明确弱情绪数量关系，按已有证据覆盖度选取三个优先核验模式。"


def render_final_step(step: ReviewStep, review: DetailedReview) -> list[str]:
    lines = ["", f"## STEP {step.number}: {step.title}", "", "### 最终应对", ""]
    if not review.final_responses:
        draft = review.final_plan_draft
        candidate_by_code = {candidate.code: candidate for candidate in review.candidates}
        focus_text = "、".join(
            f"{code} {candidate_by_code[code].name}" for code in draft.focus_codes
        ) or "无"
        lines.extend([
            "- 状态：ai_conditional_plan。",
            "- 以下直接给出 AI 条件行动方案；风险由用户自行承担，但历史事实仍必须来自本地证据，且不会自动交易或写入计划、池子、Observation 或 SQLite。",
            f"- 次日能否参与：{PARTICIPATION_DECISION_LABELS[draft.participation_decision]}。",
            f"- 建议总仓位：{draft.position_guidance}",
            f"- 次日总纲：{draft.summary}",
            f"- 行动对象：{focus_text}",
            f"- 执行条件：{draft.participation_condition}",
            f"- 风险与放弃边界：{draft.risk_boundary}",
            f"- 证据字段：{'、'.join(draft.evidence_field_keys)}",
            f"- 证据引用：{review.snapshot_reference}",
            "- 执行边界：计划参数不等于市场事实，只有对应触发实际出现时才成立。",
        ])
        lines.extend(["", "### 逐候选行动", ""])
        for action in draft.candidate_actions:
            candidate = candidate_by_code[action.code]
            lines.extend(
                [
                    f"- {candidate.code} {candidate.name}｜{CANDIDATE_ACTION_LABELS[action.action]}｜仓位：{action.position_guidance}",
                    f"  - 理由：{action.rationale}",
                    f"  - 触发：{action.trigger}",
                    f"  - 放弃：{action.abandon_condition}",
                ]
            )
        return lines
    lines.append("- 状态：confirmed_by_user。以下内容来自已保存的用户确认记录。")
    for response in review.final_responses:
        lines.extend(
            [
                "",
                f"### {response.final_id}",
                "",
                f"- 最终应对：{response.response}",
                f"- 风险边界：{response.risk_boundary}",
                f"- 关联真实池：{'、'.join(response.pool_codes)}",
                f"- 关联预演：{'、'.join(response.preview_ids)}",
                f"- 关联 Observation：{'、'.join(response.observation_ids)}",
                f"- 计划项：{response.plan_item}",
                f"- 证据引用：{response.evidence_reference}",
            ]
        )
    return lines


def render_hard_gap_labels(evidence: StepEvidence) -> str:
    return "、".join(HARD_GAP_LABELS.get(name, name) for name in evidence.hard_missing_fields) or "无"


def write_detailed_review_log(
    review: DetailedReview,
    framework_path: Path,
    output_path: Path,
    log_path: Path,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.detailed_review")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=review create-detailed trade_date=%s framework=%s snapshot=%s output=%s step_count=%s status=created",
            review.review_date,
            framework_path,
            review.snapshot_reference,
            output_path,
            len(review.framework.steps),
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
