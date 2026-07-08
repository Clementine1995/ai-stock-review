# 本文件只负责 Markdown 文本渲染，不新增市场判断或证据推断。

from __future__ import annotations

from typing import Any

from stock_review.evidence.evidence_snapshot import EvidenceSnapshot
from stock_review.review_framework.parse_framework import ReviewFramework


# Evidence Snapshot 只作为事实来源展示，渲染层不得推导市场阶段、板块强弱结论或买卖建议。
def render_daily_review(
    trade_date: str,
    framework: ReviewFramework,
    evidence_snapshot: EvidenceSnapshot | None = None,
) -> str:
    evidence_status = (
        f"已接入 Evidence Snapshot，来源：{evidence_snapshot.source}，样本日期：{evidence_snapshot.sample_date}。"
        if evidence_snapshot
        else "未接入 Evidence Snapshot，自动证据统一标记为待补充。"
    )
    lines: list[str] = [
        f"# {trade_date} 每日复盘",
        "",
        f"- 交易日期：{trade_date}",
        f"- 复盘框架：{framework.source_path}",
        f"- 证据状态：{evidence_status}",
        "",
    ]

    for step in framework.steps:
        lines.extend(render_step(step.number, step.title, step.body, evidence_snapshot))

    return "\n".join(lines).rstrip() + "\n"


# 每个 STEP 固定保留五个填写区，保证人工复盘时能区分原始规则、事实证据和风险缺口。
def render_step(
    number: int,
    title: str,
    body: str,
    evidence_snapshot: EvidenceSnapshot | None = None,
) -> list[str]:
    original_rule = body if body else "（原始规则为空，请人工确认该 STEP 是否需要补充。）"
    return [
        f"## STEP {number}: {title}",
        "",
        "### 原始规则",
        "",
        original_rule,
        "",
        "### 自动证据",
        "",
        *render_evidence_lines(number, evidence_snapshot),
        "",
        "### 人工判断",
        "",
        "- 待填写。",
        "",
        "### 待验证假设",
        "",
        "- 待填写。",
        "",
        "### 风险缺口",
        "",
        *render_gap_lines(number, evidence_snapshot),
        "",
    ]


# STEP 与证据字段保持窄映射，避免把一个缺口在全篇重复展示。
def render_evidence_lines(number: int, snapshot: EvidenceSnapshot | None) -> list[str]:
    if snapshot is None:
        return ["- 待补充：当前未接入 Evidence Snapshot，不能自动生成市场、板块或个股事实。"]

    if number == 1:
        return render_market_lines(snapshot)
    if number in (2, 3):
        return render_sentiment_lines(snapshot)
    if number in (4, 5):
        return render_sector_lines(snapshot)
    if number in (6, 7):
        return render_stock_lines(snapshot)

    return [
        f"- 证据来源：{snapshot.source}；样本日期：{snapshot.sample_date}。",
        "- 当前 STEP 暂无直接映射的自动证据，请人工判断。",
    ]


def render_market_lines(snapshot: EvidenceSnapshot) -> list[str]:
    lines = [f"- 证据来源：{snapshot.source}；样本日期：{snapshot.sample_date}。"]
    indices = snapshot.market.get("indices")
    if isinstance(indices, list) and indices:
        for index_item in indices:
            if isinstance(index_item, dict):
                name = format_value(index_item.get("name"))
                close = format_value(index_item.get("close"))
                change_percent = format_value(index_item.get("change_percent"))
                lines.append(f"- 指数：{name}，收盘：{close}，涨跌幅：{change_percent}%。")
    if snapshot.market.get("total_amount") not in (None, ""):
        lines.append(f"- 两市成交额：{format_value(snapshot.market.get('total_amount'))}。")
    if snapshot.market.get("up_count") not in (None, "") or snapshot.market.get("down_count") not in (None, ""):
        lines.append(
            f"- 涨跌家数：上涨 {format_value(snapshot.market.get('up_count'))}，下跌 {format_value(snapshot.market.get('down_count'))}。"
        )
    return lines


def render_sentiment_lines(snapshot: EvidenceSnapshot) -> list[str]:
    sentiment = snapshot.sentiment
    return [
        f"- 证据来源：{snapshot.source}；样本日期：{snapshot.sample_date}。",
        f"- 涨停数：{format_value(sentiment.get('limit_up_count'))}。",
        f"- 跌停数：{format_value(sentiment.get('limit_down_count'))}。",
        f"- 炸板率：{format_value(sentiment.get('broken_board_rate'))}。",
        f"- 连板高度：{format_value(sentiment.get('highest_board'))}。",
        f"- 情绪温度：{format_value(sentiment.get('emotion_temperature'))}。",
    ]


def render_sector_lines(snapshot: EvidenceSnapshot) -> list[str]:
    lines = [f"- 证据来源：{snapshot.source}；样本日期：{snapshot.sample_date}。"]
    for sector in snapshot.sectors:
        name = format_value(sector.get("name"))
        strength = format_value(sector.get("strength"))
        change_percent = format_value(sector.get("change_percent"))
        turnover = format_value(sector.get("turnover"))
        market_value = format_value(sector.get("market_value"))
        turnover_rate = format_value(sector.get("turnover_rate"))
        up_count = format_value(sector.get("up_count"))
        down_count = format_value(sector.get("down_count"))
        leading_stock = format_value(sector.get("leading_stock"))
        leading_stock_change_percent = format_value(sector.get("leading_stock_change_percent"))
        limit_up_count = format_value(sector.get("limit_up_count"))
        core_stocks = format_list_value(sector.get("core_stocks"))
        lines.append(
            f"- 板块：{name}，状态：{strength}，涨跌幅：{change_percent}%，"
            f"成交额：{turnover}，总市值：{market_value}，换手率：{turnover_rate}%，"
            f"上涨家数：{up_count}，下跌家数：{down_count}，涨停家数：{limit_up_count}，"
            f"领涨股：{leading_stock}（{leading_stock_change_percent}%），核心票：{core_stocks}。"
        )
    return lines


def render_stock_lines(snapshot: EvidenceSnapshot) -> list[str]:
    lines = [f"- 证据来源：{snapshot.source}；样本日期：{snapshot.sample_date}。"]
    for stock in snapshot.stocks:
        code = format_value(stock.get("code"))
        name = format_value(stock.get("name"))
        exchange = format_value(stock.get("exchange"))
        sector = format_value(stock.get("sector"))
        role = format_value(stock.get("role"))
        change_percent = format_value(stock.get("change_percent"))
        reason = format_value(stock.get("reason"))
        lines.append(
            f"- 个股：{code} {name}，交易所：{exchange}，板块：{sector}，角色：{role}，"
            f"涨跌幅：{change_percent}%，原因：{reason}"
        )
    return lines


def render_gap_lines(number: int, snapshot: EvidenceSnapshot | None) -> list[str]:
    if snapshot is None:
        return ["- missing_evidence_snapshot：尚未导入或生成当前交易日证据快照。"]

    relevant_gaps = [gap for gap in snapshot.missing_fields if is_gap_relevant_to_step(number, gap)]
    if not relevant_gaps:
        return ["- 暂无当前 STEP 直接相关的证据缺口。"]
    return [f"- {gap}：当前 STEP 需要对应证据，请人工补齐或确认。" for gap in relevant_gaps]


def is_gap_relevant_to_step(number: int, gap: str) -> bool:
    step_gaps = {
        1: {"missing_indices", "missing_total_amount"},
        2: {"missing_sentiment", "missing_emotion_temperature"},
        3: {"missing_sentiment", "missing_emotion_temperature"},
        4: {"missing_sectors"},
        5: {"missing_sectors"},
        6: {"missing_stocks"},
        7: {"missing_stocks"},
    }
    return gap in step_gaps.get(number, set())


def format_value(value: Any) -> str:
    if value in (None, ""):
        return "待确认"
    return str(value)


def format_list_value(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "待确认"
    return "、".join(str(item) for item in value if item not in (None, "")) or "待确认"
