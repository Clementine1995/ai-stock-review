# 本文件负责离线样例证据导入和证据快照检查，不调用真实行情接口。

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable

from stock_review.evidence.akshare_source import (
    AkshareSourceError,
    collect_akshare_market_evidence,
    collect_akshare_sector_evidence,
    collect_akshare_sentiment_evidence,
    collect_akshare_stock_evidence,
)
from stock_review.evidence.evidence_snapshot import EvidenceSnapshot, build_evidence_snapshot
from stock_review.evidence.hhxg_source import HhxgSourceError, collect_hhxg_snapshot_evidence
from stock_review.storage.sqlite_repository import EvidenceSnapshotRepository


DEFAULT_EVIDENCE_DIR = Path("data") / "evidence"
DEFAULT_DATABASE_PATH = Path("data") / "stock_review.sqlite"


class EvidenceSnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceScopeCollectionResult:
    scope: str
    output_path: Path | None
    error_message: str | None

    @property
    def is_successful(self) -> bool:
        return self.error_message is None


# 样例导入只接受本地 JSON，生成标准化快照并保存到本地 SQLite，保证后续复盘只读取标准证据。
def import_evidence_snapshot(
    trade_date: str,
    evidence_file: Path,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> Path:
    raw_data = read_json_mapping(evidence_file)
    raw_data = attach_field_sources(raw_data)
    snapshot = build_evidence_snapshot(trade_date, raw_data)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{trade_date}_snapshot.json"
    output_path.write_text(
        json.dumps(snapshot.to_mapping(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    repository = EvidenceSnapshotRepository(database_path)
    repository.save_snapshot(snapshot)
    return output_path


# AKShare 真实采集必须显式传入 scope，避免默认扩大到全市场扫描。
def collect_akshare_evidence_snapshot(
    trade_date: str,
    scope: str = "market",
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
    refresh: bool = False,
) -> Path:
    output_path = output_dir / f"{trade_date}_snapshot.json"
    existing_data = read_json_mapping(output_path) if output_path.exists() else {}
    # 同日同 scope 已有有效 AKShare 事实时直接复用，避免重复请求触发上游限流。
    if not refresh and is_akshare_scope_covered(existing_data, scope):
        return output_path
    try:
        if scope == "market":
            raw_data = collect_akshare_market_evidence(trade_date)
        elif scope == "sentiment":
            raw_data = collect_akshare_sentiment_evidence(trade_date)
        elif scope == "sectors":
            raw_data = collect_akshare_sector_evidence(trade_date)
        elif scope == "stocks":
            existing_sectors = existing_data.get("sectors")
            sectors = existing_sectors if isinstance(existing_sectors, list) else []
            raw_data = collect_akshare_stock_evidence(trade_date, sectors=sectors)
        else:
            raise AkshareSourceError("当前仅支持 scope=market、sentiment、sectors 或 stocks 的最小采集。")
    except AkshareSourceError as error:
        raise EvidenceSnapshotError(str(error)) from error

    output_dir.mkdir(parents=True, exist_ok=True)
    if existing_data:
        raw_data = merge_evidence_data(existing_data, raw_data)
    else:
        raw_data = attach_field_sources(raw_data)

    snapshot = build_evidence_snapshot(trade_date, raw_data)
    output_path.write_text(
        json.dumps(snapshot.to_mapping(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    repository = EvidenceSnapshotRepository(database_path)
    repository.save_snapshot(snapshot)
    return output_path


# 单日多 scope 编排只复用既有单 scope 采集；每个 scope 独立失败，避免单点异常掩盖其它事实。
def collect_akshare_evidence_scopes(
    trade_date: str,
    scopes: list[str],
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
    refresh: bool = False,
    collector: Callable[..., Path] = collect_akshare_evidence_snapshot,
) -> tuple[EvidenceScopeCollectionResult, ...]:
    if not scopes:
        raise EvidenceSnapshotError("每日采集至少需要显式传入一个 scope。")
    if len(set(scopes)) != len(scopes):
        raise EvidenceSnapshotError("每日采集 scope 不能重复传入。")

    results: list[EvidenceScopeCollectionResult] = []
    for scope in scopes:
        try:
            output_path = collector(
                trade_date,
                scope=scope,
                output_dir=output_dir,
                database_path=database_path,
                refresh=refresh,
            )
        except EvidenceSnapshotError as error:
            results.append(EvidenceScopeCollectionResult(scope, None, str(error)))
        else:
            results.append(EvidenceScopeCollectionResult(scope, output_path, None))
    return tuple(results)


# hhxg 只允许采集返回日期与指定交易日一致的最近快照，避免把最新事实错误回补到历史日期。
def collect_hhxg_evidence_snapshot(
    trade_date: str,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> Path:
    try:
        raw_data = collect_hhxg_snapshot_evidence(trade_date)
    except HhxgSourceError as error:
        raise EvidenceSnapshotError(str(error)) from error

    output_path = output_dir / f"{trade_date}_snapshot.json"
    existing_data = read_json_mapping(output_path) if output_path.exists() else {}
    if existing_data:
        raw_data = merge_evidence_data(existing_data, raw_data)
    else:
        raw_data = attach_field_sources(raw_data)
    snapshot = build_evidence_snapshot(trade_date, raw_data)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot.to_mapping(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    EvidenceSnapshotRepository(database_path).save_snapshot(snapshot)
    return output_path


def is_akshare_scope_covered(snapshot_data: dict[str, Any], scope: str) -> bool:
    # 复用只针对已标记为 AKShare 的同日快照，避免把手工事实误当作可刷新来源。
    if snapshot_data.get("source") != "akshare":
        return False
    if scope == "market":
        market = snapshot_data.get("market")
        return isinstance(market, dict) and bool(market.get("indices")) and market.get("total_amount") is not None
    if scope == "sentiment":
        sentiment = snapshot_data.get("sentiment")
        required_fields = ("limit_up_count", "limit_down_count", "broken_board_rate", "highest_board")
        return isinstance(sentiment, dict) and all(sentiment.get(field) is not None for field in required_fields)
    if scope in ("sectors", "stocks"):
        items = snapshot_data.get(scope)
        return isinstance(items, list) and bool(items)
    return False


def merge_evidence_data(existing_data: dict[str, Any], incoming_data: dict[str, Any]) -> dict[str, Any]:
    # 分 scope 采集会写入同一个交易日快照；这里只按标准字段合并，避免后采集的空字段覆盖已有证据。
    merged_data = dict(existing_data)
    merged_data["source"] = incoming_data.get("source") or existing_data.get("source") or "manual"
    merged_data["sample_date"] = incoming_data.get("sample_date") or existing_data.get("sample_date")
    field_sources = read_field_sources(existing_data)
    incoming_source = str(incoming_data.get("source") or "manual")
    for field_name in ("market", "sentiment"):
        incoming_mapping = incoming_data.get(field_name)
        if isinstance(incoming_mapping, dict) and incoming_mapping:
            merged_data[field_name] = incoming_mapping
            field_sources[field_name] = incoming_source
    for field_name in ("sectors", "stocks"):
        incoming_items = incoming_data.get(field_name)
        if isinstance(incoming_items, list) and incoming_items:
            merged_data[field_name] = incoming_items
            field_sources[field_name] = incoming_source

    existing_events = existing_data.get("events") if isinstance(existing_data.get("events"), list) else []
    incoming_events = incoming_data.get("events") if isinstance(incoming_data.get("events"), list) else []
    merged_data["events"] = deduplicate_events([*existing_events, *incoming_events])
    merged_data["field_sources"] = field_sources
    merged_data.pop("missing_fields", None)
    return merged_data


# 新建快照为每个实际写入的标准字段记录来源；空字段不产生虚假来源。
def attach_field_sources(raw_data: dict[str, Any]) -> dict[str, Any]:
    normalized_data = dict(raw_data)
    field_sources = read_field_sources(raw_data)
    source = str(raw_data.get("source") or "manual")
    for field_name in ("market", "sentiment"):
        if isinstance(raw_data.get(field_name), dict) and raw_data[field_name]:
            field_sources[field_name] = source
    for field_name in ("sectors", "stocks"):
        if isinstance(raw_data.get(field_name), list) and raw_data[field_name]:
            field_sources[field_name] = source
    normalized_data["field_sources"] = field_sources
    return normalized_data


def read_field_sources(raw_data: dict[str, Any]) -> dict[str, str]:
    value = raw_data.get("field_sources")
    if not isinstance(value, dict):
        return {}
    return {
        field_name: str(source)
        for field_name, source in value.items()
        if field_name in {"market", "sentiment", "sectors", "stocks"} and source not in (None, "")
    }


def deduplicate_events(events: list[Any]) -> list[dict[str, Any]]:
    deduplicated_events: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        key = (
            str(event.get("title") or ""),
            str(event.get("source") or ""),
            str(event.get("note") or ""),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduplicated_events.append(event)
    return deduplicated_events


# 检查命令只读取已生成快照，不写入新业务状态。
def check_evidence_snapshot(
    trade_date: str,
    snapshot_dir: Path = DEFAULT_EVIDENCE_DIR,
) -> EvidenceSnapshot:
    snapshot_path = snapshot_dir / f"{trade_date}_snapshot.json"
    if not snapshot_path.exists():
        raise EvidenceSnapshotError(f"未找到证据快照：{snapshot_path}")

    snapshot_data = read_json_mapping(snapshot_path)
    return build_evidence_snapshot(trade_date, snapshot_data)


def read_json_mapping(file_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise EvidenceSnapshotError(f"JSON 格式不可识别：{file_path}：{error}") from error

    if not isinstance(data, dict):
        raise EvidenceSnapshotError(f"证据文件必须是 JSON 对象：{file_path}")
    return data
