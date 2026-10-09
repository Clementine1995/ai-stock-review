# 本文件负责把通达信连接器解析出的个股身份与相对强弱/连续比较事实落到标准快照。
# 仅做字段计算与快照合并，不直接实现第三方接口，也不在 SQL 中拼业务判断。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# 通达信 setcode 与交易所的对应关系：0=深市，1=沪市。
SETCODE_EXCHANGE = {0: "SZ", 1: "SH"}
TONGDAXIN_STOCK_IDENTITY_SOURCE = "workbuddy:tongdaxin.tdx_lookup_stock"
TONGDAXIN_STOCK_IDENTITY_SCOPE = "snapshot_stock_identity"


class StockFactEnrichmentError(ValueError):
    pass


def normalize_stock_name(value: Any) -> str:
    """去掉名称中的空白，便于快照与候选历史按名称对齐。"""
    return "".join(str(value or "").split())


def derive_exchange(setcode: int) -> str:
    return SETCODE_EXCHANGE[setcode]


def load_stock_identity_input(
    evidence_file: Path,
    expected_trade_date: str,
) -> dict[str, Any]:
    """读取并严格校验经 tdx_lookup_stock 解析出的身份输入。"""
    try:
        data = json.loads(Path(evidence_file).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise StockFactEnrichmentError(
            f"个股身份输入不是有效 JSON：{evidence_file}：{error}"
        ) from error
    if not isinstance(data, dict):
        raise StockFactEnrichmentError("个股身份输入必须是 JSON 对象。")

    # 三项信封字段共同限定来源、用途和交易日，避免把历史查询结果写入当前快照。
    if data.get("trade_date") != expected_trade_date:
        raise StockFactEnrichmentError(
            f"个股身份输入 trade_date 必须为 {expected_trade_date}。"
        )
    if data.get("source") != TONGDAXIN_STOCK_IDENTITY_SOURCE:
        raise StockFactEnrichmentError(
            f"个股身份输入 source 必须为 {TONGDAXIN_STOCK_IDENTITY_SOURCE}。"
        )
    if data.get("scope") != TONGDAXIN_STOCK_IDENTITY_SCOPE:
        raise StockFactEnrichmentError(
            f"个股身份输入 scope 必须为 {TONGDAXIN_STOCK_IDENTITY_SCOPE}。"
        )

    identities = data.get("identities")
    if not isinstance(identities, list) or not identities:
        raise StockFactEnrichmentError("个股身份输入 identities 必须是非空数组。")

    mapping: dict[str, dict[str, Any]] = {}
    for position, item in enumerate(identities, start=1):
        if not isinstance(item, dict):
            raise StockFactEnrichmentError(
                f"个股身份输入 identities[{position}] 必须是 JSON 对象。"
            )
        raw_name = item.get("name")
        name = normalize_stock_name(raw_name)
        if not isinstance(raw_name, str) or not name:
            raise StockFactEnrichmentError(
                f"个股身份输入 identities[{position}].name 必须是非空字符串。"
            )
        if name in mapping:
            raise StockFactEnrichmentError(f"个股身份输入存在重复规范化名称：{name}")

        code = item.get("code")
        if (
            not isinstance(code, str)
            or len(code) != 6
            or not code.isascii()
            or not code.isdigit()
        ):
            raise StockFactEnrichmentError(
                f"个股身份输入 {name}.code 必须是六位数字字符串。"
            )
        setcode = item.get("setcode")
        if type(setcode) is not int or setcode not in SETCODE_EXCHANGE:
            raise StockFactEnrichmentError(
                f"个股身份输入 {name}.setcode 必须是整数 0 或 1。"
            )
        mapping[name] = {
            "code": code,
            "setcode": setcode,
            "exchange": derive_exchange(setcode),
        }
    return mapping


def enrich_snapshot_identities(
    snapshot_mapping: dict[str, Any],
    identity_mapping: dict[str, dict[str, Any]],
    source: str = TONGDAXIN_STOCK_IDENTITY_SOURCE,
) -> dict[str, Any]:
    """把真实代码、交易所与 identity_verified 写回快照个股，并回填板块领涨股代码。

    仅覆盖 identity_mapping 中存在的名称；未匹配个股保持不变，不臆造代码。
    """
    stocks = snapshot_mapping.get("stocks") or []
    for stock in stocks:
        name = normalize_stock_name(stock.get("name"))
        identity = identity_mapping.get(name)
        if not identity:
            continue
        stock["code"] = identity["code"]
        stock["exchange"] = identity["exchange"]
        stock["identity_verified"] = True
        stock["identity_source"] = source

    sectors = snapshot_mapping.get("sectors") or []
    for sector in sectors:
        leading = sector.get("leading_stock")
        if isinstance(leading, str):
            identity = identity_mapping.get(normalize_stock_name(leading))
            if identity:
                sector["leading_stock_code"] = identity["code"]
        elif isinstance(leading, list):
            for entry in leading:
                if not isinstance(entry, dict):
                    continue
                identity = identity_mapping.get(normalize_stock_name(entry.get("name")))
                if identity:
                    entry["code"] = identity["code"]
    return snapshot_mapping


def _index_close_series(
    index_history: dict[str, Any], index_code: str
) -> dict[str, float]:
    """从指数历史文件中提取指定指数的 {sample_date: close}。"""
    series: dict[str, float] = {}
    for record in index_history.get("records") or []:
        sample_date = str(record.get("sample_date") or "")
        if not sample_date:
            continue
        for item in record.get("indices") or []:
            if str(item.get("code") or "") == index_code:
                close = item.get("close")
                if close is not None:
                    series[sample_date] = float(close)
    return series


def _stock_close_on_date(daily_bars: list[dict[str, Any]], date: str) -> float | None:
    for bar in daily_bars:
        if str(bar.get("date") or "") == date:
            close = bar.get("close")
            if close is not None:
                return float(close)
    return None


def compute_relative_strength(
    daily_bars: list[dict[str, Any]],
    index_history: dict[str, Any],
    benchmark_code: str = "000001",
) -> dict[str, Any]:
    """个股相对基准指数的区间表现：超额收益 = 个股区间涨幅 - 基准区间涨幅。

    数据完备才标记 complete；缺任意一端收盘价则标记 incomplete，绝不臆造。
    """
    index_series = _index_close_series(index_history, benchmark_code)
    if not index_series:
        return {"completeness": "incomplete", "reason": "基准指数历史缺失"}
    dates = sorted(index_series.keys())
    first_date, last_date = dates[0], dates[-1]
    bench_first = index_series[first_date]
    bench_last = index_series[last_date]
    if bench_first in (0, None):
        return {"completeness": "incomplete", "reason": "基准指数起点价格无效"}
    stock_first = _stock_close_on_date(daily_bars, first_date)
    stock_last = _stock_close_on_date(daily_bars, last_date)
    if stock_first in (None, 0) or stock_last is None:
        return {"completeness": "incomplete", "reason": "个股区间端点收盘价缺失"}

    bench_return = (bench_last - bench_first) / bench_first * 100.0
    stock_return = (stock_last - stock_first) / stock_first * 100.0
    excess = stock_return - bench_return
    direction = "相对偏强" if excess > 0 else "相对偏弱"
    return {
        "completeness": "complete",
        "benchmark": benchmark_code,
        "window_start": first_date,
        "window_end": last_date,
        "stock_return_percent": round(stock_return, 4),
        "benchmark_return_percent": round(bench_return, 4),
        "excess_return_percent": round(excess, 4),
        "direction": direction,
        "basis": "个股区间涨幅减去上证指数同区间涨幅得到超额收益",
    }


def compute_continuous_comparison(daily_bars: list[dict[str, Any]]) -> dict[str, Any]:
    """个股自身趋势连续性：末端连涨天数与近五个交易日上涨比例。"""
    if not daily_bars or len(daily_bars) < 2:
        return {"completeness": "incomplete", "reason": "日K样本不足"}
    # 连接器返回顺序不作为时间语义；仅按带日期的有效收盘价升序计算。
    dated_closes = sorted(
        (
            (str(bar.get("date") or ""), float(bar["close"]))
            for bar in daily_bars
            if bar.get("date") not in (None, "") and bar.get("close") is not None
        ),
        key=lambda item: item[0],
    )
    closes = [close for _, close in dated_closes]
    if len(closes) < 2:
        return {"completeness": "incomplete", "reason": "有效收盘价不足"}

    recent_changes = [
        current > previous
        for previous, current in zip(closes[-6:-1], closes[-5:])
    ]

    # 末端连涨必须从最新一根向前回溯；遇到首个不涨交易日立即结束。
    consecutive_up = 0
    for is_up in reversed(recent_changes):
        if is_up:
            consecutive_up += 1
        else:
            break

    # 近五个相邻交易日比较中的上涨比例独立统计，不能复用末端连涨次数。
    recent_up_ratio = sum(recent_changes) / len(recent_changes)
    if recent_up_ratio >= 0.6:
        direction = "上行延续"
    elif recent_up_ratio == 0:
        direction = "转弱"
    else:
        direction = "震荡"
    return {
        "completeness": "complete",
        "consecutive_up_days": consecutive_up,
        "recent_up_ratio": round(recent_up_ratio, 4),
        "direction": direction,
        "basis": "末端连涨天数与近五日上涨比例判断自身趋势连续性",
    }


def enrich_snapshot_comparisons(
    snapshot_mapping: dict[str, Any],
    candidate_history: dict[str, Any],
    index_history: dict[str, Any],
    benchmark_code: str = "000001",
) -> dict[str, Any]:
    """把相对强弱与连续比较事实写回快照个股（需候选历史日K与指数历史）。"""
    history_by_name: dict[str, dict[str, Any]] = {}
    for record in candidate_history.get("records") or []:
        history_by_name[normalize_stock_name(record.get("name"))] = record
    history_by_code: dict[str, dict[str, Any]] = {}
    for record in candidate_history.get("records") or []:
        code = str(record.get("code") or "").strip()
        if code.isdigit():
            history_by_code[code] = record

    stocks = snapshot_mapping.get("stocks") or []
    for stock in stocks:
        name = normalize_stock_name(stock.get("name"))
        code = str(stock.get("code") or "").strip()
        record = history_by_code.get(code) or history_by_name.get(name)
        if not record:
            stock["relative_strength"] = {"completeness": "incomplete", "reason": "候选历史缺该个股"}
            stock["continuous_comparison"] = {"completeness": "incomplete", "reason": "候选历史缺该个股"}
            continue
        daily_bars = record.get("daily_bars") or []
        stock["relative_strength"] = compute_relative_strength(
            daily_bars, index_history, benchmark_code
        )
        stock["continuous_comparison"] = compute_continuous_comparison(daily_bars)
    return snapshot_mapping
