# 本文件负责离线样例证据导入和证据快照检查，不调用真实行情接口。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stock_review.evidence.akshare_source import AkshareSourceError, collect_akshare_market_evidence
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


# AKShare 真实采集只支持显式 source=akshare、scope=market，避免默认扩大到全市场扫描。
def collect_akshare_evidence_snapshot(
    trade_date: str,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> Path:
    try:
        raw_data = collect_akshare_market_evidence(trade_date)
    except AkshareSourceError as error:
        raise EvidenceSnapshotError(str(error)) from error

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
