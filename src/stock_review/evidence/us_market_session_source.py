# 本文件负责判断美股常规交易会话是否落入 A 股收盘到下一 A 股开盘的有效窗，不负责推断跨市场因果。

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math
from typing import Any


US_MARKET_SOURCE = "public:us_regular_session"
US_SESSION_AVAILABLE = "available"
US_SESSION_PENDING = "pending"
PENDING_SESSION_NOTE = (
    "截至证据时点，A 股收盘后尚无已完成且落入有效窗的美股常规交易会话；"
    "状态保持 pending，不回退使用 A 股收盘前的旧会话。"
)
AVAILABLE_SESSION_NOTE = "仅记录有效窗内且在证据时点前已经完成的美股常规交易会话，不表示跨市场因果。"


class UsMarketSessionSourceError(RuntimeError):
    pass


# 外部客户端只返回指定时间范围内的美股常规会话；时点资格由本模块统一判定。
def collect_us_market_window_evidence(
    review_date: str,
    generated_at: str | datetime,
    as_of: str | datetime,
    a_share_close_at: str | datetime,
    next_a_share_open_at: str | datetime,
    client: Any,
) -> dict[str, Any]:
    window_start = parse_aware_datetime(a_share_close_at, "A 股收盘时点")
    window_end = parse_aware_datetime(next_a_share_open_at, "下一 A 股开盘时点")
    cutoff = parse_aware_datetime(as_of, "证据时点")
    try:
        sessions = normalize_rows(
            client.fetch_us_regular_sessions(
                window_start=window_start.isoformat(timespec="seconds"),
                window_end=window_end.isoformat(timespec="seconds"),
                as_of=cutoff.isoformat(timespec="seconds"),
            )
        )
    except UsMarketSessionSourceError:
        raise
    except Exception as error:  # noqa: BLE001
        raise UsMarketSessionSourceError(f"美股常规会话请求失败：{error}") from error

    source = normalize_text(getattr(client, "source_name", None)) or US_MARKET_SOURCE
    return build_us_market_window_evidence(
        review_date,
        generated_at,
        as_of,
        window_start,
        window_end,
        sessions,
        source=source,
    )


# 有效窗采用严格开区间；未命中的旧会话不会成为 pending 状态的兜底证据。
def build_us_market_window_evidence(
    review_date: str,
    generated_at: str | datetime,
    as_of: str | datetime,
    a_share_close_at: str | datetime,
    next_a_share_open_at: str | datetime,
    sessions: list[dict[str, Any]],
    *,
    source: str = US_MARKET_SOURCE,
) -> dict[str, Any]:
    validate_iso_date(review_date, "复盘日期")
    generated = parse_aware_datetime(generated_at, "报告生成时点")
    cutoff = parse_aware_datetime(as_of, "证据时点")
    window_start = parse_aware_datetime(a_share_close_at, "A 股收盘时点")
    window_end = parse_aware_datetime(next_a_share_open_at, "下一 A 股开盘时点")
    normalized_sessions = normalize_rows(sessions)

    if cutoff > generated:
        raise UsMarketSessionSourceError("证据时点不得晚于报告生成时点。")
    if window_start >= window_end:
        raise UsMarketSessionSourceError("下一 A 股开盘时点必须晚于 A 股收盘时点。")

    eligible_sessions: list[tuple[datetime, str, dict[str, Any]]] = []
    for session in normalized_sessions:
        session_type = normalize_text(first_value(session, ("session_type", "交易时段类型")))
        if session_type not in {"regular", "常规交易"}:
            continue
        session_status = normalize_text(first_value(session, ("status", "会话状态")))
        if session_status not in {"completed", "已完成"}:
            continue

        session_close = parse_aware_datetime(
            first_value(session, ("regular_close_at", "session_close_at", "常规收盘时点")),
            "美股常规会话收盘时点",
        )
        session_date = normalize_date_value(
            first_value(session, ("us_session_date", "session_date", "交易日期"))
        )
        if session_date is None:
            raise UsMarketSessionSourceError("已完成的美股常规会话必须包含有效交易日期。")

        # 会话必须同时落入 A 股窗口并在证据截止时点前完成，两个窗口端点均不计入。
        if window_start < session_close < window_end and session_close <= cutoff:
            eligible_sessions.append((session_close, session_date, session))

    selected = max(eligible_sessions, key=lambda item: item[0]) if eligible_sessions else None
    base = {
        "schema_version": 1,
        "review_date": review_date,
        "generated_at": generated.isoformat(timespec="seconds"),
        "as_of": cutoff.isoformat(timespec="seconds"),
        "source": normalize_text(source) or US_MARKET_SOURCE,
        "window": {
            "a_share_close_at": window_start.isoformat(timespec="seconds"),
            "next_a_share_open_at": window_end.isoformat(timespec="seconds"),
            "boundary": "open_interval",
        },
    }
    if selected is None:
        return {
            **base,
            "us_session_date": None,
            "status": US_SESSION_PENDING,
            "session_close_at": None,
            "indices": [],
            "note": PENDING_SESSION_NOTE,
        }

    session_close, session_date, session = selected
    return {
        **base,
        "us_session_date": session_date,
        "status": US_SESSION_AVAILABLE,
        "session_close_at": session_close.isoformat(timespec="seconds"),
        "indices": normalize_indices(session.get("indices")),
        "note": AVAILABLE_SESSION_NOTE,
    }


def normalize_indices(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    rows = normalize_rows(value)
    indices: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for row in rows:
        symbol = normalize_text(first_value(row, ("symbol", "code", "指数代码")))
        name = normalize_text(first_value(row, ("name", "指数名称")))
        if symbol is None:
            raise UsMarketSessionSourceError("美股指数记录必须包含代码。")
        if symbol in seen_symbols:
            raise UsMarketSessionSourceError(f"美股指数代码重复：{symbol}。")
        seen_symbols.add(symbol)
        indices.append(
            {
                "symbol": symbol,
                "name": name,
                "close": parse_optional_number(
                    first_value(row, ("close", "收盘点位")),
                    f"{symbol} 收盘点位",
                    allow_negative=False,
                ),
                "change_percent": parse_optional_number(
                    first_value(row, ("change_percent", "涨跌幅")),
                    f"{symbol} 涨跌幅",
                    allow_negative=True,
                    allow_percent_sign=True,
                ),
            }
        )
    return indices


def normalize_rows(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        value = value.to_dict("records")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise UsMarketSessionSourceError("美股常规会话返回格式必须是对象数组。")
    return value


def parse_aware_datetime(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as error:
            raise UsMarketSessionSourceError(f"{label}必须是带时区的 ISO 8601 时点。") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UsMarketSessionSourceError(f"{label}必须包含明确时区。")
    return parsed


def parse_optional_number(
    value: Any,
    label: str,
    *,
    allow_negative: bool,
    allow_percent_sign: bool = False,
) -> int | float | None:
    if is_missing_value(value):
        return None
    if isinstance(value, bool):
        raise UsMarketSessionSourceError(f"{label}必须是有限数值。")
    text = str(value).strip().replace(",", "")
    if allow_percent_sign and text.endswith("%"):
        text = text[:-1].strip()
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError) as error:
        raise UsMarketSessionSourceError(f"{label}必须是有限数值。") from error
    if not parsed.is_finite() or (not allow_negative and parsed < 0):
        raise UsMarketSessionSourceError(f"{label}必须是有效数值。")
    return decimal_to_number(parsed)


def first_value(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in row:
            return row[alias]
    return None


def normalize_text(value: Any) -> str | None:
    if is_missing_value(value):
        return None
    text = str(value).strip()
    return text or None


def normalize_date_value(value: Any) -> str | None:
    if is_missing_value(value):
        return None
    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        text = text[:10]
    elif len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def validate_iso_date(value: str, label: str) -> None:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise UsMarketSessionSourceError(f"{label}必须是 YYYY-MM-DD。") from error


def is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in {"", "-", "nan", "NaN", "<NA>", "NaT"}
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, Decimal):
        return value.is_nan()
    return False


def decimal_to_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)
