# 本文件把决策输入与规则结论渲染成 Markdown 决策卡。
# 渲染只陈列上游事实与规则结论，不新增数字、对象或判断；缺失内容必须显式写成待确认。

from __future__ import annotations

from typing import Any

from stock_review.decision.decision_inputs import DecisionInputs, DecisionReading

CNY_YI = 1e8


def render_decision_card(
    inputs: DecisionInputs,
    reading: DecisionReading,
    *,
    generated_at: str,
    data_note: str = "本卡只回答「情绪 / 能否参与 / 参与什么 / 参与多少」，不构成买卖指令，仓位需由用户最终确认。",
) -> str:
    sections = [
        _render_header(inputs, generated_at, data_note),
        _render_answers(reading),
        _render_sentiment_panel(inputs, reading),
        _render_index_panel(inputs, reading),
        _render_sector_panel(reading),
        _render_candidates(reading),
        _render_news(inputs),
        _render_next_day_plan(inputs, reading),
        _render_sources(inputs),
    ]
    return "\n\n".join(sections).rstrip() + "\n"


def _render_header(inputs: DecisionInputs, generated_at: str, data_note: str) -> str:
    lines = [
        f"# {inputs.trade_date} 盘后决策卡",
        "",
        f"- 生成时间：{generated_at}",
        f"- 交易日：{inputs.trade_date}",
        f"- 用途说明：{data_note}",
    ]
    return "\n".join(lines)


# 四个问题必须直给结论，同时把依据与规则来源并列，避免只有结论没有出处。
def _render_answers(reading: DecisionReading) -> str:
    sentiment = reading.sentiment_stage
    participation = reading.participation
    position = reading.position_band
    total_range = position.get("total_range") or (0, 0)

    lines = [
        "## 一、四个问题",
        "",
        "| 问题 | 结论 | 依据 |",
        "| --- | --- | --- |",
        f"| 当前市场情绪 | {sentiment.get('label')}"
        + (f"（情绪温度 {sentiment.get('temperature')}）" if sentiment.get("temperature") is not None else "")
        + f" | {_join_brief(sentiment.get('evidence'))} |",
        f"| 明天能不能买 | {participation.get('label')} | {participation.get('rule')} |",
        f"| 买什么 | 主线方向：{reading.sector_leadership.get('label')}（候选见第五节） | {_join_brief(reading.sector_leadership.get('evidence'))} |",
        f"| 买多少 | 总仓位 {total_range[0]}%–{total_range[1]}%；单票 ≤ {position.get('single_name_cap')}%；最多 {position.get('max_names')} | {position.get('rule')} |",
        "",
        f"- 参与倾向规则：{participation.get('rule')}",
        f"- 情绪阶段规则：{sentiment.get('rule')}",
        f"- 仓位说明：{_join_sentences(position.get('notes') or [])}",
    ]
    return "\n".join(lines)


# 情绪面板把当日横截面与前一交易日并列，便于直接看出走强还是走弱。
def _render_sentiment_panel(inputs: DecisionInputs, reading: DecisionReading) -> str:
    detail = reading.sentiment_stage.get("detail") or {}
    previous_date = detail.get("previous_trade_date") or "前一交易日"
    lines = ["## 二、情绪面板", ""]
    lines.extend(
        [
            f"| 指标 | 当日 | {previous_date} |",
            "| --- | --- | --- |",
            f"| 涨停家数 | {_fmt(detail.get('limit_up_count'))} | {_fmt(detail.get('previous_limit_up_count'))} |",
            f"| 跌停家数 | {_fmt(detail.get('limit_down_count'))} | {_fmt(detail.get('previous_limit_down_count'))} |",
            f"| 炸板率 | {_fmt_rate(detail.get('broken_board_rate'))} | {_fmt_rate(detail.get('previous_broken_board_rate'))} |",
            f"| 最高连板 | {_fmt(detail.get('highest_board'))} | {_fmt(detail.get('previous_highest_board'))} |",
            f"| 全市场上涨 | {_fmt(detail.get('up_count'))} | — |",
            f"| 全市场下跌 | {_fmt(detail.get('down_count'))} | — |",
        ]
    )
    if inputs.breadth and inputs.breadth.get("activity_percent") is not None:
        lines.append(f"| 市场活跃度 | {inputs.breadth['activity_percent']:.2f}% | — |")
    lines.extend(["", "证据："])
    lines.extend(f"- {item}" for item in reading.sentiment_stage.get("evidence") or [])
    if inputs.sentiment_history:
        lines.append("")
        lines.append("近期情绪序列：")
        lines.append("")
        lines.append("| 交易日 | 涨停 | 跌停 | 炸板率 | 最高连板 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for day in inputs.sentiment_history:
            lines.append(
                f"| {day.trade_date} | {day.limit_up_count} | {day.limit_down_count} | "
                f"{_fmt_rate(day.broken_board_rate)} | {_fmt(day.highest_board)} |"
            )
    return "\n".join(lines)


def _render_index_panel(inputs: DecisionInputs, reading: DecisionReading) -> str:
    lines = ["## 三、指数与量能", ""]
    lines.extend(
        [
            f"- 指数阶段：**{reading.market_stage.get('label')}**",
            f"- 量能档位：**{reading.amount_band.get('label')}**",
        ]
    )
    for item in reading.market_stage.get("evidence") or []:
        lines.append(f"- {item}")
    for item in reading.amount_band.get("evidence") or []:
        lines.append(f"- {item}")
    lines.append(f"- 量能含义：{reading.amount_band.get('rule')}")

    if inputs.indices:
        lines.extend(["", "| 指数 | 收盘 | 涨跌幅 |", "| --- | --- | --- |"])
        for row in inputs.indices.values():
            lines.append(
                f"| {row.get('name')} | {_fmt(row.get('close'))} | {_fmt_percent(row.get('change_percent'))} |"
            )

    primary = inputs.index_history.get("sh000001") or ()
    if primary:
        lines.extend(["", "近期指数路径（上证指数）：", ""])
        lines.append("| 交易日 | 开盘 | 最高 | 最低 | 收盘 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for day in primary:
            lines.append(
                f"| {day.trade_date} | {_fmt(day.open)} | {_fmt(day.high)} | {_fmt(day.low)} | {_fmt(day.close)} |"
            )

    known_amounts = [(day, value) for day, value in inputs.amount_history if value]
    if known_amounts:
        lines.extend(["", "两市 A 股成交额序列（亿元）：", ""])
        lines.append("| 交易日 | 成交额 | 环比 |")
        lines.append("| --- | --- | --- |")
        previous_value: float | None = None
        for day, value in known_amounts:
            change = f"{(value / previous_value - 1) * 100:+.2f}%" if previous_value else "—"
            lines.append(f"| {day} | {round(value / CNY_YI, 1)} | {change} |")
            previous_value = value
    return "\n".join(lines)


def _render_sector_panel(reading: DecisionReading) -> str:
    leadership = reading.sector_leadership
    lines = ["## 四、板块主线", ""]
    lines.extend(f"- {item}" for item in leadership.get("evidence") or [])

    concepts = leadership.get("concepts") or []
    tdx_concepts = leadership.get("tdx_concepts") or []
    if concepts:
        lines.extend(["", "概念板块涨幅前列：", "", "| 排名 | 板块 | 涨跌幅 | 上涨/下跌 | 领涨股 |", "| --- | --- | --- | --- | --- |"])
        for row in concepts:
            lines.append(
                f"| {row.get('rank')} | {row.get('name')} | {_fmt_percent(row.get('change_percent'))} | "
                f"{_fmt(row.get('up_count'))}/{_fmt(row.get('down_count'))} | {_fmt_text(row.get('leading_stock'))} |"
            )
    elif tdx_concepts:
        lines.extend(["", "- akshare 概念板块榜缺失，概念维度改由通达信板块行情补充（见下表）。"])
    else:
        lines.extend(["", "- 概念板块榜缺失，本次未采到概念维度证据。"])
    industries = leadership.get("industries") or []
    if industries:
        lines.extend(["", "行业板块涨幅前列：", "", "| 排名 | 板块 | 涨跌幅 | 上涨/下跌 | 领涨股 |", "| --- | --- | --- | --- | --- |"])
        for row in industries:
            lines.append(
                f"| {row.get('rank')} | {row.get('name')} | {_fmt_percent(row.get('change_percent'))} | "
                f"{_fmt(row.get('up_count'))}/{_fmt(row.get('down_count'))} | {_fmt_text(row.get('leading_stock'))} |"
            )

    # 通达信板块是概念维度的替代证据（akshare 概念榜缺失时尤其重要），单独成表并标注来源。
    tdx_boards = list(leadership.get("tdx_concepts") or []) + list(leadership.get("tdx_industries") or [])
    if tdx_boards:
        lines.extend(
            ["", "通达信板块行情：", "", "| 类型 | 板块 | 涨跌幅 | 上涨/下跌 | 成交额(亿元) |", "| --- | --- | --- | --- | --- |"]
        )
        for item in tdx_boards:
            kind_label = "概念" if item.kind == "concept" else "行业"
            lines.append(
                f"| {kind_label} | {item.name} | {_fmt_percent(item.change_percent)} | "
                f"{_fmt(item.up_count)}/{_fmt(item.down_count)} | "
                f"{_fmt(item.amount / CNY_YI if item.amount else None)} |"
            )
    return "\n".join(lines)


def _render_candidates(reading: DecisionReading) -> str:
    candidates = reading.candidates
    lines = ["## 五、候选对象", ""]

    ladder = candidates.get("board_ladder") or []
    lines.append("连板梯队（板数从高到低）：")
    lines.append("")
    if ladder:
        lines.append("| 连板 | 代码 | 名称 | 所属行业 | 当日涨幅 | 封板资金(万元) | 炸板次数 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for stock in ladder:
            lines.append(
                f"| {_fmt(stock.get('board_count'))} | {stock.get('code')} | {stock.get('name')} | "
                f"{_fmt_text(stock.get('sector'))} | {_fmt_percent(stock.get('change_percent'))} | "
                f"{_fmt(stock.get('seal_amount') / 1e4 if stock.get('seal_amount') else None)} | "
                f"{_fmt(stock.get('broken_times'))} |"
            )
    else:
        lines.append("- 当日无 2 板及以上连板记录。")

    leaders = candidates.get("sector_leaders") or []
    if leaders:
        lines.extend(["", "板块领涨股：", "", "| 名称 | 代码 | 所属板块 | 当日涨幅 | 角色来源 |", "| --- | --- | --- | --- | --- |"])
        for stock in leaders:
            lines.append(
                f"| {stock.get('name')} | {stock.get('code')} | {_fmt_text(stock.get('sector'))} | "
                f"{_fmt_percent(stock.get('change_percent'))} | {_fmt_text(stock.get('role_source'))} |"
            )

    broken = candidates.get("broken_board_watch") or []
    if broken:
        lines.extend(["", f"炸板观察（{len(broken)} 只，次日反包候选）：", ""])
        lines.append("、".join(f"{stock.get('code')} {stock.get('name')}" for stock in broken))

    themes = candidates.get("themes") or []
    if themes:
        lines.extend(["", "涨停逻辑（通达信）：", "", "| 代码 | 名称 | 涨停主题 | 原因揭秘 | 首封时间 |", "| --- | --- | --- | --- | --- |"])
        for row in themes:
            lines.append(
                f"| {row.get('code')} | {row.get('name')} | {_fmt_text(row.get('theme'))} | "
                f"{_fmt_text(row.get('reason'))} | {_fmt_text(row.get('first_limit_time'))} |"
            )

    lines.extend(["", "说明："])
    lines.extend(f"- {item}" for item in candidates.get("notes") or [])
    return "\n".join(lines)


# 消息面只陈列通达信资讯标题与摘要，作为情绪与主线的旁证，不据此生成买卖结论。
def _render_news(inputs: DecisionInputs) -> str:
    news = list(inputs.tdx.news) if inputs.tdx else []
    lines = ["## 六、消息面（通达信）", ""]
    if not news:
        lines.append("- 本次未接入通达信资讯，消息面缺口。")
        return "\n".join(lines)
    for item in news:
        meta = "，".join(part for part in (item.source, item.published_at) if part)
        lines.append(f"- **{item.title}**" + (f"（{meta}）" if meta else ""))
        if item.summary:
            lines.append(f"  - {item.summary}")
    return "\n".join(lines)


# 次日观察与失效条件全部由当日事实推导，用于次日开盘前快速核对，不写成操作指令。
def _render_next_day_plan(inputs: DecisionInputs, reading: DecisionReading) -> str:
    detail = reading.sentiment_stage.get("detail") or {}
    market_detail = reading.market_stage.get("detail") or {}
    lines = ["## 七、次日观察点与失效条件", "", "观察点："]
    observations: list[str] = []
    if detail.get("highest_board") is not None:
        observations.append(f"最高连板能否维持 {detail['highest_board']} 板及以上。")
    if detail.get("limit_up_count") is not None:
        observations.append(f"涨停家数能否维持 {detail['limit_up_count']} 家以上。")
    if detail.get("up_count") is not None and detail.get("down_count") is not None:
        observations.append(
            f"全市场上涨家数能否继续多于下跌家数（当日 {detail['up_count']} / {detail['down_count']}）。"
        )
    if market_detail.get("ma5") is not None:
        observations.append(f"上证指数能否站稳 MA5（{market_detail['ma5']}）。")
    if not observations:
        observations.append("关键情绪与指数事实缺失，次日观察点无法生成。")
    lines.extend(f"- {item}" for item in observations)

    lines.extend(["", "失效条件："])
    invalidations: list[str] = []
    if detail.get("highest_board") is not None:
        invalidations.append(
            f"最高连板跌破 {detail['highest_board']} 板且涨停家数萎缩 20% 以上 → 按退潮处理，转为不参与。"
        )
    if market_detail.get("ma10") is not None:
        invalidations.append(
            f"上证指数跌破 MA10（{market_detail['ma10']}）且量能萎缩 → 按下降期处理，仓位减半或清仓。"
        )
    if detail.get("broken_board_rate") is not None:
        invalidations.append(
            f"炸板率显著高于当日 {detail['broken_board_rate'] * 100:.1f}% → 封板质量转差，候选降级为观察。"
        )
    if not invalidations:
        invalidations.append("关键事实缺失，无法生成失效条件。")
    lines.extend(f"- {item}" for item in invalidations)
    return "\n".join(lines)


def _render_sources(inputs: DecisionInputs) -> str:
    lines = ["## 八、数据来源与缺口", "", "来源："]
    if inputs.sources:
        lines.extend(f"- {key}：`{value}`" for key, value in sorted(inputs.sources.items()))
    else:
        lines.append("- 无可用来源。")
    lines.extend(["", "缺口："])
    if inputs.gaps:
        lines.extend(f"- {item}" for item in inputs.gaps)
    else:
        lines.append("- 本次运行未记录缺口。")
    return "\n".join(lines)


def _join_brief(items: Any) -> str:
    values = [str(item).rstrip("。；") for item in (items or []) if item]
    return "；".join(values[:2]) if values else "无"


def _join_sentences(items: Any) -> str:
    values = [str(item).rstrip("。；") for item in (items or []) if item]
    return "；".join(values)


def _fmt(value: Any) -> str:
    if value is None:
        return "待确认"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _fmt_percent(value: Any) -> str:
    return f"{value:+.2f}%" if isinstance(value, (int, float)) else "待确认"


def _fmt_rate(value: Any) -> str:
    return f"{value * 100:.2f}%" if isinstance(value, (int, float)) else "待确认"


def _fmt_text(value: Any) -> str:
    return str(value) if value else "待确认"
