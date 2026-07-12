# 本文件负责读取 hhxg.top 的最近交易日快照并转换为标准 Evidence Snapshot 输入，不回补历史数据。

from __future__ import annotations

import json
from typing import Any, Callable
import urllib.request


HHXG_SNAPSHOT_URL = "https://hhxg.top/api/snapshot"


class HhxgSourceError(RuntimeError):
    pass


# hhxg 只公开最近交易日快照，调用方必须用返回日期核验，禁止把最新数据写入指定历史日期。
def collect_hhxg_snapshot_evidence(
    trade_date: str,
    request_json: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = (request_json or request_hhxg_json)(HHXG_SNAPSHOT_URL)
    data = payload.get("data")
    if payload.get("success") is not True or not isinstance(data, dict):
        raise HhxgSourceError("hhxg 快照返回格式不可识别。")

    sample_date = str(data.get("date") or "")
    if sample_date != trade_date:
        raise HhxgSourceError(
            f"hhxg 仅返回最近交易日快照：请求日期 {trade_date}，返回日期 {sample_date or '待确认'}。"
        )

    market = data.get("market") if isinstance(data.get("market"), dict) else {}
    ladder = data.get("ladder") if isinstance(data.get("ladder"), dict) else {}
    limit_up_count = parse_number(market.get("limit_up"))
    fried_count = parse_number(market.get("fried"))
    total_touched_limit_up = (limit_up_count or 0) + (fried_count or 0)
    sentiment = {
        "limit_up_count": limit_up_count,
        "limit_down_count": parse_number(market.get("limit_down")),
        "broken_board_rate": round((fried_count or 0) / total_touched_limit_up, 4)
        if total_touched_limit_up
        else None,
        "highest_board": parse_number(ladder.get("max_streak")),
        "emotion_temperature": parse_number(market.get("sentiment_index")),
    }

    return {
        "source": "hhxg",
        "sample_date": sample_date,
        # hhxg 公开快照未提供项目要求的指数和两市成交额，不以其它字段替代。
        "market": {},
        "sentiment": sentiment,
        "sectors": collect_sector_records(data),
        # 公开快照未提供可确认的股票代码与交易所，保留缺口等待其它来源补齐。
        "stocks": [],
        "events": [
            {
                "title": "hhxg 最近交易日快照",
                "source": "hhxg",
                "note": "公开接口仅提供最近交易日快照；指数、成交额和可确认股票代码仍为待确认字段。",
            }
        ],
    }


# 板块记录保留公开快照给出的排行、资金和领涨股；bias_pct 不是涨跌幅，禁止映射为 change_percent。
def collect_sector_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    sectors: list[dict[str, Any]] = []
    for rank, item in enumerate(data.get("hot_themes") or [], start=1):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        sectors.append(
            {
                "name": str(item["name"]),
                "source_type": "hhxg_hot_theme",
                "rank": rank,
                "limit_up_count": parse_number(item.get("limitup_count")),
                "net_inflow_yi": parse_number(item.get("net_yi")),
                "leading_stock": item.get("top_stocks"),
            }
        )
    for group in data.get("sectors") or []:
        if not isinstance(group, dict):
            continue
        for direction in ("strong", "weak"):
            for rank, item in enumerate(group.get(direction) or [], start=1):
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                sectors.append(
                    {
                        "name": str(item["name"]),
                        "source_type": f"hhxg_{direction}_sector",
                        "rank": rank,
                        "net_inflow_yi": parse_number(item.get("net_yi")),
                        "leading_stock": item.get("leader"),
                        "bias_percent": parse_number(item.get("bias_pct")),
                    }
                )
    return sectors


def request_hhxg_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HhxgSourceError(f"hhxg 快照请求失败：{error}") from error
    if not isinstance(payload, dict):
        raise HhxgSourceError("hhxg 快照返回内容不是 JSON 对象。")
    return payload


def parse_number(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number
