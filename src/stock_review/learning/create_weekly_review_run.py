# 本文件基于正式日报及其关联结果生成内容寻址周报，并发布不可变周报修订。

from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping

from stock_review.review_runs.canonical_hash import canonical_hash, canonical_json
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.storage.review_run_repository import (
    OptimisticConcurrencyError,
    ReviewRunRepository,
)


WEEKLY_INPUT_SCHEMA_VERSION = 1
WEEKLY_RENDERER_VERSION = "weekly-review-run-v1"


# 周报只从当前正式日报向下追踪，避免把同日已取代运行的判断混入周度样本。
def create_weekly_review_run(
    start_date: str,
    end_date: str,
    repository: ReviewRunRepository,
    *,
    output_root: Path,
) -> dict[str, Any]:
    period_start, period_end = _normalize_period(start_date, end_date)
    snapshot = repository.load_weekly_source_snapshot(period_start, period_end)
    publications = snapshot["publications"]
    if not publications:
        raise ReviewRunDomainError("日期范围内没有当前正式日报，不能发布版本化周报。")
    observations = snapshot["observations"]
    pool_events = snapshot["pool_events"]
    frozen_input = _build_frozen_input(
        period_start,
        period_end,
        publications,
        snapshot["revisions"],
        observations,
        pool_events,
    )
    source_heads = snapshot["source_heads"]
    versioned_input = {**frozen_input, "source_heads": source_heads}
    input_hash = canonical_hash(versioned_input)
    weekly_revision_id = f"WEEKLY-REV-{input_hash}"
    revision_payload = {"input_hash": input_hash, **versioned_input}
    report_content = render_weekly_review_run(weekly_revision_id, revision_payload)
    report_hash = hashlib.sha256(report_content.encode("utf-8")).hexdigest()
    blob_key = f"blobs/{report_hash}.md"
    report_path = Path(output_root) / Path(blob_key)

    # 文件先以内容哈希落盘；数据库失败时最多留下可识别的未引用 blob。
    _write_immutable_report(report_path, report_content)
    result_revision_ids = sorted(
        {
            item["current_result"]["result_revision_id"]
            for item in observations
            if item["current_result"]["result_revision_id"] is not None
        }
    )
    pool_event_ids = [item["event_id"] for item in pool_events]
    publication_ids = [item["publication_id"] for item in publications]
    repository.save_weekly_revision(
        weekly_revision_id,
        period_start,
        period_end,
        revision_payload,
        review_publication_ids=publication_ids,
        observation_result_revision_ids=result_revision_ids,
        pool_status_event_ids=pool_event_ids,
    )

    current = repository.get_current_weekly_publication(period_start, period_end)
    if _is_same_publication(current, weekly_revision_id, report_hash):
        _require_unchanged_source_heads(
            repository,
            period_start,
            period_end,
            source_heads,
        )
        return _weekly_result(
            current,
            input_hash=input_hash,
            report_path=report_path,
            replayed=True,
        )
    expected_publication_id = (
        current["weekly_publication_id"] if current is not None else None
    )
    publication_hash = canonical_hash(
        {
            "period_start": period_start,
            "period_end": period_end,
            "weekly_revision_id": weekly_revision_id,
            "report_hash": report_hash,
            "supersedes_weekly_publication_id": expected_publication_id,
        }
    )
    weekly_publication_id = f"WEEKLY-PUB-{publication_hash}"
    audit_suffix = publication_hash[:24]
    try:
        publication = repository.publish_weekly_revision(
            weekly_publication_id,
            period_start,
            period_end,
            weekly_revision_id,
            report_hash,
            {
                "blob_key": blob_key,
                "input_hash": input_hash,
                "renderer_version": WEEKLY_RENDERER_VERSION,
            },
            expected_weekly_publication_id=expected_publication_id,
            expected_source_heads=source_heads,
            audit={
                "audit_event_id": f"AUDIT-WEEKLY-{audit_suffix}",
                "event_type": "weekly create",
                "payload": {
                    "command": "weekly create",
                    "period_start": period_start,
                    "period_end": period_end,
                    "weekly_revision_id": weekly_revision_id,
                    "weekly_publication_id": weekly_publication_id,
                    "input_hash": input_hash,
                    "report_hash": report_hash,
                    "status": "completed",
                },
                "outbox_id": f"OUTBOX-WEEKLY-{audit_suffix}",
                "outbox_payload": {
                    "command": "weekly create",
                    "period_start": period_start,
                    "period_end": period_end,
                    "weekly_publication_id": weekly_publication_id,
                    "status": "completed",
                },
            },
        )
    except OptimisticConcurrencyError:
        # 并发生成相同输入时接受已发布的同内容赢家，其它变化仍向调用方报告冲突。
        winner = repository.get_current_weekly_publication(period_start, period_end)
        if not _is_same_publication(winner, weekly_revision_id, report_hash):
            raise
        _require_unchanged_source_heads(
            repository,
            period_start,
            period_end,
            source_heads,
        )
        return _weekly_result(
            winner,
            input_hash=input_hash,
            report_path=report_path,
            replayed=True,
        )
    return _weekly_result(
        publication,
        input_hash=input_hash,
        report_path=report_path,
        replayed=False,
    )


# 渲染只陈列冻结事实和版本 ID，不在存储输入之外新增周度判断。
def render_weekly_review_run(
    weekly_revision_id: str,
    revision_payload: Mapping[str, Any],
) -> str:
    publications = revision_payload["review_publications"]
    observations = revision_payload["observations"]
    pool_events = revision_payload["pool_status_events"]
    lines = [
        f"# {revision_payload['period_start']} 至 {revision_payload['period_end']} 版本化周报",
        "",
        f"- 周报修订：{weekly_revision_id}",
        f"- 输入哈希：{revision_payload['input_hash']}",
        f"- 渲染器版本：{revision_payload['renderer_version']}",
        f"- 正式日报：{len(publications)}",
        f"- Observation：{len(observations)}",
        f"- 相关池事件：{len(pool_events)}",
        "",
        "## 正式日报",
        "",
    ]
    lines.extend(
        f"- {item['trade_date']}｜{item['publication_id']}｜{item['report_hash']}"
        for item in publications
    )
    lines.extend(["", "## Observation 当前结果", ""])
    if observations:
        lines.extend(
            f"- {item['review_date']}｜{item['observation_id']}｜"
            f"{item['current_result']['status']}｜"
            f"{item['current_result']['result_revision_id'] or 'pending'}"
            for item in observations
        )
    else:
        lines.append("- 无。")
    lines.extend(["", "## 相关池事件", ""])
    if pool_events:
        lines.extend(
            f"- {item['pool_type']}｜{item['code']}｜{item['status']}｜{item['event_id']}"
            for item in pool_events
        )
    else:
        lines.append("- 无。")
    lines.extend(
        [
            "",
            "## 冻结输入",
            "",
            "```json",
            canonical_json(revision_payload),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


# 冻结输入保存重建报告所需字段，同时显式记录日报修订的框架与程序版本。
def _build_frozen_input(
    period_start: str,
    period_end: str,
    publications: list[dict[str, Any]],
    revisions: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    pool_events: list[dict[str, Any]],
) -> dict[str, Any]:
    publication_bindings = {
        (
            item.get("run_id"),
            item.get("review_revision_id"),
            item.get("action_set_id"),
        )
        for item in publications
    }
    action_set_ids = {
        item["action_set_id"]
        for item in publications
        if item.get("action_set_id") is not None
    }
    revisions_by_id = {item["review_revision_id"]: item for item in revisions}
    revision_traces = []
    for publication in publications:
        _verify_storage_record(
            publication,
            f"正式日报 {publication['publication_id']}",
        )
        revision = revisions_by_id.get(publication["review_revision_id"])
        if revision is None:
            raise ReviewRunDomainError(
                f"正式日报引用的复盘修订不存在：{publication['review_revision_id']}"
            )
        _verify_storage_record(revision, f"日报修订 {publication['review_revision_id']}")
        if revision.get("run_id") != publication.get("run_id"):
            raise ReviewRunDomainError("正式日报与日报修订的 ReviewRun 绑定不一致。")
        payload = revision["payload"]
        revision_traces.append(
            {
                "review_revision_id": revision["review_revision_id"],
                "content_hash": revision["content_hash"],
                "evidence_bundle_id": revision["evidence_bundle_id"],
                "evidence_bundle_hash": payload.get("evidence_bundle_hash"),
                "framework_version": payload.get("framework_version"),
                "framework_hash": payload.get("framework_hash"),
                "program_version": payload.get("program_version"),
                "renderer_version": payload.get("renderer_version"),
                "model_response_hashes": payload.get("model_response_hashes", []),
            }
        )
    for observation in observations:
        _verify_storage_record(
            observation,
            f"Observation {observation['observation_id']}",
        )
        observation_binding = (
            observation.get("run_id"),
            observation.get("review_revision_id"),
            observation.get("action_set_id"),
        )
        if observation_binding not in publication_bindings:
            raise ReviewRunDomainError(
                "Observation 未绑定同一份本周正式日报的 "
                "run_id、review_revision_id 和 action_set_id。"
            )
        result = observation["current_result"]
        if result.get("result_revision_id") is not None:
            _verify_storage_record(
                result,
                f"ObservationResult {result['result_revision_id']}",
            )
            if result.get("observation_id") != observation.get("observation_id"):
                raise ReviewRunDomainError("Observation 当前结果与基础记录绑定不一致。")
    for pool_event in pool_events:
        _verify_storage_record(pool_event, f"池事件 {pool_event['event_id']}")
        if pool_event.get("action_set_id") not in action_set_ids:
            raise ReviewRunDomainError("池事件未绑定本周冻结的正式日报 ActionSet。")
    return {
        "schema_version": WEEKLY_INPUT_SCHEMA_VERSION,
        "renderer_version": WEEKLY_RENDERER_VERSION,
        "period_start": period_start,
        "period_end": period_end,
        "review_publications": [_freeze_publication(item) for item in publications],
        "review_revision_traces": revision_traces,
        "observations": [_freeze_observation(item) for item in observations],
        "pool_status_events": [_freeze_pool_event(item) for item in pool_events],
    }


def _freeze_publication(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "publication_id": item["publication_id"],
        "trade_date": item["trade_date"],
        "run_id": item["run_id"],
        "review_revision_id": item["review_revision_id"],
        "action_set_id": item["action_set_id"],
        "report_hash": item["report_hash"],
        "content_hash": item["content_hash"],
        "payload": item["payload"],
    }


def _freeze_observation(item: Mapping[str, Any]) -> dict[str, Any]:
    result = item["current_result"]
    return {
        "observation_id": item["observation_id"],
        "run_id": item["run_id"],
        "review_date": item["review_date"],
        "review_revision_id": item["review_revision_id"],
        "plan_item_id": item["plan_item_id"],
        "action_set_id": item["action_set_id"],
        "content_hash": item["content_hash"],
        "payload": item["payload"],
        "current_result": {
            "result_revision_id": result["result_revision_id"],
            "status": result["status"],
            "content_hash": result["content_hash"],
            "payload": result["payload"],
        },
    }


def _freeze_pool_event(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": item["event_id"],
        "pool_type": item["pool_type"],
        "code": item["code"],
        "status": item["status"],
        "supersedes_event_id": item["supersedes_event_id"],
        "action_set_id": item["action_set_id"],
        "content_hash": item["content_hash"],
        "payload": item["payload"],
    }


def _normalize_period(start_date: str, end_date: str) -> tuple[str, str]:
    try:
        period_start = date.fromisoformat(start_date).isoformat()
        period_end = date.fromisoformat(end_date).isoformat()
    except (TypeError, ValueError) as error:
        raise ReviewRunDomainError("周报日期必须是 YYYY-MM-DD 格式。") from error
    if period_start > period_end:
        raise ReviewRunDomainError("周报开始日期不能晚于结束日期。")
    return period_start, period_end


def _write_immutable_report(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    if path.exists():
        if path.read_bytes() != encoded:
            raise ReviewRunDomainError("周报内容寻址路径已存在不同字节内容。")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(encoded)
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    if path.read_bytes() != encoded:
        raise ReviewRunDomainError("周报内容寻址文件落盘校验失败。")


def _is_same_publication(
    publication: Mapping[str, Any] | None,
    weekly_revision_id: str,
    report_hash: str,
) -> bool:
    return publication is not None and (
        publication["weekly_revision_id"] == weekly_revision_id
        and publication["report_hash"] == report_hash
    )


# 已发布同内容重放也必须在线性化读取点确认来源头未变化，不能吞掉来源 CAS 冲突。
def _require_unchanged_source_heads(
    repository: ReviewRunRepository,
    period_start: str,
    period_end: str,
    expected_source_heads: Mapping[str, Any],
) -> None:
    current = repository.load_weekly_source_snapshot(period_start, period_end)
    if canonical_hash(current["source_heads"]) != canonical_hash(expected_source_heads):
        raise OptimisticConcurrencyError("周报来源指针在冻结后已经变化。")


def _weekly_result(
    publication: Mapping[str, Any],
    *,
    input_hash: str,
    report_path: Path,
    replayed: bool,
) -> dict[str, Any]:
    return {
        "weekly_revision_id": publication["weekly_revision_id"],
        "weekly_publication_id": publication["weekly_publication_id"],
        "supersedes_weekly_publication_id": publication[
            "supersedes_weekly_publication_id"
        ],
        "input_hash": input_hash,
        "report_hash": publication["report_hash"],
        "report_path": str(report_path),
        "replayed": replayed,
    }


def _verify_storage_record(record: Mapping[str, Any], label: str) -> None:
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or record.get("content_hash") != canonical_hash(payload):
        raise ReviewRunDomainError(f"{label} 的存储内容哈希不一致。")
