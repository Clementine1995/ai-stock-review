# 本文件负责把 AKShare 纳指和标普日线转换为带美东收盘时点的美股会话，不推断外围市场方向。

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from stock_review.evidence.akshare_source import import_akshare_client
from stock_review.evidence.us_market_session_source import (
    UsMarketSessionSourceError,
    build_us_market_window_evidence,
    decimal_to_number,
    normalize_date_value,
    normalize_rows,
    parse_aware_datetime,
)


AKSHARE_US_MARKET_SOURCE = "akshare:index_us_stock_sina:.IXIC+.INX"
AKSHARE_REQUIRED_US_INDICES = (
    (".IXIC", "纳斯达克综合指数"),
    (".INX", "标普500指数"),
)


# 双指数都具备当日与上一交易日有效收盘价时，才构造可供窗口判定的完整会话。
def collect_akshare_us_market_window_evidence(
    review_date: str,
    generated_at: str | datetime,
    as_of: str | datetime,
    a_share_close_at: str | datetime,
    next_a_share_open_at: str | datetime,
    ak_client: Any | None = None,
) -> dict[str, Any]:
    try:
        target_date = date.fromisoformat(review_date)
    except (TypeError, ValueError) as error:
        raise UsMarketSessionSourceError("复盘日期必须是 YYYY-MM-DD。") from error
    generated = parse_aware_datetime(generated_at, "报告生成时点")
    cutoff = parse_aware_datetime(as_of, "证据时点")
    window_start = parse_aware_datetime(a_share_close_at, "A 股收盘时点")
    window_end = parse_aware_datetime(next_a_share_open_at, "下一 A 股开盘时点")
    eastern_zone, china_zone = load_market_zones()

    if window_start.astimezone(china_zone).date() != target_date:
        raise UsMarketSessionSourceError("A 股收盘时点的中国日期必须等于复盘日期。")
    if cutoff > generated:
        raise UsMarketSessionSourceError("证据时点不得晚于报告生成时点。")
    if window_start >= window_end:
        raise UsMarketSessionSourceError("下一 A 股开盘时点必须晚于 A 股收盘时点。")

    client = load_akshare_client(ak_client)
    histories: dict[str, dict[date, tuple[Decimal, Decimal | None]]] = {}
    for symbol, _name in AKSHARE_REQUIRED_US_INDICES:
        try:
            raw_rows = normalize_rows(client.index_us_stock_sina(symbol=symbol))
        except UsMarketSessionSourceError:
            raise
        except Exception as error:  # noqa: BLE001
            raise UsMarketSessionSourceError(
                f"AKShare 美股指数 {symbol} 日线请求失败：{error}"
            ) from error
        histories[symbol] = build_index_history(raw_rows, symbol)

    sessions = build_complete_us_sessions(
        histories,
        cutoff,
        window_start,
        window_end,
        eastern_zone,
        china_zone,
    )
    return build_us_market_window_evidence(
        review_date,
        generated,
        cutoff,
        window_start,
        window_end,
        sessions,
        source=AKSHARE_US_MARKET_SOURCE,
    )


def build_index_history(
    rows: list[dict[str, Any]],
    symbol: str,
) -> dict[date, tuple[Decimal, Decimal | None]]:
    closes: dict[date, Decimal | None] = {}
    for row in rows:
        sample_date_text = normalize_date_value(row.get("date", row.get("日期")))
        if sample_date_text is None:
            continue
        sample_date = date.fromisoformat(sample_date_text)
        if sample_date in closes:
            raise UsMarketSessionSourceError(f"AKShare 美股指数 {symbol} 存在重复交易日期。")
        closes[sample_date] = parse_positive_decimal(
            row.get("close", row.get("收盘")),
        )

    history: dict[date, tuple[Decimal, Decimal | None]] = {}
    ordered_dates = sorted(closes)
    for position, sample_date in enumerate(ordered_dates):
        close = closes[sample_date]
        if close is None:
            continue
        previous_close = closes[ordered_dates[position - 1]] if position > 0 else None
        change_percent = None
        if previous_close is not None:
            change_percent = ((close - previous_close) / previous_close * Decimal("100")).quantize(
                Decimal("0.0001")
            )
        history[sample_date] = (close, change_percent)
    return history


def build_complete_us_sessions(
    histories: dict[str, dict[date, tuple[Decimal, Decimal | None]]],
    cutoff: datetime,
    window_start: datetime,
    window_end: datetime,
    eastern_zone: ZoneInfo,
    china_zone: ZoneInfo,
) -> list[dict[str, Any]]:
    required_symbols = [symbol for symbol, _name in AKSHARE_REQUIRED_US_INDICES]
    common_dates = set.intersection(
        *(set(histories.get(symbol, {})) for symbol in required_symbols)
    )
    sessions: list[dict[str, Any]] = []
    for session_date in sorted(common_dates):
        values = [histories[symbol][session_date] for symbol in required_symbols]
        if any(change_percent is None for _close, change_percent in values):
            continue
        regular_close = datetime.combine(
            session_date,
            time(hour=16),
            tzinfo=eastern_zone,
        ).astimezone(china_zone)
        if not window_start < regular_close < window_end:
            continue
        indices = [
            {
                "symbol": symbol,
                "name": name,
                "close": decimal_to_number(histories[symbol][session_date][0]),
                "change_percent": decimal_to_number(histories[symbol][session_date][1]),
            }
            for symbol, name in AKSHARE_REQUIRED_US_INDICES
        ]
        sessions.append(
            {
                "us_session_date": session_date.isoformat(),
                "session_type": "regular",
                "status": "completed" if regular_close <= cutoff else "pending",
                "regular_close_at": regular_close.isoformat(timespec="seconds"),
                "indices": indices,
            }
        )
    return sessions


def parse_positive_decimal(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and value.strip() in {"", "-"}):
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def load_market_zones() -> tuple[ZoneInfo, ZoneInfo]:
    try:
        return ZoneInfo("America/New_York"), ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError as error:
        raise UsMarketSessionSourceError("本地缺少美东或中国时区数据，无法确定美股常规收盘时点。") from error


def load_akshare_client(ak_client: Any | None) -> Any:
    if ak_client is not None:
        return ak_client
    try:
        return import_akshare_client()
    except Exception as error:  # noqa: BLE001
        raise UsMarketSessionSourceError(f"无法加载 AKShare 美股指数客户端：{error}") from error
