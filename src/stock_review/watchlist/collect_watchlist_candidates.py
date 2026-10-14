# 本文件自动收集「进过异动」的候选票，供用户审核后加入跟踪池。
# 候选来自龙虎榜上榜（交易所异动认定的公开口径）、涨停池连板与强势股池；
# 再对入围对象精算 10/30 日偏离值，区分「异常波动」与「严重异常波动」两档。

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from stock_review.evidence.akshare_source import AkshareRequestPacer, import_akshare_client
from stock_review.watchlist.collect_watchlist_inputs import (
    DEFAULT_LONGHUBANG_DAYS,
    WatchlistCollectError,
    normalize_daily_bars,
    to_prefixed_symbol,
)
from stock_review.watchlist.compute_watchlist_facts import (
    INTERVAL_LOOKBACK_BARS,
    REGULATION_ABNORMAL,
    REGULATION_NONE,
    REGULATION_SEVERE,
    deviation_pairs,
    interval_extremes,
    rolling_deviation,
    scan_regulation_events,
)
from stock_review.watchlist.watchlist_inputs import (
    BENCHMARK_NAMES,
    DailyBar,
    benchmark_for_board,
    board_for_code,
)


# 精算上限：精算每只需要 1 次日线请求，控制在可接受范围内。
DEFAULT_TOP_N = 50
DEFAULT_EVALUATE_LIMIT = 120
# 粗筛门槛：近 N 次上榜、连板高度达到该值才入围。
MIN_LONGHUBANG_COUNT = 2
MIN_LIMIT_UP_BOARDS = 2
# 最近一次异动出现在该自然日数内即视为「近期仍活跃」。
RECENT_ACTIVE_DAYS = 20

LONGHUBANG_SOURCE = "akshare:stock_lhb_detail_em"
LIMIT_UP_SOURCE = "akshare:stock_zt_pool_em"
STRONG_SOURCE = "akshare:stock_zt_pool_strong_em"
STOCK_SOURCE = "akshare:stock_zh_a_daily(qfq)"
INDEX_SOURCE = "akshare:stock_zh_index_daily"

# 上榜原因里出现这些字样，说明该条记录本身就是偏离值类异动认定。
DEVIATION_KEYWORDS = ("偏离值", "涨幅偏离", "累计")
STRONG_KEYWORDS = ("多次涨停", "新高", "涨停")


def collect_candidates(
    trade_date: str,
    pool_codes: tuple[str, ...] = (),
    *,
    ak_client: Any | None = None,
    top_n: int = DEFAULT_TOP_N,
    evaluate_limit: int = DEFAULT_EVALUATE_LIMIT,
    longhubang_days: int = DEFAULT_LONGHUBANG_DAYS,
) -> dict[str, Any]:
    """收集候选并精算异动档位；单只候选精算失败只记缺口，不阻断其余候选。"""
    client = ak_client or import_akshare_client()
    pacer = AkshareRequestPacer()
    gaps: list[str] = []
    query_date = trade_date.replace("-", "")

    longhubang = _safe(lambda: fetch_longhubang_seeds(client, trade_date, longhubang_days, pacer), gaps, "龙虎榜")
    limit_up = _safe(lambda: fetch_limit_up_seeds(client, query_date, pacer), gaps, "涨停池")
    strong = _safe(lambda: fetch_strong_seeds(client, query_date, pacer), gaps, "强势股池")

    seeds: dict[str, dict[str, Any]] = {}
    for code, record in (longhubang or {}).items():
        seed = seeds.setdefault(code, _empty_seed(code, record.get("name")))
        seed["longhubang_count"] = record.get("count", 0)
        seed["longhubang_reasons"] = record.get("reasons", [])
        seed["longhubang_dates"] = record.get("dates", [])
        if record.get("dates"):
            seed["last_active_date"] = max(record["dates"])
    for code, record in (limit_up or {}).items():
        seed = seeds.setdefault(code, _empty_seed(code, record.get("name")))
        seed["limit_up_boards"] = max(seed.get("limit_up_boards") or 0, record.get("boards") or 0)
        seed["sector"] = seed.get("sector") or record.get("sector")
        # 当日涨停说明该票今天在动，按当日计入活跃度。
        if (record.get("boards") or 0) >= MIN_LIMIT_UP_BOARDS:
            seed["last_active_date"] = max(seed.get("last_active_date") or "", trade_date)
    for code, record in (strong or {}).items():
        seed = seeds.setdefault(code, _empty_seed(code, record.get("name")))
        seed["strong_reasons"] = record.get("reasons", [])
        seed["sector"] = seed.get("sector") or record.get("sector")

    gated = [
        seed
        for seed in seeds.values()
        if _passes_seed_gate(seed, longhubang or {}, strong or {}, trade_date)
    ]
    # 粗排以「最近一次活跃」为主导，避免已停歇的反复上榜票压过近期才在动的核心票。
    ranked = sorted(
        gated, key=lambda seed: (-_seed_score(seed, trade_date), str(seed.get("code") or ""))
    )[:evaluate_limit]

    index_cache: dict[str, tuple[DailyBar, ...]] = {}
    records: list[dict[str, Any]] = []
    for seed in ranked:
        records.append(
            _evaluate_seed(
                client,
                seed,
                trade_date,
                pacer,
                index_cache,
                gaps,
                pool_codes,
            )
        )

    # 严重异常波动优先，其次按区间涨幅降序：先审「前期涨幅最大的领涨核心」，再看其余。
    records.sort(
        key=lambda item: (
            -_level_weight(item["regulation_level"]),
            -(item.get("interval_gain_percent") or 0),
            -_days_since_epoch(item.get("last_active_date")),
        )
    )

    return {
        "trade_date": trade_date,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": {
            "longhubang": LONGHUBANG_SOURCE,
            "limit_up": LIMIT_UP_SOURCE,
            "strong": STRONG_SOURCE,
            "stock_daily": STOCK_SOURCE,
            "index_daily": INDEX_SOURCE,
        },
        "seed_count": len(seeds),
        "gated_count": len(gated),
        "evaluated_count": len(records),
        "candidates": records[:top_n],
        "gaps": gaps,
        "note": (
            "候选由龙虎榜上榜、涨停池连板与强势股池自动汇总，仅供审核；"
            "「严重异常波动」按连续 10 日偏离值累计≥100% 或 30 日≥200% 计算，"
            "「异常波动」按连续 3 日偏离值累计≥板块阈值计算。"
        ),
    }


def _empty_seed(code: str, name: Any) -> dict[str, Any]:
    return {
        "code": code,
        "name": str(name or "").strip(),
        "longhubang_count": 0,
        "longhubang_reasons": [],
        "longhubang_dates": [],
        "limit_up_boards": 0,
        "strong_reasons": [],
        "sector": None,
        "last_active_date": "",
    }


# 粗排分数：以「异动频次」为主（前期持续异动是核心票的主要特征），近期活跃只作小幅加成。
def _seed_score(seed: dict[str, Any], trade_date: str) -> int:
    count = min(seed.get("longhubang_count") or 0, 20)
    boards = seed.get("limit_up_boards") or 0
    strong = len(seed.get("strong_reasons") or [])
    days = _recency_days(seed.get("last_active_date"), trade_date)
    bonus = 50 if days is not None and days <= 5 else (20 if days is not None and days <= 20 else 0)
    return count * 5 + boards * 8 + strong * 3 + bonus


def _recency_days(last_active: Any, trade_date: str) -> int | None:
    if not last_active:
        return None
    return _days_since_epoch(trade_date) - _days_since_epoch(last_active)


def _days_since_epoch(value: Any) -> int:
    try:
        return date.fromisoformat(str(value)[:10]).toordinal()
    except (TypeError, ValueError):
        return 0


def _recent_enough(dates: list[str], trade_date: str) -> bool:
    if not dates:
        return False
    return _days_since_epoch(trade_date) - _days_since_epoch(max(dates)) <= RECENT_ACTIVE_DAYS


# 入围要求「进过异动」：有龙虎榜偏离值类认定且近期仍活跃，或当日连板达到门槛。
def _passes_seed_gate(
    seed: dict[str, Any],
    longhubang: dict[str, Any],
    strong: dict[str, Any],
    trade_date: str,
) -> bool:
    code = str(seed.get("code") or "")
    if len(code) != 6 or not code.isdigit():
        return False
    if board_for_code(code) == "待确认":
        return False
    # ST 与退市整理类不纳入核心票跟踪：它们的异动多由退市风险驱动，形态不可比。
    name = str(seed.get("name") or "").upper()
    if "ST" in name or "退" in name:
        return False
    if (seed.get("limit_up_boards") or 0) >= MIN_LIMIT_UP_BOARDS:
        return True

    record = longhubang.get(code) or {}
    if not record:
        # 无龙虎榜认定时，只有「近期多次涨停/新高的强势股」才算候选。
        return any(
            keyword in reason for reason in (seed.get("strong_reasons") or []) for keyword in STRONG_KEYWORDS
        )
    if _recent_enough(record.get("dates") or [], trade_date):
        return True
    # 已停歇的票只在「多次上榜且含累计型偏离认定」时保留，代表前期是持续异动核心。
    return (record.get("count") or 0) >= MIN_LONGHUBANG_COUNT and any(
        keyword in reason for reason in (record.get("reasons") or []) for keyword in DEVIATION_KEYWORDS
    )


def _evaluate_seed(
    client: Any,
    seed: dict[str, Any],
    trade_date: str,
    pacer: AkshareRequestPacer,
    index_cache: dict[str, tuple[DailyBar, ...]],
    gaps: list[str],
    pool_codes: tuple[str, ...],
) -> dict[str, Any]:
    code = str(seed["code"])
    name = str(seed.get("name") or code)
    board = board_for_code(code)
    benchmark = benchmark_for_board(board)
    record: dict[str, Any] = {
        "code": code,
        "name": name,
        "board": board,
        "benchmark": benchmark,
        "benchmark_name": BENCHMARK_NAMES.get(benchmark, benchmark),
        "sector": seed.get("sector"),
        "longhubang_count": seed.get("longhubang_count") or 0,
        "longhubang_reasons": seed.get("longhubang_reasons") or [],
        "longhubang_dates": seed.get("longhubang_dates") or [],
        "limit_up_boards": seed.get("limit_up_boards") or 0,
        "strong_reasons": seed.get("strong_reasons") or [],
        "last_active_date": seed.get("last_active_date") or "",
        "close": None,
        "change_percent": None,
        "interval_low": None,
        "interval_low_date": None,
        "interval_high": None,
        "interval_high_date": None,
        "interval_gain_percent": None,
        "deviation_3d": None,
        "deviation_10d": None,
        "deviation_30d": None,
        "regulation_level": REGULATION_NONE,
        "in_pool": code in set(pool_codes),
        "missing": [],
    }

    start_date = (date.fromisoformat(trade_date) - timedelta(days=250)).strftime("%Y%m%d")
    try:
        pacer.wait_before_request()
        frame = client.stock_zh_a_daily(
            symbol=to_prefixed_symbol(code), start_date=start_date, end_date=trade_date.replace("-", ""), adjust="qfq"
        )
        bars = tuple(bar for bar in normalize_daily_bars(frame) if bar.trade_date <= trade_date)
    except Exception as error:  # noqa: BLE001
        record["missing"].append("日线不可用")
        gaps.append(f"候选 {name}（{code}）日线采集失败：{error}")
        return record

    if not bars:
        record["missing"].append("日线不可用")
        return record

    closes = [bar.close for bar in bars]
    record["close"] = closes[-1]
    if len(closes) >= 2 and closes[-2]:
        record["change_percent"] = round((closes[-1] / closes[-2] - 1) * 100, 4)

    low, low_date, high, high_date = interval_extremes(bars, INTERVAL_LOOKBACK_BARS)
    record["interval_low"] = low
    record["interval_low_date"] = low_date
    record["interval_high"] = high
    record["interval_high_date"] = high_date
    if low and high and low > 0:
        record["interval_gain_percent"] = round((high / low - 1) * 100, 2)

    if benchmark not in index_cache:
        try:
            pacer.wait_before_request()
            index_frame = client.stock_zh_index_daily(symbol=benchmark)
            index_cache[benchmark] = tuple(
                bar for bar in normalize_daily_bars(index_frame) if bar.trade_date <= trade_date
            )
        except Exception as error:  # noqa: BLE001
            index_cache[benchmark] = ()
            gaps.append(f"候选 {name}（{code}）基准指数 {benchmark} 采集失败：{error}")

    pairs = deviation_pairs(bars, index_cache.get(benchmark) or ())
    record["deviation_3d"] = rolling_deviation(pairs, 3)
    record["deviation_10d"] = rolling_deviation(pairs, 10)
    record["deviation_30d"] = rolling_deviation(pairs, 30)
    events = scan_regulation_events(pairs, board)
    level = REGULATION_NONE
    for event in events:
        if _level_weight(event["kind"]) > _level_weight(level):
            level = event["kind"]
    record["regulation_level"] = level
    return record


def fetch_longhubang_seeds(
    client: Any,
    trade_date: str,
    lookback_days: int,
    pacer: AkshareRequestPacer,
) -> dict[str, dict[str, Any]]:
    start_date = (date.fromisoformat(trade_date) - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end_date = trade_date.replace("-", "")
    pacer.wait_before_request()
    frame = client.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else []
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("代码") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        record = result.setdefault(code, {"name": row.get("名称"), "count": 0, "reasons": [], "dates": []})
        record["count"] += 1
        reason = str(row.get("上榜原因") or "").strip()
        if reason and reason not in record["reasons"]:
            record["reasons"].append(reason)
        day = str(row.get("上榜日") or "")[:10]
        if day and day not in record["dates"]:
            record["dates"].append(day)
    return result


def fetch_limit_up_seeds(
    client: Any,
    query_date: str,
    pacer: AkshareRequestPacer,
) -> dict[str, dict[str, Any]]:
    pacer.wait_before_request()
    frame = client.stock_zt_pool_em(date=query_date)
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else []
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("代码") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        result[code] = {
            "name": row.get("名称"),
            "boards": _int(row.get("连板数")) or 0,
            "sector": str(row.get("所属行业") or "").strip() or None,
        }
    return result


def fetch_strong_seeds(
    client: Any,
    query_date: str,
    pacer: AkshareRequestPacer,
) -> dict[str, dict[str, Any]]:
    pacer.wait_before_request()
    frame = client.stock_zt_pool_strong_em(date=query_date)
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else []
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("代码") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        reason = str(row.get("入选理由") or "").strip()
        result[code] = {
            "name": row.get("名称"),
            "reasons": [reason] if reason else [],
            "sector": str(row.get("所属行业") or "").strip() or None,
        }
    return result


def _level_weight(level: str) -> int:
    return {REGULATION_NONE: 0, REGULATION_ABNORMAL: 1, REGULATION_SEVERE: 2}.get(level, 0)


def _safe(action: Any, gaps: list[str], label: str) -> Any:
    try:
        return action()
    except WatchlistCollectError as error:
        gaps.append(f"{label}采集失败：{error}")
    except Exception as error:  # noqa: BLE001
        gaps.append(f"{label}采集失败：{error}")
    return {}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
