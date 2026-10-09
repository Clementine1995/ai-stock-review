# 本文件编排 ReviewRun 的开始、证据捕获与封存，只通过 repository port 改变本地状态。

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from stock_review.evidence.build_evidence_bundle import (
    build_evidence_capture,
    read_capture_payload,
    seal_evidence_bundle,
    write_capture_payload,
)
from stock_review.evidence.collect_manifest_capture import DirectCollector, collect_manifest_capture
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.canonical_hash import canonical_hash
from stock_review.review_runs.evaluate_capture_predicate import (
    evaluate_capture_predicate,
    merge_normalized_evidence,
)
from stock_review.review_runs.evidence_bundle import EvidenceBundle, EvidenceCapture
from stock_review.review_runs.review_run import ReviewRun, project_review_run
from stock_review.review_runs.review_run_event import ReviewRunEvent
from stock_review.review_runs.run_manifest import (
    DAILY_REVIEW_DATABASE_TABLES,
    CaptureSlot,
    RunManifest,
)
from stock_review.storage.review_run_repository import ReviewRunRepository


# start 的用户授权由完整 manifest 与哈希共同表达；过期或篡改的清单不得产生 run。
def start_daily_review(
    manifest: RunManifest,
    manifest_hash: str,
    repository: ReviewRunRepository,
    *,
    collectors: Mapping[str, DirectCollector] | None = None,
    now: datetime | None = None,
    event_id_factory=uuid4,
) -> ReviewRun:
    timestamp = require_manifest_active(manifest, now=now)
    if manifest_hash != manifest.content_hash:
        raise ReviewRunDomainError("daily start 提供的 manifest hash 与清单内容不一致。")
    require_repository_authorized(manifest, repository)

    repository.initialize_schema()
    repository.create_review_run(
        manifest.run_id,
        manifest.trade_date,
        {
            "run_id": manifest.run_id,
            "trade_date": manifest.trade_date,
            "manifest_id": manifest.manifest_id,
            "manifest_hash": manifest.content_hash,
        },
    )
    repository.save_run_manifest(manifest.manifest_id, manifest.run_id, manifest.to_dict())
    if not repository.list_review_run_events(manifest.run_id):
        start_event = ReviewRunEvent.create(
            event_id=_event_id("run", event_id_factory),
            run_id=manifest.run_id,
            event_type="run_started",
            occurred_at=timestamp.isoformat(),
            manifest_hash=manifest.content_hash,
        )
        _append_event(repository, manifest, start_event)
        _append_command_audit(repository, start_event, "daily start", manifest)
    else:
        projection = _load_run_projection(repository, manifest)
        if projection.manifest_hash != manifest.content_hash:
            raise ReviewRunDomainError("已存在 ReviewRun 未绑定当前 RunManifest。")
        start_record = repository.list_review_run_events(manifest.run_id)[0]
        _append_command_audit(
            repository,
            ReviewRunEvent.from_dict(start_record["payload"]),
            "daily start",
            manifest,
        )

    # 直接来源按清单顺序执行；单源失败转为 unavailable，完整性错误继续抛出。
    for slot in manifest.captures:
        if _load_run_projection(repository, manifest).capture_status(slot.slot_id) != "pending":
            capture_event = _find_capture_event(repository, manifest.run_id, slot.slot_id)
            _append_command_audit(
                repository,
                capture_event,
                "daily capture",
                manifest,
            )
            continue
        saved_capture = repository.get_evidence_capture(_capture_id(slot))
        if saved_capture is not None:
            # 采集文件与记录可能已完成而事件追加失败；恢复时不得重新调用外部来源。
            capture = _load_saved_capture(manifest, slot, saved_capture)
            event = ReviewRunEvent.create(
                event_id=_event_id("capture", event_id_factory),
                run_id=manifest.run_id,
                event_type="capture_completed",
                occurred_at=capture.captured_at,
                operation_id=slot.operation_id,
                capture_slot_id=slot.slot_id,
                capture_status=capture.status,
            )
            _append_event(repository, manifest, event)
            _append_command_audit(repository, event, "daily capture", manifest)
            continue
        if slot.is_conditional:
            evidence, statuses = _current_evidence(repository, manifest)
            if not evaluate_capture_predicate(slot.predicate, evidence, statuses):
                _complete_terminal_capture(
                    repository,
                    manifest,
                    slot,
                    _terminal_envelope(manifest, slot, "skipped", "缺口谓词不成立。"),
                    timestamp,
                    event_id_factory,
                )
                continue
        if slot.input_location == "base64-stdin":
            continue
        try:
            envelope = collect_manifest_capture(manifest, slot.slot_id, collectors)
        except Exception as error:  # noqa: BLE001
            envelope = _terminal_envelope(
                manifest,
                slot,
                "unavailable",
                f"{type(error).__name__}: {error}",
            )
        _complete_terminal_capture(
            repository,
            manifest,
            slot,
            envelope,
            timestamp,
            event_id_factory,
        )
    return _load_run_projection(repository, manifest)


# attach-evidence 是外部信封的唯一写入口，重复 operation 只允许完全相同内容。
def attach_daily_evidence(
    run_id: str,
    trade_date: str,
    manifest_hash: str,
    operation_id: str,
    envelope: Mapping[str, Any],
    repository: ReviewRunRepository,
    *,
    now: datetime | None = None,
    event_id_factory=uuid4,
) -> ReviewRun:
    manifest = load_run_manifest(repository, run_id)
    _require_run_identity(manifest, run_id, trade_date, manifest_hash)
    timestamp = require_manifest_active(manifest, now=now)
    require_repository_authorized(manifest, repository)
    projection = _load_run_projection(repository, manifest)
    if projection.state != "collecting":
        raise ReviewRunDomainError("EvidenceBundle 封存后不能继续 attach-evidence。")
    matching_slots = [slot for slot in manifest.captures if slot.operation_id == operation_id]
    if len(matching_slots) != 1:
        raise ReviewRunDomainError("operation ID 不属于当前 RunManifest 的唯一采集槽位。")
    slot = matching_slots[0]
    if slot.input_location != "base64-stdin":
        raise ReviewRunDomainError("直接来源槽位不能通过 attach-evidence 注入。")
    if projection.capture_status(slot.slot_id) != "pending":
        existing = repository.get_evidence_capture(_capture_id(slot))
        if existing is not None and existing["payload"].get("envelope_hash") == _envelope_hash(envelope):
            _append_command_audit(
                repository,
                _find_capture_event(repository, run_id, slot.slot_id),
                "daily capture",
                manifest,
            )
            return projection
        raise ReviewRunDomainError(f"采集槽位 {slot.slot_id} 已完成，不能覆盖。")
    _complete_terminal_capture(
        repository,
        manifest,
        slot,
        envelope,
        timestamp,
        event_id_factory,
    )
    return _load_run_projection(repository, manifest)


# seal 前必须让每个槽位进入显式终态，并逐个回读不可变证据文件核验正文哈希。
def seal_daily_evidence(
    run_id: str,
    trade_date: str,
    manifest_hash: str,
    repository: ReviewRunRepository,
    *,
    now: datetime | None = None,
    event_id_factory=uuid4,
) -> tuple[ReviewRun, Any]:
    manifest = load_run_manifest(repository, run_id)
    _require_run_identity(manifest, run_id, trade_date, manifest_hash)
    timestamp = require_manifest_active(manifest, now=now)
    require_repository_authorized(manifest, repository)
    projection = _load_run_projection(repository, manifest)
    if projection.state != "collecting":
        if not projection.evidence_bundle_id:
            raise ReviewRunDomainError("EvidenceBundle 只能在 collecting 状态封存。")
        bundle_record = repository.get_evidence_bundle(projection.evidence_bundle_id)
        if bundle_record is None:
            raise ReviewRunDomainError("ReviewRun 事件引用的 EvidenceBundle 不存在。")
        bundle = _load_evidence_bundle_record(bundle_record, manifest)
        _verify_capture_files(repository, manifest, bundle.captures)
        seal_event = next(
            ReviewRunEvent.from_dict(record["payload"])
            for record in repository.list_review_run_events(run_id)
            if record["event_type"] == "evidence_bundle_sealed"
            and record["payload"].get("evidence_bundle_id") == bundle.evidence_bundle_id
        )
        _append_command_audit(repository, seal_event, "daily seal-evidence", manifest)
        return projection, bundle
    pending = [slot.slot_id for slot in projection.capture_slots if slot.status == "pending"]
    if pending:
        raise ReviewRunDomainError(f"以下采集槽位尚未完成：{', '.join(pending)}")

    captures = list(_load_verified_captures(repository, manifest))
    bundle_identity = canonical_hash(
        {
            "manifest_hash": manifest.content_hash,
            "captures": [capture.to_dict() for capture in captures],
        }
    )
    bundle_id = f"EVIDENCE-{bundle_identity[:24]}"
    existing_bundle = repository.get_evidence_bundle(bundle_id)
    if existing_bundle is None:
        bundle = seal_evidence_bundle(
            manifest,
            tuple(captures),
            evidence_bundle_id=bundle_id,
            sealed_at=timestamp,
        )
        repository.save_evidence_bundle(
            bundle.evidence_bundle_id,
            manifest.run_id,
            manifest.manifest_id,
            bundle.to_dict(),
        )
    else:
        bundle = _load_evidence_bundle_record(existing_bundle, manifest)
    event = ReviewRunEvent.create(
        event_id=f"EVENT-seal-{bundle_identity[:24]}",
        run_id=manifest.run_id,
        event_type="evidence_bundle_sealed",
        occurred_at=bundle.sealed_at,
        evidence_bundle_id=bundle.evidence_bundle_id,
        evidence_bundle_hash=bundle.content_hash,
    )
    _append_event(repository, manifest, event)
    _append_command_audit(repository, event, "daily seal-evidence", manifest)
    return _load_run_projection(repository, manifest), bundle


def _load_evidence_bundle_record(
    record: Mapping[str, Any],
    manifest: RunManifest,
) -> EvidenceBundle:
    bundle = EvidenceBundle.from_dict(record["payload"])
    bundle.validate_against_manifest(manifest)
    return bundle


def _load_verified_captures(
    repository: ReviewRunRepository,
    manifest: RunManifest,
) -> tuple[EvidenceCapture, ...]:
    captures: list[EvidenceCapture] = []
    for slot in manifest.captures:
        record = repository.get_evidence_capture(_capture_id(slot))
        if record is None:
            raise ReviewRunDomainError(f"数据库缺少采集槽位记录：{slot.slot_id}")
        capture = EvidenceCapture.from_dict(record["payload"]["capture"])
        read_capture_payload(Path(record["payload"]["output_path"]), capture)
        captures.append(capture)
    return tuple(captures)


def _verify_capture_files(
    repository: ReviewRunRepository,
    manifest: RunManifest,
    expected_captures: tuple[EvidenceCapture, ...],
) -> None:
    actual = _load_verified_captures(repository, manifest)
    if actual != expected_captures:
        raise ReviewRunDomainError("EvidenceBundle 与当前不可变 capture 记录不一致。")


def _find_capture_event(
    repository: ReviewRunRepository,
    run_id: str,
    slot_id: str,
) -> ReviewRunEvent:
    for record in repository.list_review_run_events(run_id):
        event = ReviewRunEvent.from_dict(record["payload"])
        if event.event_type == "capture_completed" and event.capture_slot_id == slot_id:
            return event
    raise ReviewRunDomainError(f"采集槽位 {slot_id} 已完成但缺少事件。")


def load_run_manifest(repository: ReviewRunRepository, run_id: str) -> RunManifest:
    run_record = repository.get_review_run(run_id)
    if run_record is None:
        raise ReviewRunDomainError(f"未找到 ReviewRun：{run_id}")
    manifest_id = str(run_record["payload"].get("manifest_id") or "")
    manifest_record = repository.get_run_manifest(manifest_id)
    if manifest_record is None:
        raise ReviewRunDomainError(f"ReviewRun 缺少 RunManifest：{run_id}")
    return RunManifest.from_dict(manifest_record["payload"])


def load_run_projection(repository: ReviewRunRepository, run_id: str) -> ReviewRun:
    return _load_run_projection(repository, load_run_manifest(repository, run_id))


def _complete_terminal_capture(
    repository: ReviewRunRepository,
    manifest: RunManifest,
    slot: CaptureSlot,
    envelope: Mapping[str, Any],
    timestamp: datetime,
    event_id_factory,
) -> None:
    envelope_hash = _envelope_hash(envelope)
    existing = repository.get_evidence_capture(_capture_id(slot))
    if existing is None:
        output_path = Path(slot.output_location)
        if output_path.exists():
            # 文件先落盘而数据库写入中断时，按文件内首次时间重建当前信封并逐字段比对。
            capture, payload = _load_unrecorded_capture_file(
                manifest,
                slot,
                envelope,
                output_path,
            )
        else:
            capture, payload = build_evidence_capture(
                manifest,
                slot.slot_id,
                slot.operation_id,
                envelope,
                captured_at=timestamp,
            )
            output_path = write_capture_payload(output_path, payload)
        repository_payload = {
            "capture": capture.to_dict(),
            "output_path": str(output_path),
            "payload_hash": payload["payload_hash"],
            "envelope_hash": envelope_hash,
        }
        saved_record = repository.save_evidence_capture(
            _capture_id(slot),
            manifest.run_id,
            manifest.manifest_id,
            repository_payload,
        )
        # 数据库不可变记录落盘后复读目标文件，确保事件发布前文件仍与胜出的记录一致。
        capture = _load_saved_capture(manifest, slot, saved_record)
    else:
        # 文件与 capture 已保存但事件追加失败时，重试必须复用首次冻结的业务时间和内容。
        if existing["payload"].get("envelope_hash") != envelope_hash:
            raise ReviewRunDomainError(
                f"采集槽位 {slot.slot_id} 已保存不同运行或不同信封，不能覆盖。"
            )
        capture = _load_saved_capture(manifest, slot, existing)
    event = ReviewRunEvent.create(
        event_id=_event_id("capture", event_id_factory),
        run_id=manifest.run_id,
        event_type="capture_completed",
        occurred_at=capture.captured_at,
        operation_id=slot.operation_id,
        capture_slot_id=slot.slot_id,
        capture_status=capture.status,
    )
    _append_event(repository, manifest, event)
    _append_command_audit(repository, event, "daily capture", manifest)


def _load_saved_capture(
    manifest: RunManifest,
    slot: CaptureSlot,
    record: Mapping[str, Any],
) -> EvidenceCapture:
    if (
        record.get("run_id") != manifest.run_id
        or record.get("manifest_id") != manifest.manifest_id
    ):
        raise ReviewRunDomainError(f"采集槽位 {slot.slot_id} 的保存记录绑定错误。")
    capture = EvidenceCapture.from_dict(record["payload"]["capture"])
    if (
        capture.slot_id != slot.slot_id
        or capture.operation_id != slot.operation_id
        or capture.source != slot.source
        or capture.scope != slot.scope
    ):
        raise ReviewRunDomainError(f"采集槽位 {slot.slot_id} 的保存身份不一致。")
    output_path = Path(str(record["payload"].get("output_path") or ""))
    if output_path.resolve() != Path(slot.output_location).resolve():
        raise ReviewRunDomainError(f"采集槽位 {slot.slot_id} 的不可变文件路径越界。")
    payload = read_capture_payload(output_path, capture)
    if record["payload"].get("payload_hash") != payload.get("payload_hash"):
        raise ReviewRunDomainError(f"采集槽位 {slot.slot_id} 的保存哈希不一致。")
    return capture


# 孤立候选文件必须既自洽又能由本次规范信封按原 captured_at 完整重建，否则禁止接管。
def _load_unrecorded_capture_file(
    manifest: RunManifest,
    slot: CaptureSlot,
    envelope: Mapping[str, Any],
    output_path: Path,
) -> tuple[EvidenceCapture, dict[str, Any]]:
    try:
        stored_payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewRunDomainError(
            f"采集槽位 {slot.slot_id} 的孤立候选文件无法读取：{error}"
        ) from error
    if not isinstance(stored_payload, dict) or not isinstance(
        stored_payload.get("capture"),
        Mapping,
    ):
        raise ReviewRunDomainError(
            f"采集槽位 {slot.slot_id} 的孤立候选文件结构无效。"
        )
    capture = EvidenceCapture.from_dict(stored_payload["capture"])
    verified_payload = read_capture_payload(output_path, capture)
    original_timestamp = datetime.fromisoformat(capture.captured_at)
    expected_capture, expected_payload = build_evidence_capture(
        manifest,
        slot.slot_id,
        slot.operation_id,
        envelope,
        captured_at=original_timestamp,
    )
    if capture != expected_capture or verified_payload != expected_payload:
        raise ReviewRunDomainError(
            f"采集槽位 {slot.slot_id} 的落盘内容与本次规范信封不一致。"
        )
    return capture, verified_payload


def _load_run_projection(repository: ReviewRunRepository, manifest: RunManifest) -> ReviewRun:
    events = tuple(
        ReviewRunEvent.from_dict(record["payload"])
        for record in repository.list_review_run_events(manifest.run_id)
    )
    return project_review_run(manifest, events)


def _append_event(
    repository: ReviewRunRepository,
    manifest: RunManifest,
    event: ReviewRunEvent,
) -> None:
    events = repository.list_review_run_events(event.run_id)
    for record in events:
        if record["event_id"] != event.event_id:
            continue
        existing = ReviewRunEvent.from_dict(record["payload"])
        if existing != event:
            raise ReviewRunDomainError(f"事件 {event.event_id} 已存在且内容不同。")
        return
    projected_events = tuple(
        ReviewRunEvent.from_dict(record["payload"])
        for record in events
    )
    # 捕获生命周期事件先在当前完整事件流上预演，再由仓储事件头 CAS 拒绝读后竞态。
    project_review_run(manifest, (*projected_events, event))
    previous_id = events[-1]["event_id"] if events else None
    repository.append_review_run_event(
        event.run_id,
        event.event_id,
        event.event_type,
        event.to_dict(),
        previous_id,
    )


def _append_command_audit(
    repository: ReviewRunRepository,
    event: ReviewRunEvent,
    command: str,
    manifest: RunManifest,
) -> None:
    repository.append_audit_event(
        audit_event_id=f"AUDIT-{event.event_id}",
        event_type=command,
        payload={
            "command": command,
            "run_id": manifest.run_id,
            "trade_date": manifest.trade_date,
            "manifest_id": manifest.manifest_id,
            "manifest_hash": manifest.content_hash,
            "event_id": event.event_id,
            "status": "completed",
        },
        outbox_id=f"OUTBOX-{event.event_id}",
        outbox_payload={
            "event_id": event.event_id,
            "command": command,
            "run_id": manifest.run_id,
            "trade_date": manifest.trade_date,
            "status": "completed",
        },
        run_id=manifest.run_id,
    )


def _current_evidence(
    repository: ReviewRunRepository,
    manifest: RunManifest,
) -> tuple[dict[str, Any], dict[str, str]]:
    payloads: list[Mapping[str, Any]] = []
    statuses: dict[str, str] = {}
    for slot in manifest.captures:
        record = repository.get_evidence_capture(_capture_id(slot))
        if record is None:
            statuses[slot.slot_id] = "pending"
            continue
        capture = EvidenceCapture.from_dict(record["payload"]["capture"])
        statuses[slot.slot_id] = capture.status
        if capture.status == "available":
            payloads.append(read_capture_payload(Path(record["payload"]["output_path"]), capture))
    return merge_normalized_evidence(tuple(payloads)), statuses


def _terminal_envelope(
    manifest: RunManifest,
    slot: CaptureSlot,
    status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "run_id": manifest.run_id,
        "trade_date": manifest.trade_date,
        "sample_date": "",
        "source": slot.source,
        "scope": slot.scope,
        "operation_id": slot.operation_id,
        "status": status,
        "normalized": None,
        "missing_fields": [],
        "failure_reason": reason,
    }


def _require_run_identity(
    manifest: RunManifest,
    run_id: str,
    trade_date: str,
    manifest_hash: str,
) -> None:
    manifest.validate_hash()
    if manifest.run_id != run_id or manifest.trade_date != trade_date:
        raise ReviewRunDomainError("命令中的 run_id 或 trade_date 与 RunManifest 不一致。")
    if manifest.content_hash != manifest_hash:
        raise ReviewRunDomainError("命令中的 manifest hash 与 RunManifest 不一致。")


# RunManifest 必须显式绑定实际写入的 SQLite，防止确认后改用另一目标库。
def require_repository_authorized(
    manifest: RunManifest,
    repository: ReviewRunRepository,
) -> None:
    actual_path = repository.database_path.resolve()
    authorized_effects = {
        (Path(effect.rsplit(":", 1)[0]).resolve(), effect.rsplit(":", 1)[1])
        for effect in manifest.database_effects
        if ":" in effect
    }
    if actual_path not in {path for path, _table_name in authorized_effects}:
        raise ReviewRunDomainError("实际 SQLite 路径不在 RunManifest 的 database_effects 内。")
    missing_tables = tuple(
        table_name
        for table_name in DAILY_REVIEW_DATABASE_TABLES
        if (actual_path, table_name) not in authorized_effects
    )
    if missing_tables:
        raise ReviewRunDomainError(
            "RunManifest 的 database_effects 未完整覆盖 ReviewRun 写入："
            + ", ".join(missing_tables)
        )


# RunManifest 的时间授权覆盖所有写入口和幂等补写，调用方复用同一规范化业务时间。
def require_manifest_active(
    manifest: RunManifest,
    *,
    now: datetime | None = None,
) -> datetime:
    timestamp = _timestamp(now)
    manifest.validate_hash()
    if manifest.is_expired_at(timestamp.isoformat()):
        raise ReviewRunDomainError("RunManifest 已过期，请重新生成并确认运行范围。")
    return timestamp


def _capture_id(slot: CaptureSlot) -> str:
    return f"CAPTURE-{slot.operation_id}"


def _envelope_hash(envelope: Mapping[str, Any]) -> str:
    return canonical_hash(envelope)


def _event_id(prefix: str, id_factory) -> str:
    return f"EVENT-{prefix}-{id_factory().hex}"


def _timestamp(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ReviewRunDomainError("业务时间必须包含时区。")
    return timestamp.astimezone(timezone.utc).replace(microsecond=0)
