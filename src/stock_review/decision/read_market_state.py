# 本文件把当日与近期事实按 stock-review.md 的框架规则转成决策结论。
# 所有判定都是显式阈值 + 有序规则，每条结论必须同时给出 evidence 与 rule，便于人工复核和后续调整阈值。

from __future__ import annotations

from statistics import mean
from typing import Any

from stock_review.decision.decision_inputs import (
    DecisionInputs,
    DecisionReading,
    IndexDay,
    SentimentDay,
    TdxEnrichment,
)

# 量能档位阈值（单位：元），对应 stock-review.md STEP 1.2 的三档量能结论。
AMOUNT_BAND_THRESHOLDS = (1_0000_0000_0000, 2_0000_0000_0000)
AMOUNT_BAND_LABELS = ("1万亿以下", "1万亿–2万亿", "2万亿以上")
# 量能环比变化超过该比例时提示「急剧变化」，对应 STEP 1.2 的重点关注项。
AMOUNT_SURGE_PERCENT = 15.0
AMOUNT_BAND_SUPPORT = {
    "1万亿以下": "量能偏弱，一般只能支撑小盘股与缩量抱团，难以支撑板块级主升。",
    "1万亿–2万亿": "量能中性，一般可支撑一个板块走出主升。",
    "2万亿以上": "量能充沛，一般可同时支撑两到三个板块。",
}

# 指数阶段阈值，对应 STEP 1.1 的主升/震荡/下降三分。
MARKET_UPTREND_RET5_PERCENT = 3.0
MARKET_UPTREND_POSITION_RATIO = 0.8
MARKET_DOWNTREND_RET5_PERCENT = -3.0
# 「收盘强度」= (收盘 - 最低) / (最高 - 最低)，即收盘价在当日振幅中的位置，不是下影线占比。
# 该值高说明尾盘把日内跌幅收回去，是承接强的表现；指数日线描述与止跌迹象共用同一阈值。
CLOSE_STRENGTH_STRONG = 0.6

# 情绪阶段阈值，对应 STEP 3 的冰点/主升/上升/退潮/混沌。
SENTIMENT_ICE_LIMIT_DOWN = 15
SENTIMENT_ICE_MAX_HIGHEST_BOARD = 4
SENTIMENT_MAIN_MIN_HIGHEST_BOARD = 6
SENTIMENT_MAIN_MIN_LIMIT_UP = 70
SENTIMENT_MAIN_MAX_BROKEN_RATE = 0.25
SENTIMENT_RISING_MIN_LIMIT_UP = 50
SENTIMENT_RISING_MAX_BROKEN_RATE = 0.35
SENTIMENT_RISING_MIN_HIGHEST_BOARD = 3
SENTIMENT_FADE_LIMIT_UP_RATIO = 0.8

# 分歧质量（结构）阈值：只用盘后已有事实判断情绪结构是否转弱，对应「连板高度环比、封板/炸板结构」。
# 与 STEP 3 情绪阶段互补——情绪阶段看当日横截面强弱，结构标记看高度与封板质量的环比变化。
DIVERGENCE_BOARD_DROP = 3
DIVERGENCE_BROKEN_RATE_RISE = 0.10

# 止跌迹象阈值：只用盘后已有事实判断「指数当日是否出现低位承接形态」。
# 与指数阶段互补——阶段看趋势方向（均线/箱体），止跌标记看当日 K 线在低位是否被接住。
STABILIZE_MAX_BOX_POSITION = 0.3
STABILIZE_RULE = (
    "下降期或震荡期中，收盘高于开盘、收盘位于当日振幅 ≥ 60% 位置，"
    "且处于 10 日箱体下沿 30% 以内时，标记指数止跌迹象。"
    "止跌迹象只提示短线承接，不代表趋势反转，需次日确认。"
)

# 仓位区间按量能档位给基线，再由参与倾向和指数阶段修正。
POSITION_BASE_BAND = {
    "2万亿以上": (50, 80),
    "1万亿–2万亿": (30, 50),
    "1万亿以下": (10, 30),
}
POSITION_CAUTIOUS_BAND = (10, 20)
POSITION_NONE_BAND = (0, 0)
# 单票上限（占总资金比例）；总仓位区间更低时，实际单票金额以区间为准。
SINGLE_NAME_POSITION_CAP = 30
POSITION_NAME_COUNT = {
    "2万亿以上": "2–3 只",
    "1万亿–2万亿": "2 只",
    "1万亿以下": "1–2 只",
}

PRIMARY_INDEX_SYMBOL = "sh000001"
PRIMARY_INDEX_NAME = "上证指数"

# 候选展示上限：各候选列表最多渲染前 N 条；总数与覆盖率由 candidates["coverage"] 单独承载。
CANDIDATE_DISPLAY_LIMIT = 10


# 决策入口：先算单点事实（量能档、指数阶段、情绪阶段），再合成参与倾向与仓位区间。
def read_decision(inputs: DecisionInputs) -> DecisionReading:
    amount_band = read_amount_band(inputs.total_amount, inputs.amount_history)
    market_stage = read_market_stage(inputs.index_history)
    sentiment_stage = read_sentiment_stage(
        inputs.sentiment_today, inputs.sentiment_history, inputs.breadth
    )
    divergence_quality = read_divergence_quality(inputs.sentiment_today, inputs.sentiment_history)
    participation = read_participation(market_stage, sentiment_stage)
    position_band = read_position_band(amount_band, market_stage, sentiment_stage, participation)
    sector_leadership = read_sector_leadership(inputs.limit_up_stocks, inputs.sectors, inputs.tdx)
    candidates = build_candidates(
        inputs.limit_up_stocks, inputs.broken_board_stocks, inputs.sectors, inputs.tdx
    )
    return DecisionReading(
        market_stage=market_stage,
        amount_band=amount_band,
        sentiment_stage=sentiment_stage,
        divergence_quality=divergence_quality,
        participation=participation,
        position_band=position_band,
        sector_leadership=sector_leadership,
        candidates=candidates,
    )


# 两市成交额先落到三档量能结论，再补充环比变化；成交额缺失时明确不可判定，不按 0 处理。
def read_amount_band(
    total_amount: float | None,
    amount_history: tuple[tuple[str, float | None], ...] = (),
) -> dict[str, Any]:
    if not total_amount or total_amount <= 0:
        return {
            "label": "不可判定",
            "evidence": ["两市 A 股成交额缺失。"],
            "rule": "STEP 1.2 需要两市 A 股成交额才能定位量能档位。",
            "amount_yi": None,
        }
    lower, upper = AMOUNT_BAND_THRESHOLDS
    if total_amount < lower:
        label = AMOUNT_BAND_LABELS[0]
    elif total_amount < upper:
        label = AMOUNT_BAND_LABELS[1]
    else:
        label = AMOUNT_BAND_LABELS[2]
    amount_yi = round(total_amount / 1e8, 1)
    evidence = [f"两市 A 股成交额 {amount_yi} 亿元。"]

    # STEP 1.2 特别关注量能急剧变化，因此对最近两个有值交易日给出环比。
    known = [(day, value) for day, value in amount_history if value]
    if len(known) >= 2:
        previous_value = known[-2][1]
        change_percent = (known[-1][1] / previous_value - 1) * 100 if previous_value else None
        if change_percent is not None:
            evidence.append(
                f"较上一交易日（{known[-2][0]}，{round(previous_value / 1e8, 1)} 亿元）"
                f"环比 {change_percent:+.2f}%。"
            )
            if abs(change_percent) >= AMOUNT_SURGE_PERCENT:
                evidence.append(f"量能环比变化超过 {AMOUNT_SURGE_PERCENT}%，属于急剧变化，需重点核对。")
    return {
        "label": label,
        "evidence": evidence,
        "rule": AMOUNT_BAND_SUPPORT[label],
        "amount_yi": amount_yi,
    }


# 指数阶段以沪指为主，用 5 日涨跌幅与 10 日箱体位置区分主升、下降与震荡。
def read_market_stage(index_history: dict[str, tuple[IndexDay, ...]]) -> dict[str, Any]:
    days = index_history.get(PRIMARY_INDEX_SYMBOL) or ()
    closes = [day.close for day in days]
    if len(closes) < 5:
        return {
            "label": "不可判定",
            "evidence": [f"{PRIMARY_INDEX_NAME}可用交易日不足 5 天。"],
            "rule": "STEP 1.1 需要至少 5 个交易日路径才能判断阶段。",
            "detail": {},
            "stabilization": _read_stabilization(None, (), None, "不可判定"),
        }

    latest = days[-1]
    ret5 = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else None
    window = closes[-10:] if len(closes) >= 10 else closes
    high, low = max(window), min(window)
    position = (closes[-1] - low) / (high - low) if high > low else None
    ma5 = mean(closes[-5:])
    ma10 = mean(window)
    ma5_prev = mean(closes[-6:-1]) if len(closes) >= 6 else ma5

    evidence = [
        f"{PRIMARY_INDEX_NAME}收 {latest.close:.2f}"
        + (f"（5 日 {ret5:+.2f}%）" if ret5 is not None else "（5 日涨跌幅不可判定）"),
        f"近 {len(window)} 日收盘区间 {low:.2f}–{high:.2f}，当前处于箱体 "
        + (f"{position * 100:.0f}% 位置。" if position is not None else "位置不可判定。"),
        f"MA5 {ma5:.2f}，MA10 {ma10:.2f}。",
    ]

    if ret5 is not None and ret5 >= MARKET_UPTREND_RET5_PERCENT and (position or 0) >= MARKET_UPTREND_POSITION_RATIO:
        label = "主升期"
        rule = "5 日涨幅 ≥ 3% 且收盘位于 10 日箱体上沿（≥80%），按主升处理。"
    elif (ret5 is not None and ret5 <= MARKET_DOWNTREND_RET5_PERCENT) or (
        closes[-1] < ma5 and closes[-1] < ma10 and ma5 < ma10
    ):
        label = "下降期"
        rule = "5 日跌幅 ≥ 3%，或收盘同时跌破 MA5/MA10 且 MA5 低于 MA10，按下降处理。"
    else:
        label = "震荡期"
        rule = "未满足主升或下降条件，按震荡处理。"

    detail: dict[str, Any] = {
        "close": latest.close,
        "ret5_percent": round(ret5, 2) if ret5 is not None else None,
        "position_ratio": round(position, 3) if position is not None else None,
        "ma5": round(ma5, 2),
        "ma10": round(ma10, 2),
        "ma5_rising": ma5 > ma5_prev,
        "box_low": round(low, 2),
        "box_high": round(high, 2),
        "low": latest.low,
        "high": latest.high,
    }
    if latest.high and latest.low and latest.high > latest.low:
        close_strength = (latest.close - latest.low) / (latest.high - latest.low)
        detail["close_strength"] = round(close_strength, 3)
        detail["strong_close"] = close_strength >= CLOSE_STRENGTH_STRONG
        evidence.append(f"当日收盘位于振幅 {close_strength * 100:.0f}% 位置（收盘强度）。")

    return {
        "label": label,
        "evidence": evidence,
        "rule": rule,
        "detail": detail,
        "stabilization": _read_stabilization(latest, days[:-1], position, label),
    }


# 止跌迹象只回答「指数当日是否在低位被接住」，不改变指数阶段标签，也不进入参与倾向与仓位判定。
# 它补的是均线判定的盲点：阴跌途中收盘仍在均线下方（阶段照判下降期），但当日已出现低开高走与尾盘承接。
def _read_stabilization(
    latest: IndexDay | None,
    prior_days: tuple[IndexDay, ...],
    position: float | None,
    stage_label: str,
) -> dict[str, Any]:
    if (
        latest is None
        or latest.open is None
        or latest.high is None
        or latest.low is None
        or latest.high <= latest.low
    ):
        return {
            "label": "不可判定",
            "evidence": ["当日 K 线开/高/低缺失或不完整，无法判断止跌形态。"],
            "rule": STABILIZE_RULE,
            "flags": [],
        }

    close_strength = (latest.close - latest.low) / (latest.high - latest.low)
    evidence = [
        f"开盘 {latest.open:.2f}，收盘 {latest.close:.2f}，"
        f"收盘位于振幅 {close_strength * 100:.0f}% 位置。"
    ]
    if position is not None:
        evidence.append(f"处于 10 日箱体 {position * 100:.0f}% 位置。")

    # 逐条记录未满足项，避免只给「没有迹象」而不说清是差在哪一条。
    blockers: list[str] = []
    if stage_label not in ("下降期", "震荡期"):
        blockers.append(f"指数阶段为{stage_label}，不适用止跌判断")
    if latest.close <= latest.open:
        blockers.append("收盘未高于开盘")
    if close_strength < CLOSE_STRENGTH_STRONG:
        blockers.append(f"收盘强度低于 {round(CLOSE_STRENGTH_STRONG * 100)}%")
    if position is None or position > STABILIZE_MAX_BOX_POSITION:
        blockers.append(f"未处于 10 日箱体下沿 {round(STABILIZE_MAX_BOX_POSITION * 100)}% 以内")

    if blockers:
        evidence.append("未满足：" + "、".join(blockers) + "。")
        return {"label": "无止跌迹象", "evidence": evidence, "rule": STABILIZE_RULE, "flags": []}

    flags = [f"低开高走：开盘 {latest.open:.2f} → 收盘 {latest.close:.2f}。"]
    prior_lows = [day.low for day in prior_days if day.low]
    if prior_lows and latest.low < min(prior_lows):
        flags.append(f"盘中最低 {latest.low:.2f} 创近 {len(prior_days) + 1} 日新低后收回。")
    flags.append(
        f"收盘位于当日振幅 {close_strength * 100:.0f}% 位置，"
        f"处于 10 日箱体 {position * 100:.0f}% 低位。"
    )
    return {"label": "止跌迹象", "evidence": evidence, "rule": STABILIZE_RULE, "flags": flags}


# 情绪阶段按有序规则命中：冰点 → 退潮 → 主升 → 上升 → 混沌，先命中者优先。
def read_sentiment_stage(
    today: SentimentDay | None,
    history: tuple[SentimentDay, ...],
    breadth: dict[str, Any] | None,
) -> dict[str, Any]:
    if today is None:
        return {
            "label": "不可判定",
            "evidence": ["当日涨停/跌停/炸板池缺失。"],
            "rule": "STEP 3 需要涨停、跌停、炸板与连板高度事实。",
            "temperature": None,
            "detail": {},
        }

    previous = _previous_sentiment(history, today.trade_date)
    up_count = (breadth or {}).get("up_count")
    down_count = (breadth or {}).get("down_count")
    evidence = [
        f"涨停 {today.limit_up_count} 家，跌停 {today.limit_down_count} 家，"
        f"炸板率 {today.broken_board_rate * 100:.1f}%"
        if today.broken_board_rate is not None
        else f"涨停 {today.limit_up_count} 家，跌停 {today.limit_down_count} 家，炸板率缺失",
        f"最高连板 {today.highest_board if today.highest_board is not None else '缺失'} 板。",
    ]
    if up_count is not None and down_count is not None:
        evidence.append(f"全市场上涨 {up_count} 家，下跌 {down_count} 家。")
    if previous is not None:
        evidence.append(
            f"上一交易日（{previous.trade_date}）涨停 {previous.limit_up_count} 家，"
            f"最高连板 {previous.highest_board if previous.highest_board is not None else '缺失'} 板。"
        )

    broken_rate = today.broken_board_rate
    highest_board = today.highest_board or 0
    breadth_down = up_count is not None and down_count is not None and down_count > up_count

    if (
        today.limit_down_count >= SENTIMENT_ICE_LIMIT_DOWN
        and breadth_down
        and highest_board <= SENTIMENT_ICE_MAX_HIGHEST_BOARD
    ):
        label = "冰点日"
        rule = "跌停 ≥ 15 家、全市场跌多涨少且最高连板 ≤ 4，按冰点处理。"
    elif (
        previous is not None
        and previous.highest_board is not None
        and today.highest_board is not None
        and today.highest_board < previous.highest_board
        and today.limit_up_count < previous.limit_up_count * SENTIMENT_FADE_LIMIT_UP_RATIO
    ):
        label = "退潮期"
        rule = "最高连板下降且涨停家数较前一交易日萎缩 20% 以上，按退潮处理。"
    elif (
        highest_board >= SENTIMENT_MAIN_MIN_HIGHEST_BOARD
        and today.limit_up_count >= SENTIMENT_MAIN_MIN_LIMIT_UP
        and (broken_rate is None or broken_rate <= SENTIMENT_MAIN_MAX_BROKEN_RATE)
    ):
        label = "主升期"
        rule = "最高连板 ≥ 6 板、涨停 ≥ 70 家且炸板率 ≤ 25%，按主升处理。"
    elif (
        today.limit_up_count >= SENTIMENT_RISING_MIN_LIMIT_UP
        and (broken_rate is None or broken_rate <= SENTIMENT_RISING_MAX_BROKEN_RATE)
        and highest_board >= SENTIMENT_RISING_MIN_HIGHEST_BOARD
    ):
        label = "上升期"
        rule = "涨停 ≥ 50 家、炸板率 ≤ 35% 且最高连板 ≥ 3，按上升/修复处理。"
    else:
        label = "混沌期"
        rule = "未满足冰点、退潮、主升或上升条件，按混沌处理。"

    if previous is None:
        evidence.append("缺少前一交易日情绪序列，退潮方向未参与判定。")

    return {
        "label": label,
        "evidence": evidence,
        "rule": rule,
        "temperature": _sentiment_temperature(today, up_count, down_count),
        "detail": {
            "limit_up_count": today.limit_up_count,
            "limit_down_count": today.limit_down_count,
            "broken_board_rate": today.broken_board_rate,
            "highest_board": today.highest_board,
            "up_count": up_count,
            "down_count": down_count,
            "previous_limit_up_count": previous.limit_up_count if previous else None,
            "previous_limit_down_count": previous.limit_down_count if previous else None,
            "previous_broken_board_rate": previous.broken_board_rate if previous else None,
            "previous_highest_board": previous.highest_board if previous else None,
            "previous_trade_date": previous.trade_date if previous else None,
        },
    }


# 分歧质量（结构）只看盘后已有事实：最高连板环比与炸板率环比。
# 它不引入分时、竞价或次日反馈，只给出「结构转弱 / 结构健康 / 不可判定」这一层标记，
# 用于补上情绪阶段阈值（只看当日横截面）发现不了的连板高度断层。
def read_divergence_quality(
    today: SentimentDay | None,
    history: tuple[SentimentDay, ...],
) -> dict[str, Any]:
    if today is None:
        return {
            "label": "不可判定",
            "evidence": ["当日涨停/跌停/炸板/连板事实缺失。"],
            "rule": "分歧质量需要当日与前一日事实才能比较。",
            "flags": [],
        }

    previous = _previous_sentiment(history, today.trade_date)
    if previous is None or previous.highest_board is None:
        return {
            "label": "不可判定",
            "evidence": ["缺少前一交易日的最高连板事实，无法比较连板高度环比。"],
            "rule": "分歧质量需要前一交易日最高连板才能判断高标是否断板。",
            "flags": [],
        }

    flags: list[str] = []
    evidence: list[str] = []

    # 高标断板：最高连板环比骤降，是情绪高度断裂最直接的信号。
    if today.highest_board is not None:
        board_drop = previous.highest_board - today.highest_board
        evidence.append(f"最高连板 {previous.highest_board} → {today.highest_board} 板。")
        if board_drop >= DIVERGENCE_BOARD_DROP:
            flags.append(
                f"高标断板：最高连板 {previous.highest_board} → {today.highest_board} 板（降 {board_drop} 板）。"
            )
    else:
        evidence.append("当日最高连板缺失，未参与结构判断。")

    # 封板质量转差：炸板率环比明显抬升，说明承接转弱。
    if previous.broken_board_rate is not None and today.broken_board_rate is not None:
        rate_delta = today.broken_board_rate - previous.broken_board_rate
        evidence.append(
            f"炸板率 {previous.broken_board_rate * 100:.1f}% → {today.broken_board_rate * 100:.1f}%。"
        )
        if rate_delta >= DIVERGENCE_BROKEN_RATE_RISE:
            flags.append(
                f"封板质量转差：炸板率 {previous.broken_board_rate * 100:.1f}% → "
                f"{today.broken_board_rate * 100:.1f}%（抬升 {rate_delta * 100:.1f} 个百分点）。"
            )

    return {
        "label": "结构转弱" if flags else "结构健康",
        "evidence": evidence,
        "rule": "最高连板环比骤降或炸板率环比抬升时标记结构转弱；两者均未触发时视为结构健康。",
        "flags": flags,
    }


# 参与倾向由指数阶段与情绪阶段共同决定，任一不可判定时降级为待确认。
def read_participation(market_stage: dict[str, Any], sentiment_stage: dict[str, Any]) -> dict[str, Any]:
    market = market_stage.get("label")
    sentiment = sentiment_stage.get("label")
    evidence = [f"指数阶段：{market}。", f"情绪阶段：{sentiment}。"]

    if market == "不可判定" or sentiment == "不可判定":
        return {
            "label": "待确认",
            "evidence": evidence,
            "rule": "指数阶段或情绪阶段不可判定时，不给出参与倾向。",
        }
    # 退潮期先于指数阶段判定：STEP 3 要求退潮期撤离、仓位收敛为零，不因指数主升或下降而改变。
    if sentiment == "退潮期":
        if market == "主升期":
            rule = "指数主升但情绪退潮，先按 STEP 3 撤离，等退潮衰竭再参与。"
        elif market == "下降期":
            rule = "指数下降且情绪退潮，双重防守，不参与。"
        else:
            rule = "STEP 3 退潮期及时撤离，等退潮衰竭。"
        return {"label": "不参与", "evidence": evidence, "rule": rule}
    if market == "主升期":
        return {
            "label": "可参与",
            "evidence": evidence,
            "rule": "STEP 1.1 指数主升期直接做主升板块，回调是买点。",
        }
    if market == "下降期":
        return {
            "label": "谨慎参与",
            "evidence": evidence,
            "rule": "STEP 1.1 下降期总体防守，小仓位试错，做核心抱团。",
        }
    if sentiment == "冰点日":
        return {
            "label": "可参与",
            "evidence": evidence,
            "rule": "STEP 3 冰点日是最重要的参与机会，围绕主线或抗住分歧的个股。",
        }
    if sentiment in ("上升期", "主升期"):
        return {
            "label": "可参与",
            "evidence": evidence,
            "rule": "STEP 3 上升/主升期参与核心，关注涨幅靠前标的。",
        }
    return {
        "label": "谨慎参与",
        "evidence": evidence,
        "rule": "STEP 3 混沌期无明确主线，小仓位试错。",
    }


# 仓位区间 = 量能基线 × 参与倾向 × 指数阶段修正；输出为区间建议而非执行指令。
def read_position_band(
    amount_band: dict[str, Any],
    market_stage: dict[str, Any],
    sentiment_stage: dict[str, Any],
    participation: dict[str, Any],
) -> dict[str, Any]:
    amount_label = amount_band.get("label")
    participation_label = participation.get("label")
    notes: list[str] = []

    # 退潮期兜底与参与倾向的「不参与」保持一致，防止未来改动破坏「退潮期不建新仓」这一不变量。
    if participation_label == "不参与" or sentiment_stage.get("label") == "退潮期":
        return {
            "total_range": POSITION_NONE_BAND,
            "single_name_cap": 0,
            "max_names": "0 只",
            "evidence": ["参与倾向为不参与或情绪处于退潮期。"],
            "rule": "STEP 3 退潮期以撤离为主，不建议建立新仓。",
            "notes": ["本区间为框架推导建议，不是执行指令，需由用户最终确认。"],
        }
    if participation_label == "待确认" or amount_label not in POSITION_BASE_BAND:
        return {
            "total_range": POSITION_NONE_BAND,
            "single_name_cap": 0,
            "max_names": "待确认",
            "evidence": ["关键阶段不可判定。"],
            "rule": "阶段不可判定时不给仓位建议。",
            "notes": ["补齐指数阶段与情绪阶段后再评估仓位。"],
        }

    low, high = POSITION_BASE_BAND[amount_label]
    notes.append(f"基线 {low}%–{high}% 来自量能档位「{amount_label}」。")
    if participation_label == "谨慎参与":
        low, high = POSITION_CAUTIOUS_BAND
        notes.append("参与倾向为谨慎参与，仓位压到 10%–20%。")
    if market_stage.get("label") == "下降期":
        low, high = round(low * 0.5), round(high * 0.5)
        notes.append("指数下降期按防守处理，仓位减半。")
    if sentiment_stage.get("label") == "冰点日":
        notes.append("冰点日可分批建仓，留足后续加仓空间。")

    notes.append("单票上限为总资金口径的硬性上限，实际单票金额以总仓位区间为准。")
    return {
        "total_range": (low, high),
        "single_name_cap": SINGLE_NAME_POSITION_CAP,
        "max_names": POSITION_NAME_COUNT.get(amount_label, "待确认"),
        "evidence": [*amount_band.get("evidence", []), *participation.get("evidence", [])],
        "rule": "仓位区间由量能档位、指数阶段与情绪阶段共同推导。",
        "notes": [*notes, "本区间为框架推导建议，不是执行指令，需由用户最终确认。"],
    }


# 板块主线以「板块内涨停家数」为主证据，辅以板块涨幅榜前列，避免只看涨幅被单一异动板块误导。
def read_sector_leadership(
    limit_up_stocks: tuple[dict[str, Any], ...],
    sectors: tuple[dict[str, Any], ...],
    tdx: TdxEnrichment | None = None,
) -> dict[str, Any]:
    industry_counts: dict[str, int] = {}
    for stock in limit_up_stocks:
        sector = stock.get("sector")
        if sector:
            industry_counts[sector] = industry_counts.get(sector, 0) + 1
    top_industries = sorted(industry_counts.items(), key=lambda item: (-item[1], item[0]))[:6]

    concepts = [row for row in sectors if "concept" in str(row.get("source_type") or "")][:5]
    industries = [row for row in sectors if "industry" in str(row.get("source_type") or "")][:5]

    # 通达信板块行情是概念维度的补充证据：akshare 概念榜被环境代理阻断时，用它替代而不是留空。
    tdx_concepts = [item for item in (tdx.board_quotes if tdx else ()) if item.kind == "concept"]
    tdx_industries = [item for item in (tdx.board_quotes if tdx else ()) if item.kind == "industry"]

    evidence: list[str] = []
    if top_industries:
        evidence.append(
            "涨停家数最多行业："
            + "、".join(f"{name} {count} 家" for name, count in top_industries[:5])
            + "。"
        )
    if concepts:
        evidence.append(
            "概念板块涨幅前列："
            + "、".join(f"{row.get('name')} {_fmt_percent(row.get('change_percent'))}" for row in concepts[:5])
            + "。"
        )
    if industries:
        evidence.append(
            "行业板块涨幅前列："
            + "、".join(f"{row.get('name')} {_fmt_percent(row.get('change_percent'))}" for row in industries[:5])
            + "。"
        )
    if tdx_concepts:
        evidence.append(
            "通达信概念板块："
            + "、".join(f"{item.name} {_fmt_percent(item.change_percent)}" for item in tdx_concepts[:5])
            + "。"
        )
    if not evidence:
        evidence.append("板块证据缺失，无法识别主线。")

    main_line = top_industries[0][0] if top_industries else None
    return {
        "label": main_line or "不可判定",
        "evidence": evidence,
        "rule": "STEP 4 以近期反复活跃、涨幅榜靠前的板块为核心方向；涨停聚集度作为主证据。",
        "top_industries": top_industries,
        "concepts": concepts,
        "industries": industries,
        "tdx_concepts": tdx_concepts,
        "tdx_industries": tdx_industries,
    }


# 候选只做事实归类：连板梯队、首板强势、炸板观察、板块领涨股，均带来源，不生成买入建议。
def build_candidates(
    limit_up_stocks: tuple[dict[str, Any], ...],
    broken_board_stocks: tuple[dict[str, Any], ...],
    sectors: tuple[dict[str, Any], ...],
    tdx: TdxEnrichment | None = None,
) -> dict[str, Any]:
    themes = tdx.stock_themes if tdx else {}

    ladder = sorted(
        (stock for stock in limit_up_stocks if (stock.get("board_count") or 0) >= 2),
        key=lambda stock: (-(stock.get("board_count") or 0), stock.get("code") or ""),
    )
    first_boards = sorted(
        (stock for stock in limit_up_stocks if (stock.get("board_count") or 0) <= 1),
        key=lambda stock: -(stock.get("seal_amount") or 0),
    )

    # 名称到代码优先用涨停池事实，缺失时用通达信 lookup 补全，仍无法确认才标记待确认。
    code_by_name = {stock.get("name"): stock.get("code") for stock in limit_up_stocks}
    lookup = tdx.code_lookup if tdx else {}
    sector_leaders: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for sector in sectors:
        name = sector.get("leading_stock")
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        resolved = code_by_name.get(name)
        source = f"akshare:{sector.get('source_type')} 板块领涨股"
        if not resolved and name in lookup:
            resolved = lookup[name].get("code")
            source = f"{source} + 通达信代码解析"
        sector_leaders.append(
            {
                "name": name,
                "code": resolved or "待确认",
                "sector": sector.get("name"),
                "change_percent": sector.get("leading_stock_change_percent"),
                "role": "板块领涨事实候选",
                "role_source": source,
            }
        )

    theme_rows = [
        {
            "code": stock.get("code"),
            "name": stock.get("name"),
            "theme": themes[stock.get("code")].theme,
            "reason": themes[stock.get("code")].reason,
            "first_limit_time": themes[stock.get("code")].first_limit_time,
            "seal_amount": themes[stock.get("code")].seal_amount,
        }
        for stock in ladder
        if stock.get("code") in themes
    ]

    # 展示条数受限（各列表最多前 CANDIDATE_DISPLAY_LIMIT 条）与涨停逻辑覆盖率一并输出，
    # 供渲染层显式标注，避免读者把「截断 / 未采」误读为「没有」。
    coverage = {
        "board_ladder_total": len(ladder),
        "strong_first_board_total": len(first_boards),
        "broken_board_total": len(broken_board_stocks),
        "sector_leaders_total": len(sector_leaders),
        "themes_covered": len(theme_rows),
        "themes_total": len(ladder),
    }
    return {
        "board_ladder": list(ladder[:CANDIDATE_DISPLAY_LIMIT]),
        "strong_first_board": list(first_boards[:CANDIDATE_DISPLAY_LIMIT]),
        "broken_board_watch": list(broken_board_stocks[:CANDIDATE_DISPLAY_LIMIT]),
        "sector_leaders": sector_leaders[:CANDIDATE_DISPLAY_LIMIT],
        "themes": theme_rows,
        "coverage": coverage,
        "themes_available": bool(themes),
        "notes": [
            "候选来自涨停池、炸板池与板块领涨股字段，只作为可追溯事实，不构成买入建议。",
            "板块领涨股若未在当日涨停池中出现，代码优先用通达信解析，仍无法确认才标记为待确认。",
        ],
    }


def _previous_sentiment(history: tuple[SentimentDay, ...], trade_date: str) -> SentimentDay | None:
    earlier = [day for day in history if day.trade_date < trade_date]
    return max(earlier, key=lambda day: day.trade_date) if earlier else None


# 情绪温度为 0–100 的参考指标，只用于横向比较当日强弱，不作为框架事实。
def _sentiment_temperature(today: SentimentDay, up_count: int | None, down_count: int | None) -> float:
    score = 0.0
    score += min(today.limit_up_count, 100) * 0.4
    score += min(today.highest_board or 0, 10) * 2.0
    if today.broken_board_rate is not None:
        score += max(0.0, 1 - today.broken_board_rate / 0.6) * 20
    score -= min(today.limit_down_count, 30) * 0.5
    if up_count and down_count and (up_count + down_count) > 0:
        score += ((up_count / (up_count + down_count)) - 0.5) * 40
    return round(max(0.0, min(score, 100.0)), 1)


def _fmt_percent(value: Any) -> str:
    return f"{value:+.2f}%" if isinstance(value, (int, float)) else "涨幅待确认"
