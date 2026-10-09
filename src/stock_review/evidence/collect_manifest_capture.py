# 本文件把 RunManifest 的直接采集槽位路由到既有来源适配器，不持久化证据或业务状态。

from __future__ import annotations

from typing import Any, Callable, Mapping

from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.run_manifest import CaptureSlot, RunManifest


DirectCollector = Callable[[str, CaptureSlot], Mapping[str, Any]]


# 调用发生前再次核对槽位属于当前 manifest；来源异常由上层记录为 unavailable 终态。
def collect_manifest_capture(
    manifest: RunManifest,
    slot_id: str,
    collectors: Mapping[str, DirectCollector] | None = None,
) -> dict[str, Any]:
    manifest.validate_hash()
    slot = manifest.capture_by_id(slot_id)
    collector = (collectors or default_direct_collectors()).get(slot.source)
    if collector is None:
        raise ReviewRunDomainError(f"数据源 {slot.source} 没有直接采集适配器。")
    result = collector(manifest.trade_date, slot)
    if not isinstance(result, Mapping):
        raise ReviewRunDomainError(f"数据源 {slot.source} 返回值必须是 JSON 对象。")
    return {
        "run_id": manifest.run_id,
        "trade_date": manifest.trade_date,
        "sample_date": str(result.get("sample_date") or manifest.trade_date),
        "source": slot.source,
        "scope": slot.scope,
        "operation_id": slot.operation_id,
        "status": "available",
        "normalized": _select_scope_payload(slot, result),
        "raw": dict(result),
        "missing_fields": [],
    }


def default_direct_collectors() -> dict[str, DirectCollector]:
    return {
        "akshare": _collect_akshare,
        "hhxg": _collect_hhxg,
    }


def _collect_akshare(trade_date: str, slot: CaptureSlot) -> Mapping[str, Any]:
    from stock_review.evidence.akshare_source import (
        collect_akshare_market_evidence,
        collect_akshare_sector_evidence,
        collect_akshare_sentiment_evidence,
        collect_akshare_stock_evidence,
    )

    collectors: dict[str, Callable[..., dict[str, Any]]] = {
        "market": collect_akshare_market_evidence,
        "sentiment": collect_akshare_sentiment_evidence,
        "sectors": collect_akshare_sector_evidence,
        "stocks": collect_akshare_stock_evidence,
    }
    collector = collectors.get(slot.scope)
    if collector is None:
        raise ReviewRunDomainError(f"AKShare 不支持 manifest scope：{slot.scope}")
    return collector(trade_date)


def _collect_hhxg(trade_date: str, slot: CaptureSlot) -> Mapping[str, Any]:
    if slot.scope not in {"snapshot", "sentiment", "sectors"}:
        raise ReviewRunDomainError(f"hhxg 不支持 manifest scope：{slot.scope}")
    from stock_review.evidence.hhxg_source import collect_hhxg_snapshot_evidence

    return collect_hhxg_snapshot_evidence(trade_date)


# 旧适配器返回稀疏 Snapshot；这里仅保留槽位声明的业务字段，避免空字段扩大授权表面。
def _select_scope_payload(slot: CaptureSlot, result: Mapping[str, Any]) -> dict[str, Any]:
    if slot.scope == "snapshot":
        selected = {
            key: result.get(key)
            for key in ("market", "sentiment", "sectors", "stocks", "events")
            if key in result
        }
    elif slot.scope in {"market", "sentiment", "sectors", "stocks"}:
        selected = {slot.scope: result.get(slot.scope, {} if slot.scope in {"market", "sentiment"} else [])}
    else:
        selected = {slot.scope: result.get(slot.scope)}
    selected.update(
        {
            "source": str(result.get("source") or slot.source),
            "sample_date": str(result.get("sample_date") or ""),
        }
    )
    if slot.allowed_objects:
        _filter_allowed_objects(selected, slot)
    return selected


def _filter_allowed_objects(payload: dict[str, Any], slot: CaptureSlot) -> None:
    records = payload.get(slot.scope)
    if not isinstance(records, list):
        return
    allowed = set(slot.allowed_objects)
    payload[slot.scope] = [
        record
        for record in records
        if isinstance(record, Mapping)
        and str(record.get("code") or record.get("name") or "") in allowed
    ]
