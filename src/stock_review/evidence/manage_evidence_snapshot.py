# 本文件负责离线样例证据导入和证据快照检查，不调用真实行情接口。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stock_review.evidence.akshare_source import (
    AkshareSourceError,
    collect_akshare_market_evidence,
    collect_akshare_sector_evidence,
    collect_akshare_sentiment_evidence,
    collect_akshare_stock_evidence,
)
from stock_review.evidence.evidence_snapshot import EvidenceSnapshot, build_evidence_snapshot
from stock_review.storage.sqlite_repository import EvidenceSnapshotRepository


DEFAULT_EVIDENCE_DIR = Path("data") / "evidence"
DEFAULT_DATABASE_PATH = Path("data") / "stock_review.sqlite"


class EvidenceSnapshotError(ValueError):
    pass


# 样例导入只接受本地 JSON，生成标准化快照并保存到本地 SQLite，保证后续复盘只读取标准证据。
def import_evidence_snapshot(
    trade_date: str,
    evidence_file: Path,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> Path:
    raw_data = read_json_mapping(evidence_file)
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
) -> Path:
    output_path = output_dir / f"{trade_date}_snapshot.json"
    existing_data = read_json_mapping(output_path) if output_path.exists() else {}
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

    snapshot = build_evidence_snapshot(trade_date, raw_data)
    output_path.write_text(
        json.dumps(snapshot.to_mapping(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    repository = EvidenceSnapshotRepository(database_path)
    repository.save_snapshot(snapshot)
    return output_path


def merge_evidence_data(existing_data: dict[str, Any], incoming_data: dict[str, Any]) -> dict[str, Any]:
    # 分 scope 采集会写入同一个交易日快照；这里只按标准字段合并，避免后采集的空字段覆盖已有证据。
    merged_data = dict(existing_data)
    merged_data["source"] = incoming_data.get("source") or existing_data.get("source") or "manual"
    merged_data["sample_date"] = incoming_data.get("sample_date") or existing_data.get("sample_date")
    for field_name in ("market", "sentiment"):
        incoming_mapping = incoming_data.get(field_name)
        if isinstance(incoming_mapping, dict) and incoming_mapping:
            merged_data[field_name] = incoming_mapping
    for field_name in ("sectors", "stocks"):
        incoming_items = incoming_data.get(field_name)
        if isinstance(incoming_items, list) and incoming_items:
            merged_data[field_name] = incoming_items

    existing_events = existing_data.get("events") if isinstance(existing_data.get("events"), list) else []
    incoming_events = incoming_data.get("events") if isinstance(incoming_data.get("events"), list) else []
    merged_data["events"] = deduplicate_events([*existing_events, *incoming_events])
    merged_data.pop("missing_fields", None)
    return merged_data


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
