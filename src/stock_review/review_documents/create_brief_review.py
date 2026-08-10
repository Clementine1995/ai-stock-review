# 本文件负责生成不依赖 LLM 的盘后复盘决策卡，并把未确认的系统候选限制在证据附录。

from __future__ import annotations

from pathlib import Path
from typing import Any
import logging

from stock_review.evidence.summarize_review_facts import (
    HARD_GAP_LABELS,
    ReviewFactsSummary,
    summarize_review_facts,
)
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
    real_pool_codes = {item.code for item in context.active_real_pool_items}
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
        f"- 两市成交额：{render_amount(facts.market.get('total_amount'))}",
        f"- 短线情绪：{render_sentiment(facts.sentiment)}",
        f"- 板块前三：{render_sector_summary(facts.sectors)}",
        "",
        "## 待人工确认",
        "",
        "- 市场阶段：待填写（上升 / 震荡 / 下降 / 不可判定）。",
        "- 市场风格：待填写（趋势 / 情绪连板 / 混合 / 不可判定）。",
        "- 情绪周期：待填写（上升 / 主升 / 退潮 / 混沌 / 冰点 / 不可判定）。",
        "- 核心或主升板块：待从当日事实中确认；系统不自动认定。",
        "- 次日是否参与：待用户确认；允许明确填写“不参与”。",
        "- 总仓位与风险边界：待用户填写。",
        "",
        "## 有效真实池",
        "",
    ]
    lines.extend(render_real_pool(context.active_real_pool_items, history_by_code))
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
            "## 附录：未确认系统候选",
            "",
            "- 使用边界：以下对象只来自当日快照事实，不属于有效真实池，不进入上方行动计划。",
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


def render_sentiment(sentiment: dict[str, Any]) -> str:
    if not sentiment:
        return "待确认"
    return (
        f"涨停 {render_number(sentiment.get('limit_up_count'))}；"
        f"跌停 {render_number(sentiment.get('limit_down_count'))}；"
        f"炸板率 {render_ratio_percent(sentiment.get('broken_board_rate'))}；"
        f"最高连板 {render_number(sentiment.get('highest_board'))}；"
        f"赚钱效应 {render_change_percent(sentiment.get('profit_effect_percent'))}（来源公式未验证）"
    )


def render_sector_summary(sectors: tuple[dict[str, Any], ...]) -> str:
    if not sectors:
        return "待确认"
    values = []
    for sector in sectors[:MAX_DECISION_SECTORS]:
        values.append(
            f"{sector.get('name') or '待确认'}（排名 {render_number(sector.get('rank'))}，涨停 {render_number(sector.get('limit_up_count'))}）"
        )
    return "；".join(values) or "待确认"


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
    for code, stock in candidates_by_code.items():
        history = history_by_code.get(code, {})
        lines.append(
            f"- {code} {stock.get('name') or '待确认'}｜板块：{stock.get('sector') or '待确认'}｜"
            f"角色事实：{stock.get('role') or '待确认'}｜"
            f"5 日：{render_change_percent(history.get('return_5d_percent'))}｜"
            f"20 日：{render_change_percent(history.get('return_20d_percent'))}"
        )
    return lines


def is_confirmed_stock_code(code: str) -> bool:
    return len(code) == 6 and code.isdigit()


def render_missing_fields(missing_fields: tuple[str, ...]) -> list[str]:
    if not missing_fields:
        return ["- Evidence Snapshot 标准字段缺口为 0；竞价、分时和身份确认仍需人工核查。"]
    return [f"- {HARD_GAP_LABELS.get(field_name, field_name)}" for field_name in missing_fields]


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
            "command=review create-brief trade_date=%s snapshot=%s output=%s real_pool_count=%s status=created",
            context.review_date,
            context.snapshot.source,
            output_path,
            len(context.active_real_pool_items),
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
