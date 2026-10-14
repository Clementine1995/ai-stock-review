# 本文件把跟踪事实渲染成 Markdown：提醒、位置与量价、异动认定与资金、逐票事实、来源与缺口。
# 只呈现可追溯事实与客观分层，不给买卖建议，也不判定形态成立。

from __future__ import annotations

from typing import Any

from stock_review.watchlist.compute_watchlist_facts import (
    MA21_AT_PERCENT,
    MA21_NEAR_PERCENT,
    WatchlistFact,
    build_reminders,
)
from stock_review.watchlist.watchlist_inputs import WatchlistInputs


def render_watchlist(
    inputs: WatchlistInputs,
    facts: tuple[WatchlistFact, ...],
    *,
    generated_at: str,
) -> str:
    lines: list[str] = []
    lines.append(f"# {inputs.trade_date} 核心异动票跟踪")
    lines.append("")
    lines.append(f"- 生成时间：{generated_at}")
    lines.append(f"- 交易日：{inputs.trade_date}")
    lines.append(f"- 跟踪对象：{len(facts)} 只（在池且启用）")
    lines.append(
        "- 数据来源：通达信同口径的 AKShare 前复权日线（`stock_zh_a_daily` qfq）"
        "＋基准指数日线（`stock_zh_index_daily`）＋龙虎榜（`stock_lhb_detail_em`）"
        "＋资金流向（`stock_fund_flow_individual`）"
    )
    lines.append(
        "- 定位：**长期跟踪**进过异动的核心票，只记录可追溯事实与位置分层，"
        "不构成买卖建议，不判定形态成立。"
    )
    lines.append(
        f"- 观察位置口径：21 日线是**更优**的观察位置，但不是唯一位置——"
        f"距 21 日线 ±{MA21_AT_PERCENT:.0f}% 记为「到线」并提醒，"
        f"±{MA21_NEAR_PERCENT:.0f}% 记为「接近」；形态也可能在未到 21 日线时因放量中阳先启动。"
    )
    lines.append("")

    lines.extend(_render_reminders(facts))
    lines.extend(_render_position_table(facts))
    lines.extend(_render_regulation_table(facts))
    lines.extend(_render_detail(facts))
    lines.extend(_render_sources(inputs))
    return "\n".join(lines).rstrip() + "\n"


def _render_reminders(facts: tuple[WatchlistFact, ...]) -> list[str]:
    lines = ["## 一、提醒", ""]
    reminders = build_reminders(facts)
    if reminders:
        lines.extend(f"- {item}" for item in reminders)
    else:
        lines.append("- 本次无位置或量价提醒。")
    lines.append("")
    return lines


def _render_position_table(facts: tuple[WatchlistFact, ...]) -> list[str]:
    lines = [
        "## 二、位置与量价",
        "",
        "| 代码 | 名称 | 板块 | 收盘 | 当日 | 区间低→高（涨幅） | 距高点 | MA21 | 距 MA21 | 位置 | 量比 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    if not facts:
        lines.append("| — | — | — | — | — | — | — | — | — | — | — |")
    for fact in facts:
        interval = (
            f"{_num(fact.interval_low)} → {_num(fact.interval_high)}"
            f"（{_signed(fact.interval_gain_percent)}）"
            if fact.interval_low is not None and fact.interval_high is not None
            else "—"
        )
        span = f"{fact.interval_low_date} → {fact.interval_high_date}" if fact.interval_low_date else "—"
        lines.append(
            "| {code} | {name} | {board} | {close} | {change} | {interval} | {days} | {ma21} | {vs_ma21} | {zone} | {ratio} |".format(
                code=fact.code,
                name=fact.name,
                board=fact.board,
                close=_num(fact.close),
                change=_signed(fact.change_percent),
                interval=interval,
                days=_days(fact.days_since_high),
                ma21=_num(fact.ma21),
                vs_ma21=_signed(fact.close_vs_ma21_percent),
                zone=fact.position_zone,
                ratio=_ratio(fact.volume_ratio_5d),
            )
        )
        # 区间起止日期单独一行，避免主表过宽；同时标注量能与收盘强度。
        if fact.interval_low_date or fact.close_strength is not None:
            lines.append(
                f"|  |  |  |  |  | 区间：{span} |  |  |  |  | 收盘强度 {_strength(fact.close_strength)} |"
            )
    lines.append("")
    lines.append(
        f"- 「距高点」为区间最高价所在交易日到 {facts[0].trade_date if facts else '当日'} 的交易日数；"
        "「位置」按距 MA21 分层：远离 / 接近 / 到线 / 跌破。"
    )
    lines.append("- 量比为当日成交量与**前 5 个交易日**均量之比；前 5 日均量不含当日。")
    lines.append("")
    return lines


def _render_regulation_table(facts: tuple[WatchlistFact, ...]) -> list[str]:
    lines = [
        "## 三、异动认定与资金",
        "",
        "| 代码 | 名称 | 异动档位 | 最近认定 | 10 日偏离 | 30 日偏离 | 龙虎榜 | 主力净额 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    if not facts:
        lines.append("| — | — | — | — | — | — | — | — |")
    for fact in facts:
        latest = fact.regulation_events[-1] if fact.regulation_events else None
        latest_text = (
            f"{latest['date']}｜{latest['kind']}（{latest['window']} 日 {_signed(latest['deviation'])}）"
            if latest
            else "—"
        )
        lines.append(
            "| {code} | {name} | {level} | {latest} | {d10} | {d30} | {lhb} | {fund} |".format(
                code=fact.code,
                name=fact.name,
                level=fact.regulation_level,
                latest=latest_text,
                d10=_signed(fact.deviation_10d),
                d30=_signed(fact.deviation_30d),
                lhb=_longhubang_text(fact.longhubang),
                fund=_fund_text(fact.fund_flow),
            )
        )
    lines.append("")
    lines.append(
        "- 偏离值＝同交易日「个股涨跌幅 − 基准指数涨跌幅」，逐日累计；"
        "「异常波动」为连续 3 日偏离值累计达板块阈值（主板 20%、双创 30%），"
        "「严重异常波动」为连续 10 日≥100% 或 30 日≥200%。"
    )
    lines.append("- 龙虎榜只统计最近 45 个自然日的公开日榜记录；主力净额为同花顺当日资金流口径。")
    lines.append("")
    return lines


def _render_detail(facts: tuple[WatchlistFact, ...]) -> list[str]:
    lines = ["## 四、逐票事实", ""]
    if not facts:
        lines.append("（跟踪池为空或全部停用。）")
        lines.append("")
        return lines
    for fact in facts:
        lines.append(f"### {fact.code} {fact.name}（{fact.board}｜基准 {fact.benchmark_name}）")
        lines.append("")
        lines.append(f"- 当日：收盘 {_num(fact.close)}（{_signed(fact.change_percent)}）；")
        lines.append(
            f"- 区间：{_num(fact.interval_low)}（{fact.interval_low_date}）→ "
            f"{_num(fact.interval_high)}（{fact.interval_high_date}），幅度 {_signed(fact.interval_gain_percent)}"
        )
        lines.append(
            f"- 位置：距区间高点 {_days(fact.days_since_high)}，收盘较高点 {_signed(fact.close_vs_high_percent)}；"
            f"距 MA21 {_signed(fact.close_vs_ma21_percent)}（{fact.position_zone}）"
        )
        lines.append(
            f"- 均线：MA5 {_num(fact.ma5)}　MA10 {_num(fact.ma10)}　MA21 {_num(fact.ma21)}；"
            f"量比 {_ratio(fact.volume_ratio_5d)}；收盘强度 {_strength(fact.close_strength)}"
        )
        lines.append(
            f"- 偏离值：3 日 {_signed(fact.deviation_3d)}　10 日 {_signed(fact.deviation_10d)}　"
            f"30 日 {_signed(fact.deviation_30d)}；异动档位 **{fact.regulation_level}**"
        )
        events = fact.regulation_events[-3:]
        if events:
            lines.append("- 近期异动认定：")
            lines.extend(
                f"  - {event['date']}：{event['kind']}（{event['window']} 日偏离 {_signed(event['deviation'])}，阈值 {event['threshold']}%）"
                for event in events
            )
        if fact.longhubang.get("listed"):
            lines.append(
                f"- 龙虎榜：近 45 日上榜 {fact.longhubang.get('count')} 次；最近 {fact.longhubang['latest']['date']}"
                f"（{fact.longhubang['latest'].get('reason') or '—'}），净买额 {_amount(fact.longhubang['latest'].get('net_amount'))}"
            )
        else:
            lines.append("- 龙虎榜：近 45 日无公开日榜记录（不代表负面）。")
        if fact.fund_flow:
            lines.append(
                f"- 资金流向：主力净额 {fact.fund_flow.get('net_amount_text') or '—'}"
                f"（流入 {fact.fund_flow.get('inflow_text') or '—'}，流出 {fact.fund_flow.get('outflow_text') or '—'}）"
            )
        else:
            lines.append("- 资金流向：无当日记录。")
        if fact.missing:
            lines.append("- 缺口：" + "；".join(fact.missing))
        lines.append("")
    return lines


def _render_sources(inputs: WatchlistInputs) -> list[str]:
    lines = ["## 五、数据来源与缺口", ""]
    lines.append("来源：")
    lines.append("")
    for key, value in inputs.sources.items():
        lines.append(f"- {key}：`{value}`")
    lines.append("")
    lines.append("缺口与口径说明：")
    lines.append("")
    if inputs.gaps:
        lines.extend(f"- {item}" for item in inputs.gaps)
    else:
        lines.append("- 本次运行未记录缺口。")
    lines.append("")
    lines.append(
        "- 除权除息日前复权价会随最新交易日滚动调整，历史绝对价格可能与当日快照不同。"
    )
    lines.append(
        "- 跟踪池由用户审核后加入；候选清单位于 `data/watchlist/candidates/<日期>.json`。"
    )
    return lines


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _signed(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}×"


def _strength(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _days(value: int | None) -> str:
    return "—" if value is None else f"{value} 个交易日"


def _amount(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    if abs(value) >= 1e8:
        return f"{value / 1e8:+.2f} 亿"
    if abs(value) >= 1e4:
        return f"{value / 1e4:+.2f} 万"
    return f"{value:+.0f}"


def _longhubang_text(record: dict[str, Any]) -> str:
    if not record.get("listed"):
        return "—"
    latest = record.get("latest") or {}
    return f"{record.get('count')} 次（{latest.get('date') or '—'}）"


def _fund_text(record: dict[str, Any]) -> str:
    text = record.get("net_amount_text")
    return str(text) if text else "—"
