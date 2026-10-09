# 本文件把权威 audit outbox 幂等投影为本地 JSONL 日志，不产生新的业务事实。

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from stock_review.review_runs.canonical_hash import canonical_json
from stock_review.storage.review_run_repository import ReviewRunRepository


# 先识别日志内已存在的 outbox_id，再逐条落盘并标记 delivered，支持崩溃后安全重放。
def project_pending_audit_log(
    repository: ReviewRunRepository,
    log_path: Path,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    target = Path(log_path)
    delivered_ids = _read_projected_ids(target)
    pending = repository.list_pending_outbox(limit=limit)
    projected = 0
    recovered = 0
    for item in pending:
        outbox_id = str(item["outbox_id"])
        if outbox_id in delivered_ids:
            repository.mark_outbox_delivered(outbox_id)
            recovered += 1
            continue
        line = canonical_json(
            {
                "outbox_id": outbox_id,
                "audit_event_id": item["audit_event_id"],
                "created_at": item["created_at"],
                "event": item["payload"],
            }
        )
        _append_line(target, line)
        repository.mark_outbox_delivered(outbox_id)
        delivered_ids.add(outbox_id)
        projected += 1
    return {
        "pending_count": len(pending),
        "projected_count": projected,
        "recovered_count": recovered,
        "log_path": str(target),
    }


def _read_projected_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    result: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("outbox_id"), str):
            result.add(value["outbox_id"])
    return result


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_separator = path.exists() and path.stat().st_size > 0 and not _ends_with_newline(path)
    with path.open("a", encoding="utf-8", newline="\n") as output:
        if needs_separator:
            output.write("\n")
        output.write(line)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as source:
        source.seek(-1, os.SEEK_END)
        return source.read(1) in {b"\n", b"\r"}
