# 本文件通过 AKShare 采集跟踪池个股的日线、基准指数、龙虎榜与资金流向事实，装配成输入模型。
# 它不做全市场选股，也不写正式对象链；采不到的字段留空并记入 gaps。

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from stock_review.evidence.akshare_source import AkshareRequestPacer, import_akshare_client
from stock_review.watchlist.watchlist_inputs import (
    BENCHMARK_NAMES,
    DailyBar,
    WatchlistInputs,
    WatchlistItem,
)


class WatchlistCollectError(RuntimeError):
    pass


# 个股日线回看 250 个自然日，覆盖约 170 个交易日，足够算 30 日偏离与 60 日区间极值。
DEFAULT_LOOKBACK_CALENDAR_DAYS = 250
# 龙虎榜只回溯 45 个自然日，用于判断「近期是否多次上榜」。
DEFAULT_LONGHUBANG_DAYS = 45

CN_AMOUNT_UNITS = {"万亿": 1e12, "亿": 1e8, "万": 1e4}
CN_AMOUNT_PATTERN = re.compile(r"^(-?[\d.]+)(万亿|亿|万)?$")

STOCK_SOURCE = "akshare:stock_zh_a_daily(qfq)"
INDEX_SOURCE = "akshare:stock_zh_index_daily"
LONGHUBANG_SOURCE = "akshare:stock_lhb_detail_em"
FUND_FLOW_SOURCE = "akshare:stock_fund_flow_individual"


def collect_watchlist_inputs(
    trade_date: str,
    items: tuple[WatchlistItem, ...],
    *,
    ak_client: Any | None = None,
    lookback_calendar_days: int = DEFAULT_LOOKBACK_CALENDAR_DAYS,
    longhubang_days: int = DEFAULT_LONGHUBANG_DAYS,
) -> WatchlistInputs:
    """采集池内对象事实。单只票失败只记缺口，不阻断其余对象的采集。"""
    target_date = date.fromisoformat(trade_date)
    start = target_date - timedelta(days=lookback_calendar_days)
    start_date = start.strftime("%Y%m%d")
    start_date_iso = start.isoformat()
    end_date = target_date.strftime("%Y%m%d")
    client = ak_client or import_akshare_client()
    pacer = AkshareRequestPacer()
    gaps: list[str] = []

    daily_bars: dict[str, tuple[DailyBar, ...]] = {}
    for item in items:
        try:
            bars = fetch_stock_bars(client, item.code, start_date, end_date, pacer)
        except WatchlistCollectError as error:
            gaps.append(f"{item.name}（{item.code}）日线采集失败：{error}")
            daily_bars[item.code] = ()
            continue
        # 只保留目标交易日及之前的数据，避免把更晚的快照混入该日跟踪表。
        daily_bars[item.code] = tuple(bar for bar in bars if bar.trade_date <= trade_date)
        if not daily_bars[item.code]:
            gaps.append(f"{item.name}（{item.code}）在 {trade_date} 前没有可用日线")

    index_bars: dict[str, tuple[DailyBar, ...]] = {}
    for benchmark in sorted({item.benchmark for item in items}):
        try:
            bars = fetch_index_bars(client, benchmark, pacer)
        except WatchlistCollectError as error:
            gaps.append(f"基准指数 {BENCHMARK_NAMES.get(benchmark, benchmark)} 采集失败：{error}")
            index_bars[benchmark] = ()
            continue
        # 指数接口返回全历史（创业板指约 4000 根），必须同时按回看窗口的下限与目标日上限截断：
        # 偏离值只需与个股日线同日配对，多留的历史只会让每日快照体积膨胀数十倍。
        index_bars[benchmark] = tuple(
            bar for bar in bars if start_date_iso <= bar.trade_date <= trade_date
        )
        if not index_bars[benchmark]:
            gaps.append(f"基准指数 {BENCHMARK_NAMES.get(benchmark, benchmark)} 在 {trade_date} 前没有可用日线")

    longhubang: dict[str, dict[str, Any]] = {}
    try:
        longhubang = fetch_longhubang(client, [item.code for item in items], trade_date, longhubang_days, pacer)
    except WatchlistCollectError as error:
        gaps.append(f"龙虎榜采集失败：{error}")

    fund_flow: dict[str, dict[str, Any]] = {}
    try:
        fund_flow = fetch_fund_flow(client, [item.code for item in items], pacer)
    except WatchlistCollectError as error:
        gaps.append(f"资金流向采集失败：{error}")

    sources = {
        "stock_daily": STOCK_SOURCE,
        "index_daily": INDEX_SOURCE,
        "longhubang": LONGHUBANG_SOURCE,
        "fund_flow": FUND_FLOW_SOURCE,
    }
    return WatchlistInputs(
        trade_date=trade_date,
        items=items,
        daily_bars=daily_bars,
        index_bars=index_bars,
        longhubang=longhubang,
        fund_flow=fund_flow,
        sources=sources,
        gaps=tuple(gaps),
    )


# 新浪前复权日线：与通达信前复权口径在已核对样本上逐位一致。
def fetch_stock_bars(
    client: Any,
    code: str,
    start_date: str,
    end_date: str,
    pacer: AkshareRequestPacer,
) -> tuple[DailyBar, ...]:
    symbol = to_prefixed_symbol(code)
    try:
        pacer.wait_before_request()
        frame = client.stock_zh_a_daily(
            symbol=symbol, start_date=start_date, end_date=end_date, adjust="qfq"
        )
    except Exception as error:  # noqa: BLE001
        raise WatchlistCollectError(f"stock_zh_a_daily 调用失败：{error}") from error
    return normalize_daily_bars(frame)


# 新浪指数日线返回全历史且无日期参数；取回后由调用方按回看窗口截断（下限=回看起点，上限=目标交易日）。
def fetch_index_bars(
    client: Any,
    benchmark: str,
    pacer: AkshareRequestPacer,
) -> tuple[DailyBar, ...]:
    try:
        pacer.wait_before_request()
        frame = client.stock_zh_index_daily(symbol=benchmark)
    except Exception as error:  # noqa: BLE001
        raise WatchlistCollectError(f"stock_zh_index_daily 调用失败：{error}") from error
    return normalize_daily_bars(frame)


def fetch_longhubang(
    client: Any,
    codes: list[str],
    trade_date: str,
    lookback_days: int,
    pacer: AkshareRequestPacer,
) -> dict[str, dict[str, Any]]:
    start_date = (date.fromisoformat(trade_date) - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end_date = trade_date.replace("-", "")
    try:
        pacer.wait_before_request()
        frame = client.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
    except Exception as error:  # noqa: BLE001
        raise WatchlistCollectError(f"stock_lhb_detail_em 调用失败：{error}") from error
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else []

    wanted = set(codes)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("代码") or "").strip()
        if code not in wanted:
            continue
        grouped.setdefault(code, []).append(
            {
                "date": str(row.get("上榜日") or "")[:10],
                "reason": _text(row.get("上榜原因")),
                "interpretation": _text(row.get("解读")),
                "net_amount": _float(row.get("龙虎榜净买额")),
                "buy_amount": _float(row.get("龙虎榜买入额")),
                "sell_amount": _float(row.get("龙虎榜卖出额")),
                "turnover_rate": _float(row.get("换手率")),
            }
        )
    result: dict[str, dict[str, Any]] = {}
    for code, records in grouped.items():
        records.sort(key=lambda item: item["date"])
        result[code] = {"listed": True, "count": len(records), "records": records, "latest": records[-1]}
    return result


def fetch_fund_flow(
    client: Any,
    codes: list[str],
    pacer: AkshareRequestPacer,
) -> dict[str, dict[str, Any]]:
    try:
        pacer.wait_before_request()
        frame = client.stock_fund_flow_individual(symbol="即时")
    except Exception as error:  # noqa: BLE001
        raise WatchlistCollectError(f"stock_fund_flow_individual 调用失败：{error}") from error
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else []

    wanted = set(codes)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("股票代码") or "").strip()
        if code not in wanted:
            continue
        result[code] = {
            "change_percent_text": _text(row.get("涨跌幅")),
            "net_amount": parse_cn_amount(row.get("净额")),
            "net_amount_text": _text(row.get("净额")),
            "inflow_text": _text(row.get("流入资金")),
            "outflow_text": _text(row.get("流出资金")),
            "main_net_amount": parse_cn_amount(row.get("净额")),
        }
    return result


def normalize_daily_bars(frame: Any) -> tuple[DailyBar, ...]:
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else frame if isinstance(frame, list) else []
    bars: list[DailyBar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = str(row.get("date") or "")[:10]
        open_price = _float(row.get("open"))
        high = _float(row.get("high"))
        low = _float(row.get("low"))
        close = _float(row.get("close"))
        if not day or None in (open_price, high, low, close):
            continue
        bars.append(
            DailyBar(
                trade_date=day,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=_float(row.get("volume")) or 0.0,
                amount=_float(row.get("amount")),
                turnover=_float(row.get("turnover")),
            )
        )
    bars.sort(key=lambda bar: bar.trade_date)
    return tuple(bars)


def to_prefixed_symbol(code: str) -> str:
    text = str(code).strip()
    if text.startswith("6"):
        return f"sh{text}"
    if text.startswith(("0", "3")):
        return f"sz{text}"
    if text.startswith(("4", "8", "9")):
        return f"bj{text}"
    raise WatchlistCollectError(f"无法确定交易所前缀：{code}")


# 同花顺资金流的金额是「5.15亿」这类文本，需换算为元；无法解析时返回 None 而不是 0。
def parse_cn_amount(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text in {"-", "--", "None"}:
        return None
    match = CN_AMOUNT_PATTERN.match(text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    return round(number * CN_AMOUNT_UNITS.get(unit or "", 1.0), 2)


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip() in {"", "-", "--", "nan", "None"}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
