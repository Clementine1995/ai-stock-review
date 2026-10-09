# 本文件负责把最多三个榜单板块通过 AKShare 名称列表唯一映射到日 K，不做模糊匹配或成分股扫描。

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import math
from typing import Any

from stock_review.evidence.akshare_source import import_akshare_client


AKSHARE_SECTOR_KLINE_SOURCE = "akshare:ranked_sector_daily_kline"
MAX_SECTOR_KLINE_OBJECTS = 3
MIN_LOOKBACK_BARS = 5
MAX_LOOKBACK_BARS = 10
POSITIVE_SECTOR_SOURCE_TYPES = frozenset(
    {
        "hhxg_hot_theme",
        "hhxg_strong_sector",
        "hhxg_strong_industry",
        "hhxg_strong_concept",
        "concept",
        "industry",
        "industry_ths",
    }
)
SECTOR_HISTORY_SOURCES = {
    "concept": "akshare:stock_board_concept_hist_em",
    "industry": "akshare:stock_board_industry_hist_em",
}


class SectorKlineSourceError(RuntimeError):
    pass


# 只消费明确正向榜；跨榜单按 rank 升序和 Snapshot 原顺序选择，名称去重后硬截断为三个。
def select_ranked_sector_objects(
    sectors: list[Any],
) -> list[dict[str, Any]]:
    if not isinstance(sectors, list):
        raise SectorKlineSourceError("Snapshot.sectors 必须是数组。")
    eligible: list[tuple[int, int, str, dict[str, Any]]] = []
    for position, raw_object in enumerate(sectors):
        if not isinstance(raw_object, dict):
            continue
        if not is_positive_ranked_sector(raw_object):
            continue
        name = normalize_text(raw_object.get("name"))
        rank = parse_positive_rank(raw_object.get("rank"))
        if name is None or rank is None:
            continue
        original = dict(raw_object)
        eligible.append((rank, position, name, original))

    selected: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for rank, position, name, original in sorted(
        eligible,
        key=lambda item: (item[0], item[1]),
    ):
        if name in seen_names:
            continue
        seen_names.add(name)
        selected.append(
            {
                "name": name,
                "source_type": normalize_text(original.get("source_type")),
                "ranking_group": normalize_text(original.get("ranking_group")),
                "rank": rank,
                "snapshot_position": position,
                "original_ranking_object": original,
            }
        )
        if len(selected) == MAX_SECTOR_KLINE_OBJECTS:
            break
    return selected


# 名称列表各请求一次；只有名称恰好命中一个类型且一个代码时，才请求对应历史详情。
def collect_akshare_sector_kline_evidence(
    review_date: str,
    ranked_objects: list[dict[str, Any]],
    lookback_bars: int,
    ak_client: Any | None = None,
) -> dict[str, Any]:
    target_date = validate_review_date(review_date)
    validate_lookback_bars(lookback_bars)
    if not ranked_objects or len(ranked_objects) > MAX_SECTOR_KLINE_OBJECTS:
        raise SectorKlineSourceError("板块日 K 必须包含 1 至 3 个明确排行对象。")

    client = load_akshare_client(ak_client)
    board_maps = {
        "concept": request_board_name_map(client, "concept"),
        "industry": request_board_name_map(client, "industry"),
    }
    start_date = (target_date - timedelta(days=45)).strftime("%Y%m%d")
    end_date = target_date.strftime("%Y%m%d")
    records: list[dict[str, Any]] = []
    for ranked_object in ranked_objects:
        name = normalize_text(ranked_object.get("name"))
        if name is None:
            raise SectorKlineSourceError("板块排行对象缺少精确名称。")
        mapping_matches = [
            board_type
            for board_type in ("concept", "industry")
            if name in board_maps[board_type]
        ]
        base_record = {
            "name": name,
            "source_type": ranked_object.get("source_type"),
            "ranking_group": ranked_object.get("ranking_group"),
            "rank": ranked_object.get("rank"),
            "snapshot_position": ranked_object.get("snapshot_position"),
            "original_ranking_object": ranked_object.get("original_ranking_object"),
            "mapping_basis": "exact_unique_name",
            "mapping_matches": mapping_matches,
        }
        if not mapping_matches:
            records.append(
                {
                    **base_record,
                    "status": "unavailable",
                    "resolved_board_type": None,
                    "resolved_board_code": None,
                    "history_source": None,
                    "period": "daily",
                    "adjustment": "none",
                    "bar_count": 0,
                    "bars": [],
                    "note": "名称未在 AKShare 概念或行业板块名称列表中精确命中。",
                }
            )
            continue
        if len(mapping_matches) > 1:
            records.append(
                {
                    **base_record,
                    "status": "ambiguous",
                    "resolved_board_type": None,
                    "resolved_board_code": None,
                    "history_source": None,
                    "period": "daily",
                    "adjustment": "none",
                    "bar_count": 0,
                    "bars": [],
                    "note": "名称同时命中概念和行业板块，无法唯一确定历史接口。",
                }
            )
            continue

        board_type = mapping_matches[0]
        board_codes = board_maps[board_type][name]
        if len(board_codes) != 1:
            records.append(
                {
                    **base_record,
                    "status": "ambiguous",
                    "resolved_board_type": board_type,
                    "resolved_board_code": None,
                    "history_source": None,
                    "period": "daily",
                    "adjustment": "none",
                    "bar_count": 0,
                    "bars": [],
                    "note": "同一类型名称对应多个板块代码，无法唯一确定历史对象。",
                }
            )
            continue
        board_code = next(iter(board_codes))
        rows = request_sector_history(
            client,
            name,
            board_type,
            board_code,
            start_date,
            end_date,
        )
        bars = normalize_sector_bars(rows, target_date, lookback_bars, name)
        records.append(
            {
                **base_record,
                "status": "available",
                "resolved_board_type": board_type,
                "resolved_board_code": board_code,
                "history_source": SECTOR_HISTORY_SOURCES[board_type],
                "period": "daily",
                "adjustment": "none",
                "bar_count": len(bars),
                "bars": bars,
                "note": "名称在概念和行业列表中恰好唯一命中一个类型。",
            }
        )

    available_count = sum(record["status"] == "available" for record in records)
    return {
        "schema_version": 1,
        "review_date": review_date,
        "sample_date": review_date,
        "source": AKSHARE_SECTOR_KLINE_SOURCE,
        "scope": "ranked_sector_kline_limited",
        "lookback_bars": lookback_bars,
        "max_objects": MAX_SECTOR_KLINE_OBJECTS,
        "selection_basis": (
            "positive_source_type_then_rank_then_snapshot_order_then_exact_unique_name"
        ),
        "eligible_source_types": sorted(POSITIVE_SECTOR_SOURCE_TYPES),
        "mapping_sources": [
            "akshare:stock_board_concept_name_em",
            "akshare:stock_board_industry_name_em",
        ],
        "selected_object_count": len(records),
        "available_count": available_count,
        "unavailable_count": sum(record["status"] == "unavailable" for record in records),
        "ambiguous_count": sum(record["status"] == "ambiguous" for record in records),
        "value_unit_note": "成交额与成交量保留 AKShare 上游原生数值；本地接口文档未声明单位，不另行换算。",
        "records": records,
    }


def request_board_name_map(
    client: Any,
    board_type: str,
) -> dict[str, set[str]]:
    try:
        value = (
            client.stock_board_concept_name_em()
            if board_type == "concept"
            else client.stock_board_industry_name_em()
        )
    except Exception as error:  # noqa: BLE001
        raise SectorKlineSourceError(f"AKShare {board_type} 板块名称列表请求失败：{error}") from error
    rows = normalize_rows(value)
    mapping: dict[str, set[str]] = {}
    for row in rows:
        name = normalize_text(row.get("板块名称"))
        code = normalize_board_code(row.get("板块代码"))
        if name is None or code is None:
            continue
        mapping.setdefault(name, set()).add(code)
    if not mapping:
        raise SectorKlineSourceError(f"AKShare {board_type} 板块名称列表没有有效名称和代码。")
    return mapping


def request_sector_history(
    client: Any,
    name: str,
    board_type: str,
    board_code: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    try:
        if board_type == "concept":
            value = client.stock_board_concept_hist_em(
                symbol=board_code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="",
            )
        else:
            value = client.stock_board_industry_hist_em(
                symbol=board_code,
                start_date=start_date,
                end_date=end_date,
                period="日k",
                adjust="",
            )
    except Exception as error:  # noqa: BLE001
        raise SectorKlineSourceError(
            f"AKShare {board_type} 板块 {name} 日 K 请求失败：{error}"
        ) from error
    return normalize_rows(value)


def normalize_sector_bars(
    rows: list[dict[str, Any]],
    target_date: date,
    lookback_bars: int,
    name: str,
) -> list[dict[str, Any]]:
    rows_by_date: dict[date, dict[str, Any]] = {}
    for row in rows:
        sample_date = parse_date_value(row.get("日期", row.get("date")))
        if sample_date is None or sample_date > target_date:
            continue
        if sample_date in rows_by_date:
            raise SectorKlineSourceError(f"板块 {name} 日 K 存在重复日期：{sample_date}。")
        rows_by_date[sample_date] = row
    ordered_dates = sorted(rows_by_date)
    if not ordered_dates or ordered_dates[-1] != target_date:
        actual = ordered_dates[-1].isoformat() if ordered_dates else "无有效日期"
        raise SectorKlineSourceError(
            f"板块 {name} 日 K 必须以复盘日期 {target_date.isoformat()} 结束，实际为 {actual}。"
        )
    if len(ordered_dates) < lookback_bars:
        raise SectorKlineSourceError(
            f"板块 {name} 只有 {len(ordered_dates)} 根有效日 K，少于要求的 {lookback_bars} 根。"
        )

    bars: list[dict[str, Any]] = []
    for sample_date in ordered_dates[-lookback_bars:]:
        row = rows_by_date[sample_date]
        open_price = parse_positive_decimal(row.get("开盘", row.get("open")), f"{name}.{sample_date}.open")
        high_price = parse_positive_decimal(row.get("最高", row.get("high")), f"{name}.{sample_date}.high")
        low_price = parse_positive_decimal(row.get("最低", row.get("low")), f"{name}.{sample_date}.low")
        close_price = parse_positive_decimal(row.get("收盘", row.get("close")), f"{name}.{sample_date}.close")
        if high_price < max(open_price, close_price) or low_price > min(open_price, close_price):
            raise SectorKlineSourceError(f"板块 {name} 在 {sample_date} 的 OHLC 关系无效。")
        volume = parse_optional_nonnegative_decimal(
            row.get("成交量", row.get("volume")),
            f"{name}.{sample_date}.volume",
        )
        amount = parse_optional_nonnegative_decimal(
            row.get("成交额", row.get("amount")),
            f"{name}.{sample_date}.amount",
        )
        if volume is None and amount is None:
            raise SectorKlineSourceError(f"板块 {name} 在 {sample_date} 同时缺少成交额和成交量。")
        bars.append(
            {
                "date": sample_date.isoformat(),
                "open": decimal_to_number(open_price),
                "high": decimal_to_number(high_price),
                "low": decimal_to_number(low_price),
                "close": decimal_to_number(close_price),
                "volume": decimal_to_number(volume) if volume is not None else None,
                "amount": decimal_to_number(amount) if amount is not None else None,
            }
        )
    return bars


def parse_positive_rank(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0 or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def parse_positive_decimal(value: Any, label: str) -> Decimal:
    parsed = parse_decimal(value)
    if parsed is None or parsed <= 0:
        raise SectorKlineSourceError(f"{label} 必须是正的有限数值。")
    return parsed


def parse_optional_nonnegative_decimal(value: Any, label: str) -> Decimal | None:
    if is_missing_value(value):
        return None
    parsed = parse_decimal(value)
    if parsed is None or parsed < 0:
        raise SectorKlineSourceError(f"{label} 必须是非负有限数值。")
    return parsed


def parse_decimal(value: Any) -> Decimal | None:
    if is_missing_value(value) or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def parse_date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if is_missing_value(value):
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def normalize_rows(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        value = value.to_dict("records")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise SectorKlineSourceError("AKShare 板块日 K 返回格式必须是对象数组。")
    return value


def normalize_text(value: Any) -> str | None:
    if is_missing_value(value):
        return None
    text = str(value).strip()
    return text or None


def normalize_board_code(value: Any) -> str | None:
    text = normalize_text(value)
    if text is None or not text.startswith("BK") or not text[2:].isdigit():
        return None
    return text


# weak 标记无论来自来源类型还是榜单分组都优先排除，避免弱势榜误入核心板块证据。
def is_positive_ranked_sector(raw_object: dict[str, Any]) -> bool:
    source_type = normalize_text(raw_object.get("source_type"))
    ranking_group = normalize_text(raw_object.get("ranking_group"))
    normalized_source_type = source_type.lower() if source_type is not None else ""
    normalized_ranking_group = ranking_group.lower() if ranking_group is not None else ""
    if "weak" in normalized_source_type or "weak" in normalized_ranking_group:
        return False
    return normalized_source_type in POSITIVE_SECTOR_SOURCE_TYPES


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


def validate_review_date(review_date: str) -> date:
    try:
        return date.fromisoformat(review_date)
    except (TypeError, ValueError) as error:
        raise SectorKlineSourceError("复盘日期必须是 YYYY-MM-DD。") from error


def validate_lookback_bars(lookback_bars: int) -> None:
    if (
        isinstance(lookback_bars, bool)
        or not isinstance(lookback_bars, int)
        or not MIN_LOOKBACK_BARS <= lookback_bars <= MAX_LOOKBACK_BARS
    ):
        raise SectorKlineSourceError("lookback_bars 只能是 5 至 10 的整数。")


def load_akshare_client(ak_client: Any | None) -> Any:
    if ak_client is not None:
        return ak_client
    try:
        return import_akshare_client()
    except Exception as error:  # noqa: BLE001
        raise SectorKlineSourceError(f"无法加载 AKShare 板块日 K 客户端：{error}") from error


def decimal_to_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)
