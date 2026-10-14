# 本文件把池内个股的日线事实换算成跟踪所需的位置事实：均线、区间极值、异动认定、量比。
# 只做确定性计算与客观分层，不输出形态成立结论、不给买卖建议。

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from stock_review.watchlist.watchlist_inputs import (
    ABNORMAL_DEVIATION_3D,
    BENCHMARK_NAMES,
    CORE_MA_WINDOW,
    MID_MA_WINDOW,
    SEVERE_DEVIATION_10D,
    SEVERE_DEVIATION_30D,
    SHORT_MA_WINDOW,
    DailyBar,
    WatchlistInputs,
    WatchlistItem,
)


# 区间极值回看根数：约三个月，覆盖一轮「前期核心」的起涨到见顶。
INTERVAL_LOOKBACK_BARS = 60
# 量比取当日量与前 N 日均量之比；前 N 日均量不含当日。
VOLUME_RATIO_WINDOW = 5

# 位置分层阈值：21 日线是更优的观察位置，但不是唯一位置，故只做客观距离分层与提示。
MA21_AT_PERCENT = 3.0
MA21_NEAR_PERCENT = 8.0

# 当日「放量」与「中阳」的展示口径：仅作客观事实标记，不构成形态判定。
VOLUME_SURGE_RATIO = 1.5
MID_BULLISH_CHANGE_PERCENT = 5.0

ZONE_FAR = "远离均线"
ZONE_NEAR = "接近均线"
ZONE_AT = "到线"
ZONE_BELOW = "跌破均线"
ZONE_UNKNOWN = "不可判定"

REGULATION_NONE = "未达认定"
REGULATION_ABNORMAL = "异常波动"
REGULATION_SEVERE = "严重异常波动"

# 顺序即严重程度，聚合同日多档位时取更重的一档。
REGULATION_ORDER = (REGULATION_NONE, REGULATION_ABNORMAL, REGULATION_SEVERE)


@dataclass(frozen=True)
class WatchlistFact:
    """单只跟踪对象的全部事实；任何一项数据缺失时保留 None 并写入 missing。"""

    code: str
    name: str
    board: str
    benchmark: str
    benchmark_name: str
    trade_date: str
    close: float | None
    change_percent: float | None
    interval_low: float | None
    interval_low_date: str | None
    interval_high: float | None
    interval_high_date: str | None
    interval_gain_percent: float | None
    days_since_high: int | None
    close_vs_high_percent: float | None
    ma5: float | None
    ma10: float | None
    ma21: float | None
    close_vs_ma21_percent: float | None
    volume_ratio_5d: float | None
    volume_surge: bool
    close_strength: float | None
    deviation_3d: float | None
    deviation_10d: float | None
    deviation_30d: float | None
    regulation_level: str
    regulation_events: tuple[dict[str, Any], ...] = ()
    position_zone: str = ZONE_UNKNOWN
    longhubang: dict[str, Any] = field(default_factory=dict)
    fund_flow: dict[str, Any] = field(default_factory=dict)
    missing: tuple[str, ...] = ()


def build_facts(inputs: WatchlistInputs) -> tuple[WatchlistFact, ...]:
    facts: list[WatchlistFact] = []
    for item in inputs.items:
        if item.status != "active":
            continue
        facts.append(
            build_fact(
                item,
                inputs.daily_bars.get(item.code) or (),
                inputs.index_bars.get(item.benchmark) or (),
                inputs.trade_date,
                inputs.longhubang.get(item.code) or {},
                inputs.fund_flow.get(item.code) or {},
            )
        )
    return tuple(facts)


def build_fact(
    item: WatchlistItem,
    bars: tuple[DailyBar, ...],
    index_bars: tuple[DailyBar, ...],
    trade_date: str,
    longhubang: dict[str, Any],
    fund_flow: dict[str, Any],
) -> WatchlistFact:
    missing: list[str] = []
    ordered = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    closes = [bar.close for bar in ordered if bar.close is not None]
    latest = ordered[-1] if ordered else None

    if len(ordered) < CORE_MA_WINDOW:
        missing.append(f"日线不足 {CORE_MA_WINDOW} 根，MA21 未计算")

    close = latest.close if latest else None
    change_percent = None
    if len(closes) >= 2 and closes[-2]:
        change_percent = round((closes[-1] / closes[-2] - 1) * 100, 4)

    interval = interval_extremes(ordered, INTERVAL_LOOKBACK_BARS)
    low, low_date, high, high_date = interval
    interval_gain = None
    if low and high and low > 0:
        interval_gain = round((high / low - 1) * 100, 2)
    days_since_high = trading_days_since(ordered, high_date)
    close_vs_high = round((close / high - 1) * 100, 2) if close and high else None

    ma5 = moving_average(closes, SHORT_MA_WINDOW)
    ma10 = moving_average(closes, MID_MA_WINDOW)
    ma21 = moving_average(closes, CORE_MA_WINDOW)
    close_vs_ma21 = round((close / ma21 - 1) * 100, 2) if close and ma21 else None

    volume_ratio = volume_ratio_5d(ordered)
    close_strength = bar_close_strength(latest)

    if not index_bars:
        missing.append(f"基准指数（{item.benchmark}）日线缺失，偏离值未计算")
    deviation_series = deviation_pairs(ordered, index_bars)
    dev3 = rolling_deviation(deviation_series, 3)
    dev10 = rolling_deviation(deviation_series, 10)
    dev30 = rolling_deviation(deviation_series, 30)
    events = scan_regulation_events(deviation_series, item.board)
    level = REGULATION_NONE
    for event in events:
        if REGULATION_ORDER.index(event["kind"]) > REGULATION_ORDER.index(level):
            level = event["kind"]

    return WatchlistFact(
        code=item.code,
        name=item.name,
        board=item.board,
        benchmark=item.benchmark,
        benchmark_name=BENCHMARK_NAMES.get(item.benchmark, item.benchmark),
        trade_date=trade_date,
        close=close,
        change_percent=change_percent,
        interval_low=low,
        interval_low_date=low_date,
        interval_high=high,
        interval_high_date=high_date,
        interval_gain_percent=interval_gain,
        days_since_high=days_since_high,
        close_vs_high_percent=close_vs_high,
        ma5=ma5,
        ma10=ma10,
        ma21=ma21,
        close_vs_ma21_percent=close_vs_ma21,
        volume_ratio_5d=volume_ratio,
        volume_surge=bool(volume_ratio is not None and volume_ratio >= VOLUME_SURGE_RATIO),
        close_strength=close_strength,
        deviation_3d=dev3,
        deviation_10d=dev10,
        deviation_30d=dev30,
        regulation_level=level,
        regulation_events=tuple(events),
        position_zone=position_zone(close_vs_ma21),
        longhubang=dict(longhubang),
        fund_flow=dict(fund_flow),
        missing=tuple(missing),
    )


def moving_average(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    return round(sum(closes[-window:]) / window, 2)


def interval_extremes(
    bars: tuple[DailyBar, ...], lookback: int
) -> tuple[float | None, str | None, float | None, str | None]:
    window = bars[-lookback:] if lookback and len(bars) > lookback else bars
    valued = [bar for bar in window if bar.low is not None and bar.high is not None]
    if not valued:
        return None, None, None, None
    low_bar = min(valued, key=lambda bar: bar.low)
    high_bar = max(valued, key=lambda bar: bar.high)
    return low_bar.low, low_bar.trade_date, high_bar.high, high_bar.trade_date


def trading_days_since(bars: tuple[DailyBar, ...], target_date: str | None) -> int | None:
    """目标日到序列最末一日的交易日数；目标日不在序列内时返回 None。"""
    if not target_date:
        return None
    dates = [bar.trade_date for bar in bars]
    if target_date not in dates:
        return None
    return len(dates) - 1 - dates.index(target_date)


def bar_close_strength(bar: DailyBar | None) -> float | None:
    """收盘强度 =（收盘 − 最低）/（最高 − 最低），衡量尾盘承接位置。"""
    if bar is None or bar.high is None or bar.low is None or bar.close is None:
        return None
    span = bar.high - bar.low
    if span <= 0:
        return None
    return round((bar.close - bar.low) / span, 4)


def volume_ratio_5d(bars: tuple[DailyBar, ...]) -> float | None:
    if len(bars) < VOLUME_RATIO_WINDOW + 1:
        return None
    previous = [bar.volume for bar in bars[-VOLUME_RATIO_WINDOW - 1 : -1]]
    if any(value is None for value in previous):
        return None
    baseline = sum(value for value in previous if value is not None) / VOLUME_RATIO_WINDOW
    if not baseline:
        return None
    return round(bars[-1].volume / baseline, 4)


# 偏离值按交易所口径：同一交易日「个股涨跌幅 − 基准指数涨跌幅」，逐日累计。
def deviation_pairs(
    stock_bars: tuple[DailyBar, ...], index_bars: tuple[DailyBar, ...]
) -> list[tuple[str, float]]:
    stock_close = {bar.trade_date: bar.close for bar in stock_bars if bar.close is not None}
    index_close = {bar.trade_date: bar.close for bar in index_bars if bar.close is not None}
    dates = sorted(set(stock_close) & set(index_close))
    pairs: list[tuple[str, float]] = []
    for previous, current in zip(dates, dates[1:]):
        stock_previous = stock_close[previous]
        index_previous = index_close[previous]
        if not stock_previous or not index_previous:
            continue
        stock_pct = (stock_close[current] / stock_previous - 1) * 100
        index_pct = (index_close[current] / index_previous - 1) * 100
        pairs.append((current, stock_pct - index_pct))
    return pairs


def rolling_deviation(pairs: list[tuple[str, float]], window: int) -> float | None:
    if len(pairs) < window:
        return None
    return round(sum(value for _, value in pairs[-window:]), 4)


def scan_regulation_events(pairs: list[tuple[str, float]], board: str) -> list[dict[str, Any]]:
    """逐日扫描交易所异动认定：3 日偏离（异常波动）、10/30 日偏离（严重异常波动）。

    同一交易日命中多档时只保留更重的一档，避免同一事实重复计数。
    """
    threshold_3d = ABNORMAL_DEVIATION_3D.get(board, 20.0)
    events: list[dict[str, Any]] = []
    for index in range(len(pairs)):
        date_text, _ = pairs[index]
        hits: list[dict[str, Any]] = []
        if index + 1 >= 3:
            total = sum(value for _, value in pairs[index - 2 : index + 1])
            if total >= threshold_3d:
                hits.append(
                    {"date": date_text, "kind": REGULATION_ABNORMAL, "window": 3, "deviation": round(total, 4), "threshold": threshold_3d}
                )
        if index + 1 >= 10:
            total = sum(value for _, value in pairs[index - 9 : index + 1])
            if total >= SEVERE_DEVIATION_10D:
                hits.append(
                    {"date": date_text, "kind": REGULATION_SEVERE, "window": 10, "deviation": round(total, 4), "threshold": SEVERE_DEVIATION_10D}
                )
        if index + 1 >= 30:
            total = sum(value for _, value in pairs[index - 29 : index + 1])
            if total >= SEVERE_DEVIATION_30D:
                hits.append(
                    {"date": date_text, "kind": REGULATION_SEVERE, "window": 30, "deviation": round(total, 4), "threshold": SEVERE_DEVIATION_30D}
                )
        if hits:
            events.append(max(hits, key=lambda item: (REGULATION_ORDER.index(item["kind"]), item["deviation"])))
    return events


def position_zone(close_vs_ma21_percent: float | None) -> str:
    if close_vs_ma21_percent is None:
        return ZONE_UNKNOWN
    if close_vs_ma21_percent >= MA21_NEAR_PERCENT:
        return ZONE_FAR
    if close_vs_ma21_percent >= MA21_AT_PERCENT:
        return ZONE_NEAR
    if close_vs_ma21_percent > -MA21_AT_PERCENT:
        return ZONE_AT
    return ZONE_BELOW


def is_mid_bullish(fact: WatchlistFact) -> bool:
    """当日同时放量且收中阳（自定展示口径），用于提示「可能不等 21 日线就启动」。"""
    return bool(
        fact.change_percent is not None
        and fact.change_percent >= MID_BULLISH_CHANGE_PERCENT
        and fact.volume_surge
    )


def build_reminders(facts: tuple[WatchlistFact, ...]) -> tuple[str, ...]:
    """生成提醒行：位置提醒（到 21 日线 / 跌破）与放量中阳提醒（可能不等 21 日线）。

    提醒只描述客观位置与当日量价状态，不构成买卖建议。
    """
    reminders: list[str] = []
    for fact in facts:
        if fact.position_zone == ZONE_AT:
            reminders.append(
                f"[到线] {fact.name}（{fact.code}）收盘距 MA21 {_percent(fact.close_vs_ma21_percent)}，已到核心跟踪位置。"
            )
        elif fact.position_zone == ZONE_BELOW:
            reminders.append(
                f"[跌破] {fact.name}（{fact.code}）收盘距 MA21 {_percent(fact.close_vs_ma21_percent)}，已跌破核心跟踪位置。"
            )
        elif fact.position_zone == ZONE_NEAR:
            reminders.append(
                f"[接近] {fact.name}（{fact.code}）收盘距 MA21 {_percent(fact.close_vs_ma21_percent)}，进入观察窗口。"
            )
        if is_mid_bullish(fact):
            reminders.append(
                f"[放量中阳] {fact.name}（{fact.code}）当日 {_percent(fact.change_percent)}，量比 {fact.volume_ratio_5d}×，"
                "形态可能不等 21 日线即启动。"
            )
    return tuple(reminders)


def _percent(value: float | None) -> str:
    return "不可判定" if value is None else f"{value:+.2f}%"


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
