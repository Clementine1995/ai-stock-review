# 本文件负责通过 AKShare 采集最小市场层证据，不做全市场扫描和交易判断。

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class AkshareSourceError(RuntimeError):
    pass


INDEX_SYMBOLS = (
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
)

EASTMONEY_SECIDS = {
    "sh000001": "1.000001",
    "sz399001": "0.399001",
    "sz399006": "0.399006",
}


# 当前最小范围只采集 STEP 1 需要的指数事实；情绪、板块和个股留给后续数据源补齐。
def collect_akshare_market_evidence(trade_date: str, ak_client: Any | None = None) -> dict[str, Any]:
    client = ak_client or import_akshare_client()
    target_date = date.fromisoformat(trade_date)

    indices: list[dict[str, Any]] = []
    sample_dates: list[str] = []
    total_amount = Decimal("0")
    amount_count = 0
    events: list[dict[str, str]] = [
        {
            "title": "AKShare 市场层最小采集",
            "source": "akshare",
            "note": "当前仅采集指数日线数据；情绪、板块和个股证据仍需后续数据源补齐。",
        }
    ]

    for symbol, name in INDEX_SYMBOLS:
        rows = get_index_rows(client, symbol, events)
        selected_row, previous_row = select_rows_for_trade_date(rows, target_date)
        if selected_row is None:
            continue

        row_date = normalize_date(get_first_value(selected_row, ("date", "日期")))
        close = parse_decimal(get_first_value(selected_row, ("close", "收盘")))
        previous_close = parse_decimal(get_first_value(previous_row or {}, ("close", "收盘")))
        change_percent = parse_change_percent(selected_row, close, previous_close)
        amount = parse_decimal(get_first_value(selected_row, ("amount", "成交额")))

        index_record: dict[str, Any] = {
            "symbol": symbol,
            "name": name,
            "sample_date": row_date,
            "close": decimal_to_number(close),
            "change_percent": decimal_to_number(change_percent),
        }
        if amount is not None:
            index_record["amount"] = decimal_to_number(amount)
            total_amount += amount
            amount_count += 1

        indices.append(index_record)
        if row_date:
            sample_dates.append(row_date)

    sample_date = max(sample_dates) if sample_dates else trade_date
    market: dict[str, Any] = {"indices": indices}
    if amount_count == len(INDEX_SYMBOLS):
        market["total_amount"] = decimal_to_number(total_amount)
        # 该字段来自指数日线成交额求和，用于明确两市成交额当前是近似来源，避免伪装成交易所总貌。
        market["total_amount_source"] = "akshare_stock_zh_index_daily_em_amount_sum"

    return {
        "source": "akshare",
        "sample_date": sample_date,
        "market": market,
        "sentiment": {},
        "sectors": [],
        "stocks": [],
        "events": events,
    }


def import_akshare_client() -> Any:
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except ImportError as error:
        raise AkshareSourceError("未安装 AKShare，请先安装可选依赖：python -m pip install -e .[data]") from error
    return ak


def get_index_rows(client: Any, symbol: str, events: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    try:
        frame = client.stock_zh_index_daily_em(symbol=symbol)
    except Exception as error:  # noqa: BLE001
        return get_index_rows_from_eastmoney(symbol, error, events)

    if hasattr(frame, "to_dict"):
        rows = frame.to_dict("records")
    elif isinstance(frame, list):
        rows = frame
    else:
        raise AkshareSourceError(f"AKShare 指数接口返回格式不可识别：{symbol}")

    return [row for row in rows if isinstance(row, dict)]


def get_index_rows_from_eastmoney(
    symbol: str,
    primary_error: Exception,
    events: list[dict[str, str]] | None,
) -> list[dict[str, Any]]:
    try:
        rows = request_eastmoney_index_rows(symbol)
    except Exception as error:  # noqa: BLE001
        raise AkshareSourceError(
            f"AKShare 指数接口调用失败且东方财富备用路径也失败：{symbol}："
            f"akshare={primary_error}；eastmoney={error}"
        ) from error

    if events is not None:
        # 备用路径只在 AKShare 封装接口失败时启用，写入事件用于日报识别数据降级来源。
        events.append(
            {
                "title": f"{symbol} 使用东方财富备用路径",
                "source": "eastmoney",
                "note": f"AKShare stock_zh_index_daily_em 失败后，使用同源 K 线接口直连；原始错误：{primary_error}",
            }
        )
    return rows


def request_eastmoney_index_rows(symbol: str) -> list[dict[str, Any]]:
    secid = EASTMONEY_SECIDS.get(symbol)
    if secid is None:
        raise AkshareSourceError(f"东方财富备用路径缺少指数映射：{symbol}")

    import requests

    # 用户本机验证完整 URL 直连可用；这里保持逗号不编码，避免退回到 AKShare 失败时的请求形态。
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}"
        "&fields1=f1,f2,f3,f4,f5"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        "&klt=101&fqt=0&beg=19900101&end=20500101"
    )
    session = requests.Session()
    # 备用路径用于绕过 AKShare 封装层的代理错误；这里明确不读取环境代理，避免同一错误继续污染直连请求。
    session.trust_env = False
    response = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    response.raise_for_status()
    payload = response.json()
    klines = payload.get("data", {}).get("klines", [])
    if not isinstance(klines, list):
        raise AkshareSourceError("东方财富备用路径返回的 K 线格式不可识别")

    rows: list[dict[str, Any]] = []
    for item in klines:
        if not isinstance(item, str):
            continue
        parts = item.split(",")
        if len(parts) < 7:
            continue
        rows.append(
            {
                "date": parts[0],
                "open": parts[1],
                "close": parts[2],
                "high": parts[3],
                "low": parts[4],
                "volume": parts[5],
                "amount": parts[6],
            }
        )

    if not rows:
        raise AkshareSourceError("东方财富备用路径未返回有效 K 线")
    return rows


def select_rows_for_trade_date(
    rows: list[dict[str, Any]],
    target_date: date,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    dated_rows = [
        (date_value, row)
        for row in rows
        if (date_value := parse_date_value(get_first_value(row, ("date", "日期")))) is not None
    ]
    dated_rows.sort(key=lambda item: item[0])
    eligible_rows = [(date_value, row) for date_value, row in dated_rows if date_value <= target_date]
    if not eligible_rows:
        return None, None

    selected_date, selected_row = eligible_rows[-1]
    previous_rows = [(date_value, row) for date_value, row in dated_rows if date_value < selected_date]
    previous_row = previous_rows[-1][1] if previous_rows else None
    return selected_row, previous_row


def parse_change_percent(
    row: dict[str, Any],
    close: Decimal | None,
    previous_close: Decimal | None,
) -> Decimal | None:
    explicit_value = parse_decimal(get_first_value(row, ("change_percent", "pct_chg", "涨跌幅")))
    if explicit_value is not None:
        return explicit_value
    if close is None or previous_close in (None, Decimal("0")):
        return None
    return ((close - previous_close) / previous_close * Decimal("100")).quantize(Decimal("0.01"))


def get_first_value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return None


def parse_date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_date(value: Any) -> str:
    parsed_date = parse_date_value(value)
    return parsed_date.isoformat() if parsed_date else ""


def parse_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def decimal_to_number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)
