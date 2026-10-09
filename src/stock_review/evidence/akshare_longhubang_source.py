# 本文件负责把 AKShare 龙虎榜日榜和有限候选席位明细映射为标准证据，不采集池外席位历史。

from __future__ import annotations

from datetime import date
from typing import Any

from stock_review.evidence.akshare_source import import_akshare_client
from stock_review.evidence.longhubang_source import (
    LonghubangSourceError,
    build_daily_longhubang_evidence,
    first_amount,
    normalize_candidates,
    normalize_date_value,
    normalize_rows,
    normalize_stock_code,
    normalize_text,
)


AKSHARE_LONGHUBANG_SOURCE = (
    "akshare:stock_lhb_detail_em+stock_lhb_stock_detail_em"
)
AKSHARE_LONGHUBANG_FLAGS = ("买入", "卖出")


# 日榜固定请求单日；席位接口只请求“当日上榜且属于有限候选”的代码。
def collect_akshare_daily_longhubang_evidence(
    review_date: str,
    candidates: list[dict[str, Any]],
    ak_client: Any | None = None,
) -> dict[str, Any]:
    try:
        target_date = date.fromisoformat(review_date)
    except (TypeError, ValueError) as error:
        raise LonghubangSourceError("复盘日期必须是 YYYY-MM-DD。") from error
    normalized_candidates = normalize_candidates(candidates)
    if not normalized_candidates:
        raise LonghubangSourceError("龙虎榜证据要求至少一个有限候选。")

    client = load_akshare_client(ak_client)
    date_key = target_date.strftime("%Y%m%d")
    try:
        raw_daily_rows = normalize_rows(
            client.stock_lhb_detail_em(start_date=date_key, end_date=date_key),
            "AKShare 龙虎榜日榜",
        )
    except LonghubangSourceError:
        raise
    except Exception as error:  # noqa: BLE001
        raise LonghubangSourceError(f"AKShare 龙虎榜日榜请求失败：{error}") from error

    daily_rows = [map_akshare_daily_row(row, review_date) for row in raw_daily_rows]
    candidate_codes = {candidate["code"] for candidate in normalized_candidates}
    listed_candidate_codes = {
        code
        for row in daily_rows
        if (code := normalize_stock_code(row.get("code"))) in candidate_codes
    }

    seat_rows: list[dict[str, Any]] = []
    seen_seats: set[tuple[Any, ...]] = set()
    for candidate in normalized_candidates:
        code = candidate["code"]
        if code not in listed_candidate_codes:
            continue
        for flag in AKSHARE_LONGHUBANG_FLAGS:
            try:
                raw_seat_rows = normalize_rows(
                    client.stock_lhb_stock_detail_em(
                        symbol=code,
                        date=date_key,
                        flag=flag,
                    ),
                    f"AKShare {code} 龙虎榜{flag}席位",
                )
            except LonghubangSourceError:
                raise
            except Exception as error:  # noqa: BLE001
                raise LonghubangSourceError(
                    f"AKShare {code} 龙虎榜{flag}席位请求失败：{error}"
                ) from error

            for raw_row in raw_seat_rows:
                seat = map_akshare_seat_row(raw_row, review_date, code)
                # 买入榜和卖出榜可能返回同一席位及同一组金额，必须在计算机构净额前精确去重。
                identity = (
                    seat["code"],
                    seat["seat_name"],
                    seat["buy_amount"],
                    seat["sell_amount"],
                    seat["net_amount"],
                    seat["seat_type"],
                )
                if identity in seen_seats:
                    continue
                seen_seats.add(identity)
                seat_rows.append(seat)

    return build_daily_longhubang_evidence(
        review_date,
        normalized_candidates,
        daily_rows,
        seat_rows,
        source=AKSHARE_LONGHUBANG_SOURCE,
    )


def map_akshare_daily_row(row: dict[str, Any], review_date: str) -> dict[str, Any]:
    sample_date = normalize_date_value(row.get("上榜日"))
    if sample_date != review_date:
        raise LonghubangSourceError(
            f"AKShare 龙虎榜日榜日期与复盘日期不一致：期望 {review_date}，实际 {sample_date or row.get('上榜日')}。"
        )
    return {
        "review_date": sample_date,
        "code": row.get("代码"),
        "name": row.get("名称"),
        "reason": row.get("上榜原因"),
        "buy_amount": row.get("龙虎榜买入额"),
        "sell_amount": row.get("龙虎榜卖出额"),
        "net_amount": row.get("龙虎榜净买额"),
    }


def map_akshare_seat_row(
    row: dict[str, Any],
    review_date: str,
    code: str,
) -> dict[str, Any]:
    return {
        "review_date": review_date,
        "code": code,
        "seat_name": normalize_text(row.get("交易营业部名称")),
        "seat_type": normalize_text(row.get("类型")),
        "buy_amount": first_amount(row, ("买入金额",), f"{code} 席位买入金额"),
        "sell_amount": first_amount(row, ("卖出金额",), f"{code} 席位卖出金额"),
        "net_amount": first_amount(
            row,
            ("净额",),
            f"{code} 席位净额",
            allow_negative=True,
        ),
    }


def load_akshare_client(ak_client: Any | None) -> Any:
    if ak_client is not None:
        return ak_client
    try:
        return import_akshare_client()
    except Exception as error:  # noqa: BLE001
        raise LonghubangSourceError(f"无法加载 AKShare 龙虎榜客户端：{error}") from error
