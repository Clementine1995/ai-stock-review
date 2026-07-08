# 本文件负责通过 AKShare 采集最小市场层证据，不做全市场扫描和交易判断。

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
import urllib.parse
import urllib.request


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

TENCENT_SYMBOLS = {
    "sh000001": "sh000001",
    "sz399001": "sz399001",
    "sz399006": "sz399006",
}

EASTMONEY_SECTOR_CONFIGS = {
    "stock_board_concept_name_em": {
        "url": "https://79.push2.eastmoney.com/api/qt/clist/get",
        "fs": "m:90 t:3 f:!50",
    },
    "stock_board_industry_name_em": {
        "url": "https://17.push2.eastmoney.com/api/qt/clist/get",
        "fs": "m:90 t:2 f:!50",
    },
}


# 当前最小范围只采集 STEP 1 需要的指数事实；情绪、板块和个股留给后续数据源补齐。
def collect_akshare_market_evidence(trade_date: str, ak_client: Any | None = None) -> dict[str, Any]:
    client = ak_client or import_akshare_client()
    target_date = date.fromisoformat(trade_date)

    indices: list[dict[str, Any]] = []
    sample_dates: list[str] = []
    total_amount = Decimal("0")
    amount_count = 0
    amount_sources: set[str] = set()
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
            amount_sources.add(
                str(selected_row.get("__amount_source", "akshare_stock_zh_index_daily_em_amount_sum"))
            )

        indices.append(index_record)
        if row_date:
            sample_dates.append(row_date)

    sample_date = max(sample_dates) if sample_dates else trade_date
    market: dict[str, Any] = {"indices": indices}
    if amount_count == len(INDEX_SYMBOLS):
        market["total_amount"] = decimal_to_number(total_amount)
        # 该字段来自指数日线成交额求和，用于明确两市成交额当前是近似来源，避免伪装成交易所总貌。
        market["total_amount_source"] = "+".join(sorted(amount_sources))

    return {
        "source": "akshare",
        "sample_date": sample_date,
        "market": market,
        "sentiment": {},
        "sectors": [],
        "stocks": [],
        "events": events,
    }


def collect_akshare_sentiment_evidence(trade_date: str, ak_client: Any | None = None) -> dict[str, Any]:
    client = ak_client or import_akshare_client()
    query_date = trade_date.replace("-", "")
    limit_up_rows = get_sentiment_rows(client, "stock_zt_pool_em", query_date)
    broken_board_rows = get_sentiment_rows(client, "stock_zt_pool_zbgc_em", query_date)
    limit_down_rows = get_sentiment_rows(client, "stock_zt_pool_dtgc_em", query_date)

    limit_up_count = len(limit_up_rows)
    broken_board_count = len(broken_board_rows)
    total_touched_limit_up = limit_up_count + broken_board_count
    highest_board = max(
        (int(value) for row in limit_up_rows if (value := parse_int(get_first_value(row, ("连板数",)))) is not None),
        default=None,
    )

    sentiment: dict[str, Any] = {
        "limit_up_count": limit_up_count,
        "limit_down_count": len(limit_down_rows),
        "highest_board": highest_board,
        # 炸板率用炸板股池 / 当日触及涨停总数表示，只作为短线情绪事实，不推导情绪阶段。
        "broken_board_rate": round(broken_board_count / total_touched_limit_up, 4)
        if total_touched_limit_up
        else None,
    }

    return {
        "source": "akshare",
        "sample_date": trade_date,
        "market": {},
        "sentiment": sentiment,
        "sectors": [],
        "stocks": [],
        "events": [
            {
                "title": "AKShare 短线情绪最小采集",
                "source": "akshare",
                "note": "当前仅采集涨停数、跌停数、炸板率和连板高度；情绪温度、板块和个股证据仍需后续数据源补齐。",
            }
        ],
    }


def collect_akshare_sector_evidence(trade_date: str, ak_client: Any | None = None) -> dict[str, Any]:
    client = ak_client or import_akshare_client()
    events: list[dict[str, str]] = [
        {
            "title": "AKShare 热门板块最小采集",
            "source": "akshare",
            "note": "当前仅采集概念和行业板块涨跌幅、总市值、换手率、涨跌家数和领涨股；不写入核心票列表。",
        }
    ]
    sectors = [
        *collect_sector_records(client, "stock_board_concept_name_em", "concept", events),
        *collect_sector_records(client, "stock_board_industry_name_em", "industry", events),
    ]
    if not sectors:
        sectors = collect_sector_records(client, "stock_board_industry_summary_ths", "industry_ths", events)
    if not sectors:
        raise AkshareSourceError("AKShare 板块接口均未返回有效板块")

    return {
        "source": "akshare",
        "sample_date": trade_date,
        "market": {},
        "sentiment": {},
        "sectors": sectors,
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
        return get_index_rows_from_fallback_sources(symbol, error, events)

    if hasattr(frame, "to_dict"):
        rows = frame.to_dict("records")
    elif isinstance(frame, list):
        rows = frame
    else:
        raise AkshareSourceError(f"AKShare 指数接口返回格式不可识别：{symbol}")

    return [row for row in rows if isinstance(row, dict)]


def get_sector_rows(client: Any, method_name: str) -> list[dict[str, Any]]:
    try:
        frame = getattr(client, method_name)()
    except Exception as error:  # noqa: BLE001
        try:
            return request_eastmoney_sector_rows(method_name)
        except Exception as fallback_error:  # noqa: BLE001
            raise AkshareSourceError(
                f"AKShare 板块接口调用失败且东方财富直连备用路径也失败：{method_name}："
                f"akshare={error}；eastmoney={fallback_error}"
            ) from fallback_error

    if hasattr(frame, "to_dict"):
        rows = frame.to_dict("records")
    elif isinstance(frame, list):
        rows = frame
    else:
        raise AkshareSourceError(f"AKShare 板块接口返回格式不可识别：{method_name}")
    return [row for row in rows if isinstance(row, dict)]


def collect_sector_records(
    client: Any,
    method_name: str,
    source_type: str,
    events: list[dict[str, str]],
) -> list[dict[str, Any]]:
    try:
        rows = get_sector_rows(client, method_name)
    except AkshareSourceError as error:
        # 概念和行业板块互为补充，单侧失败不阻断另一侧已有板块事实进入快照。
        events.append(
            {
                "title": f"{source_type} 板块采集失败",
                "source": "akshare",
                "note": str(error),
            }
        )
        return []
    return select_hot_sector_records(rows, source_type)


def request_eastmoney_sector_rows(method_name: str) -> list[dict[str, Any]]:
    config = EASTMONEY_SECTOR_CONFIGS.get(method_name)
    if config is None:
        raise AkshareSourceError(f"东方财富板块备用路径缺少接口映射：{method_name}")

    # 板块备用路径只取东方财富板块列表事实字段，避免通过 AKShare requests 继承环境代理。
    query = urllib.parse.urlencode(
        {
            "pn": "1",
            "pz": "500",
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": config["fs"],
            "fields": "f2,f3,f4,f8,f12,f14,f20,f104,f105,f128,f136",
        }
    )
    payload = request_json_without_environment_proxy(f"{config['url']}?{query}")
    diff_items = payload.get("data", {}).get("diff", [])
    if not isinstance(diff_items, list):
        raise AkshareSourceError("东方财富板块备用路径返回格式不可识别")

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(diff_items, start=1):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "排名": index,
                "板块名称": item.get("f14"),
                "板块代码": item.get("f12"),
                "最新价": item.get("f2"),
                "涨跌额": item.get("f4"),
                "涨跌幅": item.get("f3"),
                "总市值": item.get("f20"),
                "换手率": item.get("f8"),
                "上涨家数": item.get("f104"),
                "下跌家数": item.get("f105"),
                "领涨股票": item.get("f128"),
                "领涨股票-涨跌幅": item.get("f136"),
            }
        )

    if not rows:
        raise AkshareSourceError("东方财富板块备用路径未返回有效板块")
    return rows


def select_hot_sector_records(rows: list[dict[str, Any]], source_type: str, limit: int = 5) -> list[dict[str, Any]]:
    ranked_rows = sorted(
        rows,
        key=lambda row: parse_decimal(get_first_value(row, ("涨跌幅",))) or Decimal("-999999"),
        reverse=True,
    )
    sectors: list[dict[str, Any]] = []
    for row in ranked_rows[:limit]:
        sectors.append(
            {
                "name": format_optional_text(get_first_value(row, ("板块名称", "板块"))),
                "code": format_optional_text(get_first_value(row, ("板块代码",))),
                "source_type": source_type,
                "strength": "按涨跌幅排序",
                "change_percent": decimal_to_number(parse_decimal(get_first_value(row, ("涨跌幅",)))),
                "market_value": decimal_to_number(parse_decimal(get_first_value(row, ("总市值",)))),
                "turnover": decimal_to_number(parse_decimal(get_first_value(row, ("总成交额",)))),
                "net_inflow": decimal_to_number(parse_decimal(get_first_value(row, ("净流入",)))),
                "total_volume": decimal_to_number(parse_decimal(get_first_value(row, ("总成交量",)))),
                "turnover_rate": decimal_to_number(parse_decimal(get_first_value(row, ("换手率",)))),
                "up_count": decimal_to_number(parse_decimal(get_first_value(row, ("上涨家数",)))),
                "down_count": decimal_to_number(parse_decimal(get_first_value(row, ("下跌家数",)))),
                "leading_stock": format_optional_text(get_first_value(row, ("领涨股票", "领涨股"))),
                "leading_stock_change_percent": decimal_to_number(
                    parse_decimal(get_first_value(row, ("领涨股票-涨跌幅", "领涨股-涨跌幅")))
                ),
            }
        )
    return sectors


def get_sentiment_rows(client: Any, method_name: str, query_date: str) -> list[dict[str, Any]]:
    try:
        frame = getattr(client, method_name)(date=query_date)
    except Exception as error:  # noqa: BLE001
        raise AkshareSourceError(f"AKShare 短线情绪接口调用失败：{method_name}：{error}") from error

    if hasattr(frame, "to_dict"):
        rows = frame.to_dict("records")
    elif isinstance(frame, list):
        rows = frame
    else:
        raise AkshareSourceError(f"AKShare 短线情绪接口返回格式不可识别：{method_name}")
    return [row for row in rows if isinstance(row, dict)]


def get_index_rows_from_fallback_sources(
    symbol: str,
    primary_error: Exception,
    events: list[dict[str, str]] | None,
) -> list[dict[str, Any]]:
    try:
        rows = request_eastmoney_index_rows(symbol)
    except Exception as eastmoney_error:  # noqa: BLE001
        try:
            rows = request_tencent_index_rows(symbol)
        except Exception as tencent_error:  # noqa: BLE001
            raise AkshareSourceError(
                f"AKShare 指数接口调用失败且备用路径也失败：{symbol}："
                f"akshare={primary_error}；eastmoney={eastmoney_error}；tencent={tencent_error}"
            ) from tencent_error

        if events is not None:
            # 腾讯兜底只在 AKShare 和东方财富都失败时启用，避免单一接口中断阻塞市场层证据采集。
            events.append(
                {
                    "title": f"{symbol} 使用腾讯行情备用路径",
                    "source": "tencent",
                    "note": (
                        "AKShare stock_zh_index_daily_em 和东方财富 K 线直连失败后，"
                        f"使用腾讯指数 K 线接口；原始错误：{primary_error}；东方财富错误：{eastmoney_error}"
                    ),
                }
            )
        return rows

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

    # 用户本机验证完整 URL 直连可用；这里保持逗号不编码，避免退回到 AKShare 失败时的请求形态。
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}"
        "&fields1=f1,f2,f3,f4,f5"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        "&klt=101&fqt=0&beg=19900101&end=20500101"
    )
    payload = request_json_without_environment_proxy(url)
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
                "__amount_source": "eastmoney_index_kline_amount",
            }
        )

    if not rows:
        raise AkshareSourceError("东方财富备用路径未返回有效 K 线")
    return rows


def request_tencent_index_rows(symbol: str) -> list[dict[str, Any]]:
    tencent_symbol = TENCENT_SYMBOLS.get(symbol)
    if tencent_symbol is None:
        raise AkshareSourceError(f"腾讯备用路径缺少指数映射：{symbol}")

    # 腾讯路径作为第二兜底，只采集指数日线 K 线，不扩大到情绪、板块或个股扫描。
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tencent_symbol},day,,,320,qfq"
    payload = request_json_without_environment_proxy(url)
    symbol_data = payload.get("data", {}).get(tencent_symbol, {})
    klines = symbol_data.get("day") or symbol_data.get("qfqday") or []
    if not isinstance(klines, list):
        raise AkshareSourceError("腾讯备用路径返回的 K 线格式不可识别")

    rows: list[dict[str, Any]] = []
    for item in klines:
        if not isinstance(item, list) or len(item) < 6:
            continue
        row: dict[str, Any] = {
            "date": item[0],
            "open": item[1],
            "close": item[2],
            "high": item[3],
            "low": item[4],
            # 腾讯指数日线第 6 列是成交额，用于补齐市场层总成交额缺口。
            "amount": item[5],
            "__amount_source": "tencent_index_kline_amount",
        }
        if len(item) >= 7:
            row["volume"] = item[6]
        rows.append(row)

    if not rows:
        raise AkshareSourceError("腾讯备用路径未返回有效 K 线")
    return rows


def request_json_without_environment_proxy(url: str) -> dict[str, Any]:
    # 真实数据源兜底请求明确禁用环境代理，避免代理污染导致同一错误在多个备用源间传播。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AkshareSourceError("备用路径返回内容不是 JSON 对象")
    return payload


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


def parse_int(value: Any) -> int | None:
    decimal_value = parse_decimal(value)
    if decimal_value is None:
        return None
    return int(decimal_value)


def format_optional_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value)


def decimal_to_number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)
