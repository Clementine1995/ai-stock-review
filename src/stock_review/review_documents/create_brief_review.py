# 本文件负责生成不依赖 LLM 的盘后复盘决策卡，并把未确认的系统候选限制在证据附录。

from __future__ import annotations

from pathlib import Path
from typing import Any
import logging
import re

from stock_review.evidence.summarize_review_facts import (
    HARD_GAP_LABELS,
    ReviewFactsSummary,
    summarize_review_facts,
)
from stock_review.pools.classify_pool_freshness import StalePoolItem
from stock_review.pools.manage_pool_item import DEFAULT_DATABASE_PATH, PoolItem
from stock_review.review_documents.build_review_context import ReviewContext, build_review_context


DEFAULT_BRIEF_REPORT_DIR = Path("reports") / "daily"
DEFAULT_BRIEF_REVIEW_LOG_PATH = Path("logs") / "stock_review.log"
MAX_DECISION_SECTORS = 3


# 正式入口只读取本地快照、候选历史和真实池，缺少 LLM 配置或部分证据时仍生成简版报告。
def create_brief_review_report(
    review_date: str,
    snapshot_dir: Path,
    database_path: Path = DEFAULT_DATABASE_PATH,
    output_dir: Path = DEFAULT_BRIEF_REPORT_DIR,
    log_path: Path = DEFAULT_BRIEF_REVIEW_LOG_PATH,
) -> Path:
    context = build_review_context(
        review_date,
        snapshot_dir=snapshot_dir,
        database_path=database_path,
    )
    facts = summarize_review_facts(review_date, snapshot_dir, snapshot_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{review_date}_brief_review.md"
    output_path.write_text(render_brief_review(context, facts), encoding="utf-8")
    write_brief_review_log(context, output_path, log_path)
    return output_path


# 决策区只展示事实、真实池和待人工填写项；系统候选不会进入参与结论、仓位或行动计划。
def render_brief_review(context: ReviewContext, facts: ReviewFactsSummary) -> str:
    history_by_code = build_history_by_code(facts)
    real_pool_codes = set(context.known_real_pool_codes)
    lines = [
        f"# {context.review_date} 盘后复盘决策卡",
        "",
        f"- Evidence Snapshot：{context.snapshot.source}｜样本日期：{context.snapshot.sample_date}",
        "- 生成方式：仅使用本地证据，不调用 LLM；证据不完整时保留缺口，不补造结论。",
        "- 行动边界：系统候选仅进入附录；未经用户确认，不生成买卖方向、仓位或正式计划。",
        "",
        "## 当日事实",
        "",
        f"- 指数：{render_indices(facts.market)}",
        f"- {render_market_amount_fact(facts.market)}",
        f"- {render_market_breadth_fact(facts.market)}",
        f"- 短线情绪：{render_sentiment(facts.sentiment)}",
        f"- 热门题材前三：{render_sector_group(facts.sectors, 'hhxg_hot_theme')}",
        f"- 强势板块前列：{render_sector_group(facts.sectors, 'hhxg_strong_sector', maximum_rank=3)}",
        "",
        "## 待人工确认",
        "",
    ]
    lines.extend(render_manual_confirmations(context))
    lines.extend(["", "## 有效真实池", ""])
    lines.extend(render_real_pool(context.active_real_pool_items, history_by_code))
    lines.extend(["", "## 待复核真实池", ""])
    lines.extend(render_stale_real_pool(context.stale_real_pool_items))
    lines.extend(
        [
            "",
            "## 次日四类条件",
            "",
            "- 符合预期：待用户填写。",
            "- 超预期：待用户填写。",
            "- 不及预期：待用户填写。",
            "- 放弃条件：待用户填写。",
            "",
            "## Observation",
            "",
            "- 今日是否形成一条可验证判断：待用户确认。",
            "- 如创建，必须补充假设、成立条件、失效条件、证据来源和最晚回填日期。",
            "",
            "## 关键证据缺口",
            "",
        ]
    )
    lines.extend(render_missing_fields(facts.missing_fields))
    lines.extend(
        [
            "",
            "## 数据质量提醒",
            "",
        ]
    )
    lines.extend(render_collection_failures(facts.events))
    lines.extend(
        [
            "",
            "## 每日系统候选箱",
            "",
            "- 使用边界：以下对象只来自当日快照事实，不属于有效真实池，不进入上方行动计划。",
            "- 确认方式：只需回复候选编号；WorkBuddy 必须再次展示入池命令并取得确认。",
        ]
    )
    lines.extend(render_system_candidates(facts.stocks, history_by_code, real_pool_codes))
    lines.extend(
        [
            "",
            "## 附录：证据来源",
            "",
            f"- 市场：{facts.field_sources.get('market', '待确认')}",
            f"- 情绪：{facts.field_sources.get('sentiment', '待确认')}",
            f"- 板块：{facts.field_sources.get('sectors', '待确认')}",
            f"- 个股：{facts.field_sources.get('stocks', '待确认')}",
            f"- 快照文件：{context.review_date}_snapshot.json",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_history_by_code(facts: ReviewFactsSummary) -> dict[str, dict[str, Any]]:
    records = (*facts.pool_history_records, *facts.candidate_history_records)
    return {
        str(record.get("code")): record
        for record in records
        if record.get("code") not in (None, "")
    }


def render_indices(market: dict[str, Any]) -> str:
    indices = market.get("indices")
    if not isinstance(indices, list) or not indices:
        return "待确认"
    values = []
    for index in indices:
        if not isinstance(index, dict):
            continue
        values.append(
            f"{index.get('name') or '待确认'} {render_number(index.get('close'))}（{render_change_percent(index.get('change_percent'))}）"
        )
    return "；".join(values) or "待确认"


def render_market_amount_fact(market: dict[str, Any]) -> str:
    label = (
        "沪深成交额（通达信行情口径，非交易所官方汇总）"
        if market.get("total_amount_quality") == "market_quote_fallback"
        else "两市成交额"
    )
    return f"{label}：{render_amount(market.get('total_amount'))}"


def render_market_breadth_fact(market: dict[str, Any]) -> str:
    if market.get("up_count") in (None, "") or market.get("down_count") in (None, ""):
        return "涨跌家数：待确认"
    label = (
        "涨跌家数（通达信行情统计口径，非交易所官方全市场统计）"
        if market.get("advance_decline_quality") == "market_quote_breadth"
        else "涨跌家数"
    )
    return (
        f"{label}：上涨 {render_number(market.get('up_count'))}；"
        f"下跌 {render_number(market.get('down_count'))}"
    )


def render_sentiment(sentiment: dict[str, Any]) -> str:
    if not sentiment:
        return "待确认"
    profit_effect = sentiment.get("profit_effect_percent")
    if profit_effect in (None, ""):
        profit_effect_text = "赚钱效应代理 未提供"
    elif sentiment.get("profit_effect_calculation") == "limit_up_ratio_proxy":
        profit_effect_text = (
            f"涨跌停家数比例代理 {render_change_percent(profit_effect)}"
            "（涨停数/(涨停数+跌停数)，不等同完整赚钱效应）"
        )
    else:
        profit_effect_text = (
            f"赚钱效应代理 {render_change_percent(profit_effect)}（来源公式未公开）"
        )
    return (
        f"涨停 {render_number(sentiment.get('limit_up_count'))}；"
        f"跌停 {render_number(sentiment.get('limit_down_count'))}；"
        f"炸板率 {render_ratio_percent(sentiment.get('broken_board_rate'))}；"
        f"最高连板 {render_number(sentiment.get('highest_board'))}；"
        f"{profit_effect_text}"
    )


def render_sector_group(
    sectors: tuple[dict[str, Any], ...],
    source_type: str,
    maximum_rank: int | None = None,
) -> str:
    selected = [sector for sector in sectors if sector.get("source_type") == source_type]
    if maximum_rank is not None:
        selected = [sector for sector in selected if numeric_rank(sector.get("rank")) <= maximum_rank]
    else:
        selected = selected[:MAX_DECISION_SECTORS]
    if not selected:
        return "待确认"
    values = []
    for sector in selected:
        values.append(
            f"{sector.get('name') or '待确认'}（排名 {render_number(sector.get('rank'))}，"
            f"净流入 {render_number(sector.get('net_inflow_yi'))} 亿）"
        )
    return "；".join(values) or "待确认"


def numeric_rank(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 999


def render_manual_confirmations(context: ReviewContext) -> list[str]:
    records_by_step = {record.step_number: record for record in context.step_records}
    labels = {
        1: "市场阶段",
        2: "市场风格",
        3: "情绪周期",
        4: "核心板块",
        5: "主升板块及阶段",
    }
    lines = []
    for step_number, label in labels.items():
        record = records_by_step.get(step_number)
        if record is None:
            lines.append(f"- STEP {step_number} {label}：待用户填写。")
        else:
            lines.append(f"- STEP {step_number} {label}：已确认｜{record.judgment}")
    lines.extend(
        [
            "- 次日是否参与：待用户确认；允许明确填写“不参与”。",
            "- 总仓位与风险边界：待用户填写。",
        ]
    )
    return lines


def render_real_pool(
    items: tuple[PoolItem, ...],
    history_by_code: dict[str, dict[str, Any]],
) -> list[str]:
    if not items:
        return ["- 当前没有有效真实池对象；不从系统候选自动补充重点关注。"]
    lines = []
    for item in items:
        history = history_by_code.get(item.code, {})
        lines.append(
            f"- {item.code} {item.name}｜板块：{'、'.join(item.sectors)}｜"
            f"5 日：{render_change_percent(history.get('return_5d_percent'))}｜"
            f"20 日：{render_change_percent(history.get('return_20d_percent'))}｜"
            "次日结论：待用户确认"
        )
    return lines


def render_stale_real_pool(items: tuple[StalePoolItem, ...]) -> list[str]:
    if not items:
        return ["- 无。"]
    return [
        f"- {stale_item.item.code} {stale_item.item.name}｜最近确认：{stale_item.last_confirmed_date}｜"
        f"已连续 {stale_item.unconfirmed_trading_days} 个本地交易日未确认｜"
        "处理：待用户选择续期 / 暂停"
        for stale_item in items
    ]


def render_system_candidates(
    stocks: tuple[dict[str, Any], ...],
    history_by_code: dict[str, dict[str, Any]],
    real_pool_codes: set[str],
) -> list[str]:
    unresolved_count = sum(
        1
        for stock in stocks
        if not is_confirmed_stock_code(str(stock.get("code") or ""))
    )
    candidates_by_code = {
        str(stock.get("code")): stock
        for stock in stocks
        if is_confirmed_stock_code(str(stock.get("code") or ""))
        and str(stock.get("code")) not in real_pool_codes
    }
    if not candidates_by_code and not unresolved_count:
        return ["- 无。"]
    lines = []
    if unresolved_count:
        lines.append(f"- 另有 {unresolved_count} 个对象缺少可确认的六位股票代码，不进入逐只候选清单。")
    ordered_candidates = sorted(
        candidates_by_code.items(),
        key=lambda item: (-extract_board_count(item[1].get("role_source")), item[0]),
    )
    for index, (code, stock) in enumerate(ordered_candidates, start=1):
        history = history_by_code.get(code, {})
        lines.append(
            f"- 候选 {index}｜{code} {stock.get('name') or '待确认'}｜板块：{stock.get('sector') or '待确认'}｜"
            f"角色事实：{stock.get('role_source') or stock.get('role') or '待确认'}｜"
            f"当日：{render_change_percent(stock.get('change_percent'))}｜"
            f"5 日：{render_change_percent(history.get('return_5d_percent'))}｜"
            f"20 日：{render_change_percent(history.get('return_20d_percent'))}"
        )
    return lines


def extract_board_count(value: Any) -> int:
    match = re.search(r"（(\d+)板）", str(value or ""))
    return int(match.group(1)) if match else 0


def is_confirmed_stock_code(code: str) -> bool:
    return len(code) == 6 and code.isdigit()


def render_missing_fields(missing_fields: tuple[str, ...]) -> list[str]:
    if not missing_fields:
        return ["- Evidence Snapshot 标准字段缺口为 0；竞价、分时和身份确认仍需人工核查。"]
    return [f"- {HARD_GAP_LABELS.get(field_name, field_name)}" for field_name in missing_fields]


def render_collection_failures(events: tuple[dict[str, Any], ...]) -> list[str]:
    failures = [event for event in events if "失败" in str(event.get("title") or "")]
    if not failures:
        return ["- 当日快照未记录数据源采集失败。"]
    return [
        f"- {event.get('title') or '待确认'}｜来源：{event.get('source') or '待确认'}"
        for event in failures
    ]


def render_amount(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "待确认"
    if abs(value) >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f} 万亿元"
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.2f} 亿元"
    return f"{value:.2f} 元"


def render_number(value: Any) -> str:
    if value in (None, ""):
        return "待确认"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def render_change_percent(value: Any) -> str:
    if value in (None, ""):
        return "待确认"
    return f"{render_number(value)}%"


def render_ratio_percent(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "待确认"
    normalized = value * 100 if abs(value) <= 1 else value
    return f"{normalized:.2f}%"


def write_brief_review_log(
    context: ReviewContext,
    output_path: Path,
    log_path: Path,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.brief_review")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=review create-brief trade_date=%s snapshot=%s output=%s fresh_pool_count=%s stale_pool_count=%s status=created",
            context.review_date,
            context.snapshot.source,
            output_path,
            len(context.active_real_pool_items),
            len(context.stale_real_pool_items),
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
