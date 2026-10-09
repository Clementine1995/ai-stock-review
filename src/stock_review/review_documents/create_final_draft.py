# 本文件负责让 LLM 选择已有证据字段与对象关联，并由本地固定渲染复盘事实边界。

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Callable

from stock_review.evidence.summarize_review_facts import (
    ReviewFactsSummary,
    StepEvidence,
    build_step_evidence,
    render_board_ladder,
    render_candidate_capacity_ranking,
    render_candidate_trend_capacity,
    render_hhxg_sentiment_history,
    render_hhxg_style_history,
    render_market_history,
    render_longhubang_evidence,
    render_repeated_rank_facts,
    render_sector_kline_evidence,
    render_us_market_evidence,
    summarize_review_facts,
)
from stock_review.llm.openai_compatible_client import OpenAICompatibleSettings, request_json_completion
from stock_review.pools.manage_pool_item import PoolItem
from stock_review.review_documents.build_review_context import ReviewContext, build_review_context


class FinalDraftError(ValueError):
    pass


STEP_TITLES = {
    1: "大盘阶段",
    2: "市场风格",
    3: "情绪周期",
    4: "核心板块",
    5: "主升板块阶段",
    6: "核心票",
    7: "重点关注",
}
UNAVAILABLE_EVIDENCE_MARKERS = (
    "不存在",
    "为空",
    "未采集",
    "没有可确认代码",
    "没有来源可确认",
    "没有可验证",
    "事实不可用",
)


@dataclass(frozen=True)
class FinalDraft:
    review_date: str
    step_drafts: tuple[dict[str, Any], ...]
    focus_candidates: tuple[dict[str, Any], ...]
    final_response: dict[str, Any]
    evidence_facts: dict[str, str]
    step_evidence: dict[int, StepEvidence]
    active_real_pool_items: tuple[PoolItem, ...]


# 草案入口只读取现有上下文；回调参数仅用于本地测试替换网络客户端。
def create_final_draft(
    review_date: str,
    snapshot_dir: Path,
    database_path: Path,
    request_completion: Callable[[OpenAICompatibleSettings, str, str], dict[str, Any]] = request_json_completion,
    settings: OpenAICompatibleSettings | None = None,
) -> FinalDraft:
    context = build_review_context(review_date, snapshot_dir=snapshot_dir, database_path=database_path)
    facts_summary = summarize_review_facts(review_date, snapshot_dir, snapshot_dir)
    raw_step_evidence = build_step_evidence(facts_summary)
    evidence_facts = build_evidence_facts(context, facts_summary)
    evidence_references = dict(facts_summary.evidence_references)
    evidence_references["pool.active_real"] = (
        (f"SQLite:{database_path}#active_real_pool",)
        if context.active_real_pool_items
        else ()
    )
    step_evidence = filter_available_step_evidence(
        raw_step_evidence,
        evidence_facts,
        evidence_references,
    )
    resolved_settings = settings or OpenAICompatibleSettings.from_environment()
    response = request_completion(
        resolved_settings,
        build_system_prompt(),
        build_user_prompt(
            context,
            evidence_references,
            evidence_facts,
            step_evidence,
        ),
    )
    return validate_final_draft(
        response,
        context,
        evidence_facts,
        step_evidence,
        evidence_references,
    )


def build_system_prompt() -> str:
    return (
        "你是 A 股短线盘后复盘的证据关联助手。必须只基于用户提供的 JSON 上下文生成 JSON 对象。"
        "你只能从每个 STEP 自己的 allowed_evidence_field_keys 中选择已有字段作为证据，不能跨 STEP 使用字段。"
        "每项 evidence_references 必须完整等于所选字段在 evidence_references_by_field 中的实际引用，不能借用其它字段来源。"
        "某个 STEP 的 allowed_evidence_field_keys 为空时，该 STEP 的 evidence_field_keys 和 evidence_references 都必须返回空数组。"
        "STEP 状态和硬缺口均由本地固定，你不得输出或决定它们；不能输出自然语言概览、市场强弱、风险、阶段、核心票、买点或交易结论。"
        "必须完整输出 STEP 1 至 7；active_real_pool_items 非空时，重点关注候选和最终应对必须逐一关联全部真实池代码，但这不表示重点关注或交易建议。"
    )


# 提示词只给模型选择字段和对象的权力，事实文本由本地 evidence_facts 固定呈现。
def build_user_prompt(
    context: ReviewContext,
    evidence_references: dict[str, tuple[str, ...]],
    evidence_facts: dict[str, str],
    step_evidence: dict[int, StepEvidence],
) -> str:
    payload = {
        "review_date": context.review_date,
        "snapshot": context.snapshot.to_mapping(),
        "active_real_pool_items": [
            {"pool_type": item.pool_type, "code": item.code, "name": item.name, "sectors": list(item.sectors), "reason": item.reason}
            for item in context.active_real_pool_items
        ],
        "previews": [preview.__dict__ for preview in context.previews],
        "missing_step_numbers": list(context.missing_step_numbers),
        "evidence_references_by_field": {
            field_name: list(references)
            for field_name, references in evidence_references.items()
            if references
        },
        "allowed_evidence_fields": evidence_facts,
        "step_evidence": [
            {
                "step_number": step_number,
                "allowed_evidence_field_keys": list(evidence.allowed_field_keys),
                "hard_missing_fields": list(evidence.hard_missing_fields),
            }
            for step_number, evidence in step_evidence.items()
        ],
        "required_output": {
            "step_drafts": [{"step_number": "1 至 7", "evidence_field_keys": ["该 STEP 的 allowed_evidence_field_keys 中的键；白名单为空时返回空数组"], "evidence_references": ["所选字段在 evidence_references_by_field 中对应的全部引用；没有所选字段时返回空数组"]}],
            "focus_candidates": [{"code": "每个有效真实池代码各一条", "evidence_field_keys": ["STEP 7 的 allowed_evidence_field_keys 中的键"], "evidence_references": ["所选字段在 evidence_references_by_field 中对应的全部引用"]}],
            "final_response": {"status": "pending_confirmation 或 unavailable", "related_pool_codes": ["全部有效真实池代码"], "related_preview_ids": ["已存在预演 ID"]},
        },
    }
    return json.dumps(payload, ensure_ascii=False)


# 本地事实只从 Evidence Snapshot、同日真实池历史文件和真实池对象生成，缺口也按同一快照原样呈现。
def build_evidence_facts(context: ReviewContext, facts_summary: ReviewFactsSummary) -> dict[str, str]:
    snapshot = context.snapshot
    return {
        "market.indices": render_indices(snapshot.market),
        "market.total_amount": render_market_total_amount(snapshot.market),
        "market.advance_decline": render_market_advance_decline(snapshot.market),
        "market.us_session": render_us_market_evidence(facts_summary.us_market_evidence),
        "market.history_range": render_market_history(facts_summary.market_history),
        "sentiment.limit_up_count": f"涨停数：{render_value(snapshot.sentiment.get('limit_up_count'))}",
        "sentiment.limit_down_count": f"跌停数：{render_value(snapshot.sentiment.get('limit_down_count'))}",
        "sentiment.broken_board_rate": f"炸板率：{render_ratio_percent(snapshot.sentiment.get('broken_board_rate'))}",
        "sentiment.highest_board": f"最高连板：{render_value(snapshot.sentiment.get('highest_board'))}",
        "sentiment.profit_effect_percent": render_profit_effect(snapshot.sentiment),
        "sentiment.history_style": f"多日涨停与连板高度事实：{render_hhxg_style_history(facts_summary.hhxg_history)}",
        "sentiment.history_cycle": f"多日短线情绪事实：{render_hhxg_sentiment_history(facts_summary.hhxg_history)}",
        "sentiment.board_ladder": (
            f"结构化连板梯队：{render_board_ladder(facts_summary.board_ladder)}"
        ),
        "stocks.longhubang": render_longhubang_evidence(facts_summary.longhubang_evidence),
        "candidate_history.capacity_ranking": (
            "有限候选容量与 5/20 日横截面排序："
            + render_candidate_capacity_ranking(facts_summary.candidate_capacity_ranking)
        ),
        "candidate_history.trend_capacity": (
            "有限候选趋势与成交容量事实："
            + render_candidate_trend_capacity(
                facts_summary.candidate_history_records,
                facts_summary.candidate_history_missing,
            )
        ),
        "sectors.rankings": render_sectors(snapshot.sectors),
        "sectors.daily_kline": render_sector_kline_evidence(
            facts_summary.sector_kline_evidence
        ),
        "sectors.history_repeated_rankings": (
            f"多日板块重复排行事实：{render_repeated_rank_facts(facts_summary.hhxg_history)}"
        ),
        "stocks.current": render_stocks(snapshot.stocks),
        "stocks.relative_comparison": render_stock_relative_comparisons(snapshot.stocks),
        "candidate_history.latest": render_pool_history_values(
            facts_summary.candidate_history_records, "close", "当日收盘"
        ),
        "candidate_history.return_5d_percent": render_pool_history_values(
            facts_summary.candidate_history_records, "return_5d_percent", "5 日涨跌幅"
        ),
        "candidate_history.return_20d_percent": render_pool_history_values(
            facts_summary.candidate_history_records, "return_20d_percent", "20 日涨跌幅"
        ),
        "pool.active_real": render_pool_items(context.active_real_pool_items),
        "pool_history.latest": render_pool_history_values(facts_summary.pool_history_records, "close", "当日收盘"),
        "pool_history.return_5d_percent": render_pool_history_values(facts_summary.pool_history_records, "return_5d_percent", "5 日涨跌幅"),
        "pool_history.return_20d_percent": render_pool_history_values(facts_summary.pool_history_records, "return_20d_percent", "20 日涨跌幅"),
        "snapshot.missing_fields": f"标准字段缺口：{'、'.join(snapshot.missing_fields) or '无'}",
    }


# 未采集或无实际引用的字段不进入旧草稿模型白名单，避免模型选择空事实。
def filter_available_step_evidence(
    step_evidence: dict[int, StepEvidence],
    evidence_facts: dict[str, str],
    evidence_references: dict[str, tuple[str, ...]],
) -> dict[int, StepEvidence]:
    return {
        step_number: StepEvidence(
            step_number,
            tuple(
                field_name
                for field_name in evidence.allowed_field_keys
                if is_evidence_fact_available(evidence_facts.get(field_name, ""))
                and evidence_references.get(field_name)
            ),
            evidence.hard_missing_fields,
        )
        for step_number, evidence in step_evidence.items()
    }


def is_evidence_fact_available(value: str) -> bool:
    if not value.strip() or any(marker in value for marker in UNAVAILABLE_EVIDENCE_MARKERS):
        return False
    primary_clause = value.split("；", maxsplit=1)[0].strip()
    return not primary_clause.endswith(("：待确认", "：无"))


# 通达信成交额只提供行情备用事实，进入模型提示词和详细报告时必须保留非官方口径标记。
def render_market_total_amount(market: dict[str, Any]) -> str:
    value = render_value(market.get("total_amount"))
    if market.get("total_amount_quality") != "market_quote_fallback":
        return f"成交额：{value}"
    source = render_value(market.get("total_amount_source"))
    return f"沪深成交额（通达信行情口径，非交易所官方汇总）：{value}；来源：{source}"


# 行情涨跌家数必须保留非交易所官方全市场统计的质量标签。
def render_market_advance_decline(market: dict[str, Any]) -> str:
    up_count = render_value(market.get("up_count"))
    down_count = render_value(market.get("down_count"))
    if market.get("advance_decline_quality") != "market_quote_breadth":
        return f"上涨家数：{up_count}；下跌家数：{down_count}"
    source = render_value(market.get("advance_decline_source"))
    return (
        "涨跌家数（通达信行情统计口径，非交易所官方全市场统计）："
        f"上涨 {up_count}，下跌 {down_count}；来源：{source}"
    )


# 本地校验只允许既有对象、既有证据和既有字段进入草案，草案不会写入 SQLite、池子、计划或 Observation。
def validate_final_draft(
    raw_draft: dict[str, Any],
    context: ReviewContext,
    evidence_facts: dict[str, str],
    step_evidence: dict[int, StepEvidence],
    evidence_references: dict[str, tuple[str, ...]],
) -> FinalDraft:
    step_drafts = require_mapping_list(raw_draft, "step_drafts")
    focus_candidates = require_mapping_list(raw_draft, "focus_candidates")
    final_response = required_mapping(raw_draft, "final_response")
    reject_free_text_fields(raw_draft, step_drafts, focus_candidates, final_response)

    allowed_codes = {item.code for item in context.active_real_pool_items}
    allowed_preview_ids = {preview.preview_id for preview in context.previews}

    validate_step_drafts(step_drafts, evidence_references, step_evidence)
    validate_focus_candidates(
        focus_candidates,
        allowed_codes,
        evidence_references,
        set(step_evidence[7].allowed_field_keys),
    )
    if required_text(final_response, "status") not in {"pending_confirmation", "unavailable"}:
        raise FinalDraftError("LLM 最终应对状态必须是 pending_confirmation 或 unavailable。")
    validate_identifier_list(final_response, "related_pool_codes", allowed_codes, "真实池代码", exact_match=True)
    validate_identifier_list(final_response, "related_preview_ids", allowed_preview_ids, "STEP 8 预演 ID", exact_match=False)
    return FinalDraft(
        context.review_date,
        tuple(step_drafts),
        tuple(focus_candidates),
        final_response,
        evidence_facts,
        step_evidence,
        context.active_real_pool_items,
    )


def validate_step_drafts(
    step_drafts: list[dict[str, Any]],
    evidence_references: dict[str, tuple[str, ...]],
    step_evidence: dict[int, StepEvidence],
) -> None:
    numbers: set[int] = set()
    for draft in step_drafts:
        number = normalize_step_number(draft.get("step_number"))
        draft["step_number"] = number
        if number in numbers:
            raise FinalDraftError("LLM 草案不能重复同一个 STEP。")
        numbers.add(number)
        if "status" in draft:
            raise FinalDraftError("LLM 草案 STEP 状态由本地字段映射决定，不允许模型输出。")
        validate_field_keys(draft, "evidence_field_keys", set(step_evidence[number].allowed_field_keys), f"STEP {number}")
        validate_references(draft, evidence_references)
        draft["status"] = step_evidence[number].status
    if numbers != set(STEP_TITLES):
        raise FinalDraftError("LLM 草案必须覆盖 STEP 1 至 7。")


def validate_focus_candidates(
    candidates: list[dict[str, Any]],
    allowed_codes: set[str],
    evidence_references: dict[str, tuple[str, ...]],
    allowed_fields: set[str],
) -> None:
    candidate_codes = {required_text(candidate, "code") for candidate in candidates}
    if candidate_codes != allowed_codes or len(candidates) != len(candidate_codes):
        raise FinalDraftError("LLM 草案必须逐一关联全部有效真实池代码。")
    for candidate in candidates:
        validate_field_keys(candidate, "evidence_field_keys", allowed_fields)
        validate_references(candidate, evidence_references)


def reject_free_text_fields(
    raw_draft: dict[str, Any],
    step_drafts: list[dict[str, Any]],
    focus_candidates: list[dict[str, Any]],
    final_response: dict[str, Any],
) -> None:
    prohibited = {"overview", "summary", "risk_boundary", "analysis", "judgment"}
    if any(field_name in raw_draft for field_name in prohibited) or any(
        any(field_name in item for field_name in prohibited) for item in step_drafts + focus_candidates + [final_response]
    ):
        raise FinalDraftError("LLM 草案不得包含自由文本判断，只能输出证据字段和对象关联。")


def normalize_step_number(value: Any) -> int:
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    elif isinstance(value, str) and value.strip().upper().startswith("STEP ") and value.strip()[5:].isdigit():
        number = int(value.strip()[5:])
    else:
        raise FinalDraftError("LLM 草案 STEP 编号必须是 1 至 7 的单个编号。")
    if number not in range(1, 8):
        raise FinalDraftError("LLM 草案 STEP 编号必须在 1 至 7 之间。")
    return number


def validate_references(
    value: dict[str, Any],
    evidence_references: dict[str, tuple[str, ...]],
) -> None:
    fields = value.get("evidence_field_keys")
    references = value.get("evidence_references")
    if not isinstance(fields, list) or not isinstance(references, list):
        raise FinalDraftError("LLM 草案证据引用必须与所选字段一致。")
    expected_references = {
        reference
        for field_name in fields
        for reference in evidence_references.get(field_name, ())
    }
    if (
        len(references) != len(set(references))
        or not all(isinstance(item, str) for item in references)
        or set(references) != expected_references
    ):
        raise FinalDraftError("LLM 草案证据引用必须与所选字段的实际来源一致。")


def validate_field_keys(value: dict[str, Any], field_name: str, allowed_fields: set[str], label: str = "") -> None:
    fields = value.get(field_name)
    if (
        not isinstance(fields, list)
        or (not allowed_fields and fields)
        or (allowed_fields and not fields)
        or not all(isinstance(item, str) and item in allowed_fields for item in fields)
    ):
        prefix = f"{label} " if label else ""
        raise FinalDraftError(f"{prefix}LLM 草案字段必须选择本地允许的证据字段：{field_name}。")


def validate_identifier_list(
    value: dict[str, Any], field_name: str, allowed_values: set[str], label: str, exact_match: bool
) -> None:
    values = value.get(field_name)
    if not isinstance(values, list) or not all(isinstance(item, str) and item in allowed_values for item in values):
        raise FinalDraftError(f"LLM 草案关联的{label}不在复盘上下文中。")
    if exact_match and set(values) != allowed_values:
        raise FinalDraftError(f"LLM 草案必须关联全部有效{label}。")


def required_mapping(value: dict[str, Any], field_name: str) -> dict[str, Any]:
    field_value = value.get(field_name)
    if not isinstance(field_value, dict):
        raise FinalDraftError(f"LLM 草案缺少对象字段：{field_name}。")
    return field_value


def require_mapping_list(value: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    field_value = value.get(field_name)
    if not isinstance(field_value, list) or not all(isinstance(item, dict) for item in field_value):
        raise FinalDraftError(f"LLM 草案字段必须是对象列表：{field_name}。")
    return field_value


def required_text(value: dict[str, Any], field_name: str) -> str:
    field_value = value.get(field_name)
    if not isinstance(field_value, str) or not field_value.strip():
        raise FinalDraftError(f"LLM 草案缺少文本字段：{field_name}。")
    return field_value.strip()


# 渲染只使用本地事实文本；模型选择的字段决定展示位置，但不能改变事实内容。
def render_final_draft(draft: FinalDraft) -> str:
    lines = [
        f"# {draft.review_date} LLM STEP 1-10 待确认草案",
        "",
        "- 使用边界：本草案不写入 SQLite、不修改池子、计划或 Observation，不构成买卖建议。",
        "- 呈现口径：LLM 只选择已有证据字段和对象关联；事实文本由本地快照固定渲染。",
        "",
        "## STEP 1-7 草案",
        "",
    ]
    step_drafts = {item["step_number"]: item for item in draft.step_drafts}
    for step_number, title in STEP_TITLES.items():
        item = step_drafts[step_number]
        lines.append(f"- STEP {step_number} {title}｜{item['status']}｜事实：{render_field_values(item['evidence_field_keys'], draft.evidence_facts)}")
        lines.append(f"  - 硬缺口：{render_hard_gaps(draft.step_evidence[step_number])}")
        lines.append(f"  - 证据引用：{'、'.join(item['evidence_references'])}")

    candidate_by_code = {item["code"]: item for item in draft.focus_candidates}
    lines.extend(["", "## STEP 7 真实池候选", ""])
    if draft.active_real_pool_items:
        for pool_item in draft.active_real_pool_items:
            candidate = candidate_by_code[pool_item.code]
            lines.append(
                f"- {pool_item.code} {pool_item.name}｜板块：{'、'.join(pool_item.sectors)}｜进入原因：{pool_item.reason or '待确认'}"
            )
            lines.append(f"  - 已关联事实：{render_field_values(candidate['evidence_field_keys'], draft.evidence_facts)}")
            lines.append(f"  - 状态：是否重点关注待用户确认。｜证据引用：{'、'.join(candidate['evidence_references'])}")
    else:
        lines.append("- 无有效真实池对象；不从真实池以外补充候选。")

    response = draft.final_response
    lines.extend([
        "",
        "## STEP 10 最终应对（待确认）",
        "",
        f"- 状态：{response['status']}",
        "- 草案：仅保留已验证的关联对象；最终应对、风险边界和计划写入必须由用户确认。",
        f"- 关联真实池：{'、'.join(response['related_pool_codes']) or '无'}",
        f"- 关联 STEP 8 预演：{'、'.join(response['related_preview_ids']) or '无'}",
    ])
    return "\n".join(lines).rstrip() + "\n"


def render_field_values(field_keys: list[str], evidence_facts: dict[str, str]) -> str:
    return "；".join(evidence_facts[field_name] for field_name in field_keys)


def render_indices(market: dict[str, Any]) -> str:
    indices = market.get("indices")
    if not isinstance(indices, list) or not indices:
        return "指数：待确认"
    return "；".join(
        f"{item.get('name') or '待确认'} 收盘 {render_value(item.get('close'))}，涨跌幅 {render_percent(item.get('change_percent'))}"
        for item in indices if isinstance(item, dict)
    )


def render_percent(value: Any) -> str:
    return f"{value}%" if value not in (None, "") else "待确认"


# 来源直接提供的指标与本地涨跌停比例代理不是同一口径，模型输入必须明确区分。
def render_profit_effect(sentiment: dict[str, Any]) -> str:
    value = render_percent(sentiment.get("profit_effect_percent"))
    source = render_value(sentiment.get("profit_effect_source"))
    calculation = sentiment.get("profit_effect_calculation")
    if calculation == "limit_up_ratio_proxy":
        return (
            f"涨跌停家数比例代理：{value}；来源：{source}；"
            "计算：涨停数/(涨停数+跌停数)；不等同完整赚钱效应"
        )
    return f"赚钱效应代理：{value}；来源：{source}；来源公式未公开"


def render_sectors(sectors: list[dict[str, Any]]) -> str:
    if not sectors:
        return "板块排行：待确认"
    grouped_texts: list[str] = []
    for source_type, label in (
        ("concept", "AKShare 概念涨幅榜"),
        ("industry", "AKShare 行业涨幅榜"),
        ("industry_ths", "AKShare 同花顺行业涨幅榜"),
    ):
        records = [item for item in sectors if item.get("source_type") == source_type]
        if records:
            grouped_texts.append(
                f"{label}：" + "；".join(render_sector_rank_item(item) for item in records)
            )
    hot_themes = [item for item in sectors if item.get("source_type") == "hhxg_hot_theme"]
    if hot_themes:
        grouped_texts.append(
            "热门题材：" + "；".join(render_sector_rank_item(item) for item in hot_themes)
        )

    # hhxg 行业榜与概念榜共用 source_type，排名重新从一开始时表示进入下一组排行。
    strong_sectors = [item for item in sectors if item.get("source_type") == "hhxg_strong_sector"]
    strong_groups: list[list[dict[str, Any]]] = []
    for item in strong_sectors:
        previous_group = strong_groups[-1][-1].get("ranking_group") if strong_groups else None
        current_group = item.get("ranking_group")
        if not strong_groups or (
            strong_groups[-1]
            and (
                (current_group and current_group != previous_group)
                or (not current_group and item.get("rank") in (1, "1"))
            )
        ):
            strong_groups.append([])
        strong_groups[-1].append(item)
    strong_group_labels = ("强势行业", "强势概念")
    for index, records in enumerate(strong_groups):
        label = (
            strong_group_labels[index]
            if index < len(strong_group_labels)
            else f"强势板块第 {index + 1} 组"
        )
        grouped_texts.append(
            f"{label}：" + "；".join(render_sector_rank_item(item) for item in records)
        )
    if not grouped_texts:
        grouped_texts.append("其它排行：" + "；".join(render_sector_rank_item(item) for item in sectors))
        return "板块排行：" + "｜".join(grouped_texts)

    # 三类榜单的样本和排序口径相互独立，同名板块的净流入也不能跨组归并。
    return (
        "板块排行（热门题材、强势行业、强势概念为独立分组口径；"
        "净流入仅在所属分组内解释，不跨组比较或合并）："
        + "｜".join(grouped_texts)
    )


def render_sector_rank_item(item: dict[str, Any]) -> str:
    details = [f"排行 {render_value(item.get('rank'))}"]
    if item.get("change_percent") not in (None, ""):
        details.append(f"涨跌幅 {render_percent(item.get('change_percent'))}")
    if item.get("limit_up_count") not in (None, ""):
        details.append(f"涨停 {render_value(item.get('limit_up_count'))}")
    if item.get("up_count") not in (None, "") and item.get("down_count") not in (None, ""):
        details.append(
            f"上涨/下跌 {render_value(item.get('up_count'))}/{render_value(item.get('down_count'))} 家"
        )
    if item.get("net_inflow_yi") not in (None, ""):
        details.append(f"净流入 {render_value(item.get('net_inflow_yi'))} 亿")
    if item.get("net_inflow") not in (None, ""):
        details.append(f"净流入 {render_value(item.get('net_inflow'))}")
    leading_stocks = render_leading_stock_names(item.get("leading_stock"))
    if leading_stocks:
        details.append(f"领涨 {leading_stocks}")
    return f"{item.get('name') or '待确认'}（{'，'.join(details)}）"


def render_leading_stock_names(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    names = [
        str(item.get("name") or "").strip()
        for item in value
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    return "、".join(names)


def render_stocks(stocks: list[dict[str, Any]]) -> str:
    visible = [item for item in stocks if str(item.get("name") or "").strip()]
    if not visible:
        return "个股事实：没有可确认代码"
    return "个股事实：" + "；".join(
        f"{item.get('code') if str(item.get('code') or '') not in {'', '待确认'} else '代码待确认'} "
        f"{item.get('name') or '待确认'}（板块 {item.get('sector') or '待确认'}，"
        f"角色 {item.get('role') or '待确认'}，角色来源 {item.get('role_source') or '待确认'}）"
        for item in visible
    )


# 相对比较事实直接进入 STEP 6/7 提示词，并保留覆盖率，避免用少数对象放开整组缺口。
def render_stock_relative_comparisons(stocks: list[dict[str, Any]]) -> str:
    complete_rows: list[str] = []
    for stock in stocks:
        relative = stock.get("relative_strength")
        continuous = stock.get("continuous_comparison")
        if not (
            isinstance(relative, dict)
            and relative.get("completeness") == "complete"
            and isinstance(continuous, dict)
            and continuous.get("completeness") == "complete"
        ):
            continue
        recent_up_ratio = continuous.get("recent_up_ratio")
        recent_up_percent = (
            render_ratio_percent(recent_up_ratio)
            if isinstance(recent_up_ratio, (int, float))
            else "待确认"
        )
        complete_rows.append(
            f"{stock.get('code') or '代码待确认'} {stock.get('name') or '待确认'}："
            f"{relative.get('window_start') or '待确认'} 至 {relative.get('window_end') or '待确认'}，"
            f"个股 {render_percent(relative.get('stock_return_percent'))}，"
            f"基准 {render_percent(relative.get('benchmark_return_percent'))}，"
            f"超额 {render_value(relative.get('excess_return_percent'))} 个百分点，"
            f"方向 {relative.get('direction') or '待确认'}；"
            f"末端连涨 {render_value(continuous.get('consecutive_up_days'))} 日，"
            f"近 5 个交易对上涨比例 {recent_up_percent}，"
            f"方向 {continuous.get('direction') or '待确认'}"
        )
    coverage = f"覆盖 {len(complete_rows)}/{len(stocks)}"
    if not complete_rows:
        return f"个股相对强弱与连续比较事实：{coverage}；待确认"
    return f"个股相对强弱与连续比较事实：{coverage}；" + "；".join(complete_rows)


def render_pool_items(items: tuple[PoolItem, ...]) -> str:
    if not items:
        return "有效真实池：无"
    return "有效真实池：" + "；".join(f"{item.code} {item.name}" for item in items)


def render_pool_history_values(records: tuple[dict[str, Any], ...], field_name: str, label: str) -> str:
    values = [
        f"{record.get('code') or '待确认'} {record.get('name') or '待确认'} {label} {render_value(record.get(field_name))}"
        for record in records
    ]
    return "；".join(values) if values else f"真实池{label}：待确认"


def render_hard_gaps(step_evidence: StepEvidence) -> str:
    if not step_evidence.hard_missing_fields:
        return "无"
    return "、".join(step_evidence.hard_missing_fields)


def render_value(value: Any) -> str:
    return str(value) if value not in (None, "") else "待确认"


# 炸板率在 Evidence Snapshot 中保存为 0 至 1 的比例，提供给模型时统一转换为业务百分比。
def render_ratio_percent(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "待确认"
    percent = Decimal(str(value)) * 100
    return f"{format(percent.normalize(), 'f')}%"
