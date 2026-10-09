# 本文件负责把单个 A 股复盘日的公开龙虎榜日榜转换为有限候选证据，不负责选股或识别游资身份。

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import math
from typing import Any


LONGHUBANG_SOURCE = "public:daily_longhubang"
LONGHUBANG_SCOPE = "daily_list_limited_candidates"
NOT_LISTED_NOTE = "未出现在当日公开龙虎榜仅表示没有当日日榜记录，不构成负面证据。"


class LonghubangSourceError(RuntimeError):
    pass


# 外部客户端只负责返回当日日榜与可选席位明细；标准化和有限候选过滤由本模块完成。
def collect_daily_longhubang_evidence(
    review_date: str,
    candidates: list[dict[str, Any]],
    client: Any,
) -> dict[str, Any]:
    normalized_candidates = normalize_candidates(candidates)
    if not normalized_candidates:
        raise LonghubangSourceError("龙虎榜证据要求至少一个有限候选。")

    try:
        daily_rows = normalize_rows(client.fetch_longhubang_daily(review_date), "龙虎榜日榜")
        fetch_seats = getattr(client, "fetch_longhubang_seats", None)
        seat_rows = normalize_rows(fetch_seats(review_date), "龙虎榜席位明细") if callable(fetch_seats) else []
    except LonghubangSourceError:
        raise
    except Exception as error:  # noqa: BLE001
        raise LonghubangSourceError(f"龙虎榜公开日榜请求失败：{error}") from error

    source = normalize_text(getattr(client, "source_name", None)) or LONGHUBANG_SOURCE
    return build_daily_longhubang_evidence(
        review_date,
        normalized_candidates,
        daily_rows,
        seat_rows,
        source=source,
    )


# 构建函数只消费显式候选代码；榜单中的其它股票仅计数，不进入下游候选范围。
def build_daily_longhubang_evidence(
    review_date: str,
    candidates: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
    seat_rows: list[dict[str, Any]] | None = None,
    *,
    source: str = LONGHUBANG_SOURCE,
) -> dict[str, Any]:
    validate_iso_date(review_date, "复盘日期")
    normalized_candidates = normalize_candidates(candidates)
    if not normalized_candidates:
        raise LonghubangSourceError("龙虎榜证据要求至少一个有限候选。")

    normalized_daily_rows = normalize_rows(daily_rows, "龙虎榜日榜")
    normalized_seat_rows = normalize_rows(seat_rows or [], "龙虎榜席位明细")
    validate_row_dates(review_date, normalized_daily_rows, "龙虎榜日榜")
    validate_row_dates(review_date, normalized_seat_rows, "龙虎榜席位明细")

    daily_by_code = group_rows_by_code(normalized_daily_rows, "龙虎榜日榜")
    seats_by_code = group_rows_by_code(normalized_seat_rows, "龙虎榜席位明细")
    candidate_codes = {candidate["code"] for candidate in normalized_candidates}

    records = [
        build_candidate_record(
            candidate,
            daily_by_code.get(candidate["code"], []),
            seats_by_code.get(candidate["code"], []),
        )
        for candidate in normalized_candidates
    ]
    return {
        "schema_version": 1,
        "review_date": review_date,
        "sample_date": review_date,
        "source": normalize_text(source) or LONGHUBANG_SOURCE,
        "scope": LONGHUBANG_SCOPE,
        "amount_unit": "CNY",
        "candidate_count": len(normalized_candidates),
        "public_listed_count": len(daily_by_code),
        "consumed_listed_count": sum(1 for code in daily_by_code if code in candidate_codes),
        "records": records,
        "not_listed_semantics": NOT_LISTED_NOTE,
    }


def build_candidate_record(
    candidate: dict[str, str],
    daily_rows: list[dict[str, Any]],
    seat_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not daily_rows:
        return {
            "code": candidate["code"],
            "name": candidate["name"],
            "listed": False,
            "status": "not_listed",
            "reason": None,
            "buy_amount": None,
            "sell_amount": None,
            "net_amount": None,
            "institutional_net_amount": None,
            "raw_seat_names": None,
            "note": NOT_LISTED_NOTE,
        }

    source_names = unique_text_values(daily_rows, ("name", "名称", "股票名称"))
    if source_names and any(name != candidate["name"] for name in source_names):
        raise LonghubangSourceError(
            f"龙虎榜代码 {candidate['code']} 的来源名称与有限候选名称不一致。"
        )

    reasons = unique_text_values(
        daily_rows,
        ("reason", "上榜原因", "上榜原因说明", "上榜解读"),
    )
    direct_institutional_net = consistent_amount(
        daily_rows,
        ("institutional_net_amount", "机构席位净额", "机构净买入额"),
        f"{candidate['code']} 机构席位净额",
        allow_negative=True,
    )
    # 原始席位名称保留来源顺序和重复项，避免把多个“机构专用”席位误合并为一个席位。
    seat_names = text_values(seat_rows, ("seat_name", "席位名称", "交易营业部名称"))
    institutional_net = (
        direct_institutional_net
        if direct_institutional_net is not None
        else aggregate_institutional_net(seat_rows, candidate["code"])
    )
    return {
        "code": candidate["code"],
        "name": candidate["name"],
        "listed": True,
        "status": "listed",
        "reason": "；".join(reasons) if reasons else None,
        "buy_amount": consistent_amount(
            daily_rows,
            ("buy_amount", "买入金额", "买入总额", "买入额", "龙虎榜买入额"),
            f"{candidate['code']} 买入总额",
        ),
        "sell_amount": consistent_amount(
            daily_rows,
            ("sell_amount", "卖出金额", "卖出总额", "卖出额", "龙虎榜卖出额"),
            f"{candidate['code']} 卖出总额",
        ),
        "net_amount": consistent_amount(
            daily_rows,
            ("net_amount", "净买入额", "买卖净额", "净额", "净买额", "龙虎榜净买额"),
            f"{candidate['code']} 买卖净额",
            allow_negative=True,
        ),
        "institutional_net_amount": institutional_net,
        "raw_seat_names": seat_names or None,
        "note": None,
    }


# 机构席位只接受来源显式标记或原始名称“机构专用”，不维护券商营业部或游资别名字典。
def aggregate_institutional_net(seat_rows: list[dict[str, Any]], code: str) -> int | float | None:
    if not seat_rows:
        return None
    institutional_rows = [row for row in seat_rows if is_institutional_seat(row)]
    if not institutional_rows:
        return None

    values = [
        first_amount(
            row,
            ("net_amount", "净买入额", "买卖净额", "净额"),
            f"{code} 机构席位净额",
            allow_negative=True,
        )
        for row in institutional_rows
    ]
    if any(value is None for value in values):
        return None
    return decimal_to_number(sum((Decimal(str(value)) for value in values if value is not None), Decimal("0")))


def is_institutional_seat(row: dict[str, Any]) -> bool:
    explicit = first_value(row, ("is_institutional",))
    if isinstance(explicit, bool):
        return explicit
    seat_type = normalize_text(first_value(row, ("seat_type", "席位类型")))
    if seat_type in {"institution", "机构", "机构专用"}:
        return True
    seat_name = normalize_text(first_value(row, ("seat_name", "席位名称", "交易营业部名称")))
    return seat_name == "机构专用"


def normalize_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not isinstance(candidates, list):
        raise LonghubangSourceError("龙虎榜有限候选必须是数组。")
    normalized: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise LonghubangSourceError("龙虎榜有限候选中的每一项都必须是对象。")
        code = normalize_stock_code(candidate.get("code"))
        name = normalize_text(candidate.get("name"))
        if code is None or name is None:
            raise LonghubangSourceError("龙虎榜有限候选必须包含六位股票代码和名称。")
        existing = normalized.get(code)
        if existing is not None and existing["name"] != name:
            raise LonghubangSourceError(f"龙虎榜有限候选代码 {code} 对应多个名称。")
        normalized.setdefault(code, {"code": code, "name": name})
    return list(normalized.values())


def group_rows_by_code(rows: list[dict[str, Any]], label: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = normalize_stock_code(first_value(row, ("code", "代码", "股票代码")))
        if code is None:
            raise LonghubangSourceError(f"{label}存在无法标准化的股票代码。")
        grouped.setdefault(code, []).append(row)
    return grouped


def validate_row_dates(review_date: str, rows: list[dict[str, Any]], label: str) -> None:
    for row in rows:
        value = first_value(row, ("review_date", "trade_date", "交易日期", "日期"))
        if value in (None, ""):
            continue
        normalized = normalize_date_value(value)
        if normalized != review_date:
            raise LonghubangSourceError(
                f"{label}日期与复盘日期不一致：期望 {review_date}，实际 {normalized or value}。"
            )


def normalize_rows(value: Any, label: str) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        value = value.to_dict("records")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise LonghubangSourceError(f"{label}返回格式必须是对象数组。")
    return value


def consistent_amount(
    rows: list[dict[str, Any]],
    aliases: tuple[str, ...],
    label: str,
    *,
    allow_negative: bool = False,
) -> int | float | None:
    values = [first_amount(row, aliases, label, allow_negative=allow_negative) for row in rows]
    present = {Decimal(str(value)) for value in values if value is not None}
    if len(present) > 1:
        raise LonghubangSourceError(f"{label}在同一股票的多条日榜记录中不一致。")
    return decimal_to_number(next(iter(present))) if present else None


def first_amount(
    row: dict[str, Any],
    aliases: tuple[str, ...],
    label: str,
    *,
    allow_negative: bool = False,
) -> int | float | None:
    value = first_value(row, aliases)
    if is_missing_value(value):
        return None
    if isinstance(value, bool):
        raise LonghubangSourceError(f"{label}必须是有限数值。")
    text = str(value).strip().replace(",", "")
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError) as error:
        raise LonghubangSourceError(f"{label}必须是以人民币元表示的有限数值。") from error
    if not parsed.is_finite() or (not allow_negative and parsed < 0):
        raise LonghubangSourceError(f"{label}必须是以人民币元表示的有效数值。")
    return decimal_to_number(parsed)


def unique_text_values(rows: list[dict[str, Any]], aliases: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for row in rows:
        value = normalize_text(first_value(row, aliases))
        if value is not None and value not in values:
            values.append(value)
    return values


def text_values(rows: list[dict[str, Any]], aliases: tuple[str, ...]) -> list[str]:
    return [
        value
        for row in rows
        if (value := normalize_text(first_value(row, aliases))) is not None
    ]


def first_value(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in row:
            return row[alias]
    return None


def normalize_stock_code(value: Any) -> str | None:
    if isinstance(value, bool) or is_missing_value(value):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    text = str(value).strip()
    if not text.isdigit() or not 1 <= len(text) <= 6:
        return None
    return text.zfill(6)


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
        raise LonghubangSourceError(f"{label}必须是 YYYY-MM-DD。") from error


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
