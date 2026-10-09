# 本文件负责把 WorkBuddy 通达信连接器的日 K 原始行转换为成交额或三指数历史证据，不直接调用连接器。

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


class TongdaxinSourceError(RuntimeError):
    pass


TONGDAXIN_SOURCE = "workbuddy:tongdaxin.tdx_kline"
TONGDAXIN_AMOUNT_SOURCE = f"{TONGDAXIN_SOURCE}:000002.Amount+399106.Amount"
TONGDAXIN_BREADTH_SOURCE = f"{TONGDAXIN_SOURCE}:000002.up/down+399106.up/down"
MARKET_QUOTE_FALLBACK_QUALITY = "market_quote_fallback"
MARKET_BREADTH_QUOTE_QUALITY = "market_quote_breadth"
TONGDAXIN_INDEX_HISTORY_QUALITY = "connector_daily_kline_sequence"
TONGDAXIN_INDEX_HISTORY_DAYS = 5

TONGDAXIN_QUERY_CONFIGS = {
    "000001": {"setcode": 1},
    "000002": {"setcode": 1},
    "399001": {"setcode": 0},
    "399106": {"setcode": 0},
}

TONGDAXIN_INDEX_HISTORY_QUERY_CONFIGS = {
    "000001": {"setcode": 1, "symbol": "sh000001", "name": "上证指数"},
    "399001": {"setcode": 0, "symbol": "sz399001", "name": "深证成指"},
    "399006": {"setcode": 0, "symbol": "sz399006", "name": "创业板指"},
}


# 指数历史导入只接受三大指数最近五根目标日及之前的日 K，成交额和未定义字段不进入标准化结果。
def build_tongdaxin_index_history_evidence(
    trade_date: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        target_date = date.fromisoformat(trade_date)
    except ValueError as error:
        raise TongdaxinSourceError(f"交易日期格式无效：{trade_date}") from error

    queries = payload.get("queries")
    if not isinstance(queries, list):
        raise TongdaxinSourceError("通达信原始文件必须包含 queries 数组。")

    expected_codes = set(TONGDAXIN_INDEX_HISTORY_QUERY_CONFIGS)
    if len(queries) != len(expected_codes):
        raise TongdaxinSourceError("通达信指数历史证据必须恰好包含 3 个固定代码。")

    target_key = target_date.strftime("%Y%m%d")
    selected_by_code: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for query in queries:
        if not isinstance(query, dict):
            raise TongdaxinSourceError("queries 中的每一项都必须是 JSON 对象。")

        code = query.get("code")
        if not isinstance(code, str) or code not in expected_codes:
            raise TongdaxinSourceError(f"通达信指数历史查询代码不在固定范围内：{code}")
        if code in selected_by_code:
            raise TongdaxinSourceError(f"通达信指数历史查询代码重复：{code}")

        config = TONGDAXIN_INDEX_HISTORY_QUERY_CONFIGS[code]
        setcode = parse_integer(query.get("setcode"), f"{code}.setcode")
        period = parse_integer(query.get("period"), f"{code}.period")
        tq_flag = parse_integer(query.get("tqFlag"), f"{code}.tqFlag")
        if setcode != config["setcode"]:
            raise TongdaxinSourceError(f"{code}.setcode 必须为 {config['setcode']}。")
        if period != 4:
            raise TongdaxinSourceError(f"{code}.period 必须为 4（日线）。")
        if tq_flag != 0:
            raise TongdaxinSourceError(f"{code}.tqFlag 必须为 0（不复权）。")

        rows = query.get("Rows")
        if not isinstance(rows, list) or not rows:
            raise TongdaxinSourceError(f"{code}.Rows 必须是非空数组。")
        selected_by_code[code] = select_history_rows(code, rows, target_key)

    date_sequences = {
        code: tuple(row_date for row_date, _row in rows)
        for code, rows in selected_by_code.items()
    }
    expected_dates = date_sequences["000001"]
    if any(dates != expected_dates for dates in date_sequences.values()):
        raise TongdaxinSourceError("通达信三大指数最近五根日 K 的日期序列必须完全一致。")

    records: list[dict[str, Any]] = []
    for position, row_date in enumerate(expected_dates):
        indices: list[dict[str, Any]] = []
        for code, config in TONGDAXIN_INDEX_HISTORY_QUERY_CONFIGS.items():
            row = selected_by_code[code][position][1]
            close = parse_positive_decimal(row.get("Close"), f"{code}.{row_date}.Close")
            indices.append(
                {
                    "code": code,
                    "symbol": config["symbol"],
                    "name": config["name"],
                    "setcode": config["setcode"],
                    "close": decimal_to_number(close),
                    "unit": "point",
                }
            )
        records.append(
            {
                "sample_date": format_row_date(row_date),
                "indices": indices,
            }
        )

    return {
        "schema_version": 1,
        "trade_date": trade_date,
        "sample_date": trade_date,
        "source": TONGDAXIN_SOURCE,
        "quality": TONGDAXIN_INDEX_HISTORY_QUALITY,
        "period": 4,
        "tq_flag": 0,
        "continuity_basis": "matching_ordered_daily_rows_across_three_indices",
        "sample_dates": [format_row_date(row_date) for row_date in expected_dates],
        "records": records,
    }


# 备用导入只接受四个固定代码的目标日日线；指数收盘仅用于和已有快照交叉核对。
def build_tongdaxin_market_evidence(trade_date: str, payload: dict[str, Any]) -> dict[str, Any]:
    rows_by_code, target_key = select_fixed_market_rows(trade_date, payload)

    closes = {
        code: parse_positive_decimal(row.get("Close"), f"{code}.{target_key}.Close")
        for code, row in rows_by_code.items()
    }

    # 成交额只使用连接器说明为最终可用值的 Rows.Amount，不采用 RawAmount 或未定义字段。
    shanghai_amount = parse_yuan_amount(rows_by_code["000002"].get("Amount"), "000002.Amount")
    shenzhen_component_amount = parse_yuan_amount(
        rows_by_code["399001"].get("Amount"),
        "399001.Amount",
    )
    shenzhen_amount = parse_yuan_amount(rows_by_code["399106"].get("Amount"), "399106.Amount")
    # 深成指和深证综指返回相同聚合字段时才接受深市行情成交额，避免把成分指数金额误当全市场值。
    if shenzhen_component_amount != shenzhen_amount:
        raise TongdaxinSourceError("399001.Amount 与 399106.Amount 不一致，无法确认深市行情成交额口径。")
    total_amount = shanghai_amount + shenzhen_amount

    return {
        "source": TONGDAXIN_SOURCE,
        "sample_date": trade_date,
        "index_close_checks": {
            "上证指数": decimal_to_number(closes["000001"]),
            "深证成指": decimal_to_number(closes["399001"]),
        },
        "total_amount": decimal_to_number(total_amount),
        "total_amount_source": TONGDAXIN_AMOUNT_SOURCE,
        "total_amount_unit": "CNY",
        "total_amount_quality": MARKET_QUOTE_FALLBACK_QUALITY,
        "total_amount_calculation": "000002.Rows.Amount+399106.Rows.Amount",
        "total_amount_breakdown": {
            "shanghai_quote_amount": decimal_to_number(shanghai_amount),
            "shenzhen_quote_amount": decimal_to_number(shenzhen_amount),
        },
    }


# 涨跌家数与成交额分别导入；只接受固定沪深行情统计字段，不使用成分指数自行推算。
def build_tongdaxin_market_breadth_evidence(
    trade_date: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    rows_by_code, target_key = select_fixed_market_rows(trade_date, payload)
    closes = {
        code: parse_positive_decimal(row.get("Close"), f"{code}.{target_key}.Close")
        for code, row in rows_by_code.items()
    }
    shanghai_advance = parse_nonnegative_integer_decimal(
        rows_by_code["000002"].get("up"),
        "000002.up",
    )
    shanghai_decline = parse_nonnegative_integer_decimal(
        rows_by_code["000002"].get("down"),
        "000002.down",
    )
    shenzhen_component_advance = parse_nonnegative_integer_decimal(
        rows_by_code["399001"].get("up"),
        "399001.up",
    )
    shenzhen_component_decline = parse_nonnegative_integer_decimal(
        rows_by_code["399001"].get("down"),
        "399001.down",
    )
    shenzhen_advance = parse_nonnegative_integer_decimal(
        rows_by_code["399106"].get("up"),
        "399106.up",
    )
    shenzhen_decline = parse_nonnegative_integer_decimal(
        rows_by_code["399106"].get("down"),
        "399106.down",
    )
    if (
        shenzhen_component_advance != shenzhen_advance
        or shenzhen_component_decline != shenzhen_decline
    ):
        raise TongdaxinSourceError(
            "399001 与 399106 的 up/down 不一致，无法确认深市行情涨跌家数口径。"
        )

    return {
        "source": TONGDAXIN_SOURCE,
        "sample_date": trade_date,
        "index_close_checks": {
            "上证指数": decimal_to_number(closes["000001"]),
            "深证成指": decimal_to_number(closes["399001"]),
        },
        "up_count": decimal_to_number(shanghai_advance + shenzhen_advance),
        "down_count": decimal_to_number(shanghai_decline + shenzhen_decline),
        "advance_decline_source": TONGDAXIN_BREADTH_SOURCE,
        "advance_decline_quality": MARKET_BREADTH_QUOTE_QUALITY,
        "advance_decline_calculation": "000002.Rows.up/down+399106.Rows.up/down",
        "advance_decline_breakdown": {
            "shanghai_advance_count": decimal_to_number(shanghai_advance),
            "shanghai_decline_count": decimal_to_number(shanghai_decline),
            "shenzhen_advance_count": decimal_to_number(shenzhen_advance),
            "shenzhen_decline_count": decimal_to_number(shenzhen_decline),
        },
        "advance_decline_note": (
            "使用上证 A 股指数与深证综指的通达信行情统计字段；属于行情口径，"
            "不替代交易所官方全市场统计。"
        ),
    }


# 两条市场导入共用固定代码、目标日和日线参数校验，避免成交额与涨跌家数出现不同审计边界。
def select_fixed_market_rows(
    trade_date: str,
    payload: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], str]:
    try:
        target_date = date.fromisoformat(trade_date)
    except ValueError as error:
        raise TongdaxinSourceError(f"交易日期格式无效：{trade_date}") from error

    queries = payload.get("queries")
    if not isinstance(queries, list):
        raise TongdaxinSourceError("通达信原始文件必须包含 queries 数组。")

    expected_codes = set(TONGDAXIN_QUERY_CONFIGS)
    if len(queries) != len(expected_codes):
        raise TongdaxinSourceError("通达信成交额备用证据必须恰好包含 4 个固定代码。")

    rows_by_code: dict[str, dict[str, Any]] = {}
    target_key = target_date.strftime("%Y%m%d")
    for query in queries:
        if not isinstance(query, dict):
            raise TongdaxinSourceError("queries 中的每一项都必须是 JSON 对象。")

        code = query.get("code")
        if not isinstance(code, str) or code not in expected_codes:
            raise TongdaxinSourceError(f"通达信查询代码不在固定范围内：{code}")
        if code in rows_by_code:
            raise TongdaxinSourceError(f"通达信查询代码重复：{code}")

        config = TONGDAXIN_QUERY_CONFIGS[code]
        setcode = parse_integer(query.get("setcode"), f"{code}.setcode")
        period = parse_integer(query.get("period"), f"{code}.period")
        tq_flag = parse_integer(query.get("tqFlag"), f"{code}.tqFlag")
        if setcode != config["setcode"]:
            raise TongdaxinSourceError(f"{code}.setcode 必须为 {config['setcode']}。")
        if period != 4:
            raise TongdaxinSourceError(f"{code}.period 必须为 4（日线）。")
        if tq_flag != 0:
            raise TongdaxinSourceError(f"{code}.tqFlag 必须为 0（不复权）。")

        rows = query.get("Rows")
        if not isinstance(rows, list) or not rows:
            raise TongdaxinSourceError(f"{code}.Rows 必须是非空数组。")
        rows_by_code[code] = select_target_row(code, rows, target_key)

    return rows_by_code, target_key


# 每个查询必须精确包含目标日；同一日期重复时无法确认原始行，直接拒绝。
def select_target_row(
    code: str,
    rows: list[Any],
    target_key: str,
) -> dict[str, Any]:
    dated_rows = normalize_dated_rows(code, rows)
    selected_rows = [row for row_date, row in dated_rows if row_date == target_key]
    if len(selected_rows) != 1:
        raise TongdaxinSourceError(f"{code}.Rows 未精确返回目标交易日 {target_key}。")
    return selected_rows[0]


# 连续性以连接器同一查询返回的相邻日 K 行为准；允许响应同时包含目标日之后的新行情。
def select_history_rows(
    code: str,
    rows: list[Any],
    target_key: str,
) -> list[tuple[str, dict[str, Any]]]:
    dated_rows = normalize_dated_rows(code, rows)
    target_positions = [
        position
        for position, (row_date, _row) in enumerate(dated_rows)
        if row_date == target_key
    ]
    if len(target_positions) != 1:
        raise TongdaxinSourceError(f"{code}.Rows 未精确返回目标交易日 {target_key}。")
    target_position = target_positions[0]
    start_position = target_position - TONGDAXIN_INDEX_HISTORY_DAYS + 1
    if start_position < 0:
        raise TongdaxinSourceError(
            f"{code}.Rows 在目标交易日之前不足 {TONGDAXIN_INDEX_HISTORY_DAYS - 1} 根日 K。"
        )
    return dated_rows[start_position : target_position + 1]


# 所有日 K 入口共用日期唯一性校验，避免不同导入路径对重复行采取不同口径。
def normalize_dated_rows(
    code: str,
    rows: list[Any],
) -> list[tuple[str, dict[str, Any]]]:
    dated_rows: list[tuple[str, dict[str, Any]]] = []
    seen_dates: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TongdaxinSourceError(f"{code}.Rows 中存在非对象行。")
        row_date = row.get("Data")
        if not isinstance(row_date, str) or len(row_date) != 8 or not row_date.isdigit():
            raise TongdaxinSourceError(f"{code}.Rows.Data 必须是 YYYYMMDD 字符串。")
        try:
            date.fromisoformat(f"{row_date[:4]}-{row_date[4:6]}-{row_date[6:]}")
        except ValueError as error:
            raise TongdaxinSourceError(f"{code}.Rows.Data 不是有效日期：{row_date}") from error
        if row_date in seen_dates:
            raise TongdaxinSourceError(f"{code}.Rows.Data 日期重复：{row_date}")
        seen_dates.add(row_date)
        dated_rows.append((row_date, row))

    dated_rows.sort(key=lambda item: item[0])
    return dated_rows


def format_row_date(row_date: str) -> str:
    return f"{row_date[:4]}-{row_date[4:6]}-{row_date[6:]}"


def parse_integer(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TongdaxinSourceError(f"{field_name} 必须是整数。")
    return value


def parse_positive_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise TongdaxinSourceError(f"{field_name} 必须是大于 0 的有限数值。")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise TongdaxinSourceError(f"{field_name} 必须是大于 0 的有限数值。") from error
    if not parsed.is_finite() or parsed <= 0:
        raise TongdaxinSourceError(f"{field_name} 必须是大于 0 的有限数值。")
    return parsed


def parse_yuan_amount(value: Any, field_name: str) -> Decimal:
    parsed = parse_positive_decimal(value, field_name)
    if parsed != parsed.to_integral_value():
        raise TongdaxinSourceError(f"{field_name} 必须是以人民币元表示的整数。")
    return parsed


def parse_nonnegative_integer_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise TongdaxinSourceError(f"{field_name} 必须是大于等于 0 的有限整数。")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise TongdaxinSourceError(f"{field_name} 必须是大于等于 0 的有限整数。") from error
    if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
        raise TongdaxinSourceError(f"{field_name} 必须是大于等于 0 的有限整数。")
    return parsed


def decimal_to_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)
