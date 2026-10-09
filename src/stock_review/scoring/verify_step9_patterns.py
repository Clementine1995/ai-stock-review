# 本文件负责按当前 stock-review.md 的 STEP 9 模式清单核验已有证据与硬缺口，不输出买卖建议。

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
import re

from stock_review.evidence.evidence_snapshot import EvidenceSnapshot


@dataclass(frozen=True)
class Step9PatternDefinition:
    pattern_number: int
    name: str
    background_evidence: tuple[str, ...]
    trigger_evidence: tuple[str, ...]
    invalidation_boundary: str


@dataclass(frozen=True)
class Step9PatternCheck:
    pattern_number: int
    name: str
    status: str
    available_evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    candidate_codes: tuple[str, ...]
    invalidation_boundary: str


# 当前 12 项只对应现行 STEP 9 文本；框架语义变化后必须随 M10.6 一并复核。
STEP9_PATTERN_DEFINITIONS = (
    Step9PatternDefinition(1, "题材上升初期和发酵阶段的弹性标", ("sectors", "stocks"), ("daily_kline", "call_auction", "intraday"), "题材未延续或个股缺少弹性事实时失效。"),
    Step9PatternDefinition(2, "退潮末期弱转强或大分歧后超预期高开", ("sentiment", "stocks"), ("previous_day_feedback", "call_auction", "intraday"), "未出现弱转强或高开反馈时失效。"),
    Step9PatternDefinition(3, "轮动期间主线长上影线", ("sectors",), ("daily_kline", "intraday"), "上影线不大于下影线或板块不属于轮动主线时失效。"),
    Step9PatternDefinition(4, "核心盘中被动炸板回封", ("stocks", "sectors"), ("intraday", "previous_day_feedback"), "无法证明被动炸板、回封或板块联动时失效。"),
    Step9PatternDefinition(5, "大单一字板块中的近期图形较好 20cm", ("sectors", "stocks"), ("daily_kline", "call_auction", "intraday"), "缺少一字板块、20cm 标识或近期图形事实时失效。"),
    Step9PatternDefinition(6, "情绪低迷时十点前抗跌的容量核心", ("sentiment", "candidate_history"), ("intraday",), "情绪不低迷或容量对象无法证明盘中抗跌时失效。"),
    Step9PatternDefinition(7, "容量票启动后炸板回封及长上影尾盘", ("candidate_history",), ("daily_kline", "intraday", "next_day_feedback"), "无法证明启动、回封、非跌停深水或次日反馈时失效。"),
    Step9PatternDefinition(8, "指数午盘前逼空后的核心或后排共振", ("market", "sectors", "stocks"), ("intraday",), "指数未逼空、板块未共振或对象非核心事实时失效。"),
    Step9PatternDefinition(9, "前期核心 21 日线低吸", ("stocks",), ("daily_kline", "regulatory_event"), "缺少前期核心、21 日线或异动监管事实时失效。"),
    Step9PatternDefinition(10, "确定核心后的开盘下杀承接", ("stocks",), ("call_auction", "intraday"), "无法证明核心身份、开盘下杀或承接时失效。"),
    Step9PatternDefinition(11, "大涨次日前排切换", ("sectors", "stocks"), ("call_auction", "intraday"), "板块开盘强度或前后排分化事实不满足时失效。"),
    Step9PatternDefinition(12, "情绪指数双低迷尾盘炸板", ("market", "sentiment", "stocks"), ("intraday",), "未同时满足情绪和指数低迷、尾盘炸板或 3-4 板事实时失效。"),
)


# 这些模式的盘后背景可以由板块绑定、候选区间表现或弱情绪事实核验；竞价和分时仍保留为次日缺口。
# 模式 7 的容量、区间收益和完整日 K 只能界定基础候选，不能证明启动、炸板回封或长上影背景。
DIRECT_BACKGROUND_PATTERN_NUMBERS = frozenset({1, 2, 5, 6, 11, 12})


# 快照只提供部分盘后事实；竞价、分时、前日反馈和日线形态未接入时必须逐项保留为硬缺口。
def verify_step9_patterns(
    snapshot: EvidenceSnapshot,
    candidate_history_records: tuple[dict[str, object], ...] = (),
    candidate_codes: tuple[str, ...] = (),
) -> tuple[Step9PatternCheck, ...]:
    available_evidence = collect_available_evidence(snapshot, candidate_history_records)
    normalized_candidate_codes = normalize_candidate_codes(
        snapshot,
        candidate_history_records,
        candidate_codes,
    )
    return tuple(
        build_pattern_check(
            definition,
            available_evidence,
            snapshot,
            candidate_history_records,
            normalized_candidate_codes,
        )
        for definition in STEP9_PATTERN_DEFINITIONS
    )


def collect_available_evidence(
    snapshot: EvidenceSnapshot,
    candidate_history_records: tuple[dict[str, object], ...] = (),
) -> set[str]:
    available: set[str] = set()
    if snapshot.market.get("indices"):
        available.add("market")
    if snapshot.sentiment:
        available.add("sentiment")
    if snapshot.sectors:
        available.add("sectors")
    if snapshot.stocks:
        available.add("stocks")
    if any(
        candidate_has_capacity_history(record)
        for record in candidate_history_records
    ):
        available.add("candidate_history")
    return available


def build_pattern_check(
    definition: Step9PatternDefinition,
    available_evidence: set[str],
    snapshot: EvidenceSnapshot,
    candidate_history_records: tuple[dict[str, object], ...],
    candidate_codes: tuple[str, ...],
) -> Step9PatternCheck:
    bound_candidate_codes = bind_pattern_candidate_codes(
        definition,
        snapshot,
        candidate_history_records,
        candidate_codes,
    )
    pattern_available_evidence = set(available_evidence)
    if "candidate_history" in definition.background_evidence and not bound_candidate_codes:
        pattern_available_evidence.discard("candidate_history")
    if (
        "daily_kline" in definition.trigger_evidence
        and bound_candidate_codes
        and all(
            has_complete_daily_bars(record, snapshot.trade_date)
            for record in candidate_history_records
            if str(record.get("code") or "") in bound_candidate_codes
        )
        and all(
            any(str(record.get("code") or "") == code for record in candidate_history_records)
            for code in bound_candidate_codes
        )
    ):
        pattern_available_evidence.add("daily_kline")
    required_evidence = definition.background_evidence + definition.trigger_evidence
    available = tuple(
        field_name for field_name in required_evidence if field_name in pattern_available_evidence
    )
    missing = tuple(
        field_name for field_name in required_evidence if field_name not in pattern_available_evidence
    )
    missing_background = any(
        field_name not in pattern_available_evidence for field_name in definition.background_evidence
    )
    if any(
        field_name in definition.background_evidence
        for field_name in ("stocks", "candidate_history")
    ) and not bound_candidate_codes:
        missing_background = True
    missing_trigger = any(
        field_name not in pattern_available_evidence for field_name in definition.trigger_evidence
    )
    if missing_background:
        status = "unavailable"
    # 通用模式没有足够规则核验具体背景，只能保留“基础证据可用、触发待核验”的诚实状态。
    elif definition.pattern_number not in DIRECT_BACKGROUND_PATTERN_NUMBERS:
        status = "base_evidence_available_trigger_pending"
    elif not matches_background(
        definition.pattern_number,
        snapshot,
        candidate_history_records,
        bound_candidate_codes,
    ):
        status = "not_matched"
    elif missing_trigger:
        status = "background_matched_trigger_pending"
    else:
        status = "confirmed"
    return Step9PatternCheck(
        pattern_number=definition.pattern_number,
        name=definition.name,
        status=status,
        available_evidence=available,
        missing_evidence=missing,
        candidate_codes=bound_candidate_codes,
        invalidation_boundary=definition.invalidation_boundary,
    )


# 背景只用盘后已有事实；日 K、竞价和分时触发不在这里被提前确认。
def matches_background(
    pattern_number: int,
    snapshot: EvidenceSnapshot,
    candidate_history_records: tuple[dict[str, object], ...] = (),
    candidate_codes: tuple[str, ...] = (),
) -> bool:
    if pattern_number in {1, 5, 11}:
        if not any(
            candidate_has_sector_background(snapshot, code) for code in candidate_codes
        ):
            return False
        if pattern_number == 5:
            return any(code.startswith(("300", "301", "688")) for code in candidate_codes)
        if pattern_number == 11:
            return any(candidate_has_positive_current_fact(snapshot, code) for code in candidate_codes)
        return True
    if pattern_number in {6, 7}:
        return any(
            str(record.get("code") or "") in candidate_codes
            and candidate_matches_history_background(pattern_number, record)
            for record in candidate_history_records
        ) and matches_weak_sentiment_background(pattern_number, snapshot)
    if not matches_weak_sentiment_background(pattern_number, snapshot):
        return False
    if pattern_number != 12:
        return True
    indices = snapshot.market.get("indices")
    return isinstance(indices, list) and any(
        isinstance(index, dict)
        and isinstance(index.get("change_percent"), (int, float))
        and index["change_percent"] < 0
        for index in indices
    )


def normalize_candidate_codes(
    snapshot: EvidenceSnapshot,
    candidate_history_records: tuple[dict[str, object], ...],
    candidate_codes: tuple[str, ...],
) -> tuple[str, ...]:
    known_codes = {
        str(stock.get("code") or "")
        for stock in snapshot.stocks
        if str(stock.get("code") or "").isdigit()
        and len(str(stock.get("code") or "")) == 6
    } | {
        str(record.get("code") or "")
        for record in candidate_history_records
        if str(record.get("code") or "").isdigit()
        and len(str(record.get("code") or "")) == 6
    }
    requested_codes = tuple(dict.fromkeys(candidate_codes))
    if requested_codes:
        return tuple(code for code in requested_codes if code in known_codes)[:3]
    return tuple(sorted(known_codes))[:3]


def bind_pattern_candidate_codes(
    definition: Step9PatternDefinition,
    snapshot: EvidenceSnapshot,
    candidate_history_records: tuple[dict[str, object], ...],
    candidate_codes: tuple[str, ...],
) -> tuple[str, ...]:
    if not any(
        field_name in definition.background_evidence
        for field_name in ("stocks", "candidate_history")
    ):
        return ()
    bound_codes = candidate_codes
    if "candidate_history" in definition.background_evidence:
        bound_codes = tuple(
            code
            for code in bound_codes
            if any(
                str(record.get("code") or "") == code
                and candidate_matches_history_background(
                    definition.pattern_number,
                    record,
                )
                for record in candidate_history_records
            )
        )
    if "daily_kline" in definition.trigger_evidence:
        complete_codes = tuple(
            code
            for code in bound_codes
            if any(
                str(record.get("code") or "") == code
                and has_complete_daily_bars(record, snapshot.trade_date)
                for record in candidate_history_records
            )
        )
        if complete_codes:
            return complete_codes
    return bound_codes


def has_complete_daily_bars(record: dict[str, object], trade_date: str) -> bool:
    bars = record.get("daily_bars")
    recent_bars = bars[-21:] if isinstance(bars, list) else []
    bar_dates = tuple(str(bar.get("date") or "") for bar in recent_bars if isinstance(bar, dict))
    return bool(
        record.get("daily_bars_complete") is True
        and record.get("adjustment") == "none"
        and isinstance(record.get("daily_bar_count"), int)
        and int(record["daily_bar_count"]) >= 21
        and isinstance(bars, list)
        and len(bars) >= 21
        and len(bar_dates) == 21
        and len(set(bar_dates)) == 21
        and tuple(sorted(bar_dates)) == bar_dates
        and bar_dates[-1] == trade_date
        and all(has_complete_daily_bar(bar) for bar in recent_bars)
    )


def has_complete_daily_bar(value: object) -> bool:
    if not isinstance(value, dict) or not value.get("date"):
        return False
    try:
        date.fromisoformat(str(value["date"]))
    except (TypeError, ValueError):
        return False
    numeric_fields = ("open", "high", "low", "close", "volume", "amount", "turnover")
    if not all(
        isinstance(value.get(field_name), (int, float))
        and not isinstance(value.get(field_name), bool)
        and math.isfinite(float(value[field_name]))
        for field_name in numeric_fields
    ):
        return False
    open_price = float(value["open"])
    high_price = float(value["high"])
    low_price = float(value["low"])
    close_price = float(value["close"])
    return bool(
        min(open_price, close_price) >= low_price > 0
        and max(open_price, close_price) <= high_price
        and float(value["volume"]) >= 0
        and float(value["amount"]) >= 0
        and float(value["turnover"]) >= 0
    )


def candidate_has_capacity_history(record: dict[str, object]) -> bool:
    return bool(
        isinstance(record.get("average_5d_amount"), (int, float))
        and not isinstance(record.get("average_5d_amount"), bool)
        and isinstance(record.get("circulating_market_value"), (int, float))
        and not isinstance(record.get("circulating_market_value"), bool)
    )


def candidate_matches_history_background(
    pattern_number: int,
    record: dict[str, object],
) -> bool:
    if not candidate_has_capacity_history(record):
        return False
    if pattern_number != 7:
        return True
    return bool(
        isinstance(record.get("return_5d_percent"), (int, float))
        and not isinstance(record.get("return_5d_percent"), bool)
        and float(record["return_5d_percent"]) > 0
    )


def matches_weak_sentiment_background(
    pattern_number: int,
    snapshot: EvidenceSnapshot,
) -> bool:
    if pattern_number == 7:
        return True
    limit_up = snapshot.sentiment.get("limit_up_count")
    limit_down = snapshot.sentiment.get("limit_down_count")
    return bool(
        isinstance(limit_up, (int, float))
        and isinstance(limit_down, (int, float))
        and limit_down > limit_up
    )


def candidate_has_sector_background(snapshot: EvidenceSnapshot, code: str) -> bool:
    coded_stock = next(
        (stock for stock in snapshot.stocks if str(stock.get("code") or "") == code),
        None,
    )
    if not coded_stock:
        return False
    name_key = normalize_name(coded_stock.get("name"))
    candidate_sectors = {
        str(stock.get("sector") or "").strip()
        for stock in snapshot.stocks
        if normalize_name(stock.get("name")) == name_key
        and str(stock.get("sector") or "").strip()
    }
    ranked_sectors = {
        str(sector.get("name") or "").strip()
        for sector in snapshot.sectors
        if str(sector.get("name") or "").strip()
    }
    return any(
        sector_names_match(candidate_sector, ranked_sector)
        for candidate_sector in candidate_sectors
        for ranked_sector in ranked_sectors
    )


def candidate_has_positive_current_fact(snapshot: EvidenceSnapshot, code: str) -> bool:
    stock = next(
        (item for item in snapshot.stocks if str(item.get("code") or "") == code),
        None,
    )
    if not stock:
        return False
    change_percent = stock.get("change_percent")
    return bool(
        isinstance(change_percent, (int, float))
        and change_percent > 0
        or any(marker in str(stock.get("role") or "") for marker in ("领涨", "连板"))
    )


def normalize_name(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def sector_names_match(left: str, right: str) -> bool:
    normalized_left = normalize_sector_name(left)
    normalized_right = normalize_sector_name(right)
    if not normalized_left or not normalized_right:
        return False
    if normalized_left in normalized_right or normalized_right in normalized_left:
        return True
    left_latin_tokens = set(re.findall(r"[a-z]+\d*[a-z0-9]*", normalized_left))
    right_latin_tokens = set(re.findall(r"[a-z]+\d*[a-z0-9]*", normalized_right))
    return bool(left_latin_tokens & right_latin_tokens)


def normalize_sector_name(value: str) -> str:
    normalized = re.sub(r"[\s()（）\-_/]+", "", value).lower()
    return normalized.replace("概念", "").replace("行业", "")
