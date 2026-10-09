# 本文件编排模型请求和受限响应投影，不调用模型、不渲染报告，也不产生交易判断。

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from stock_review.evidence.build_evidence_bundle import read_capture_payload
from stock_review.review_framework.parse_framework import parse_framework_file
from stock_review.review_runs.canonical_hash import canonical_hash
from stock_review.review_runs.complete_daily_review import build_local_revision
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.evidence_bundle import EvidenceBundle
from stock_review.review_runs.review_revision import RevisionBundle, StepAssessment
from stock_review.review_runs.review_run import project_review_run
from stock_review.review_runs.review_run_event import ReviewRunEvent
from stock_review.review_runs.run_daily_review import (
    load_run_manifest,
    load_run_projection,
    require_manifest_active,
    require_repository_authorized,
)
from stock_review.review_runs.validate_model_response import (
    capture_supports_step,
    project_normalized_evidence_for_step,
    validate_model_narrative_facts,
    validate_model_response_shape,
    validate_revision_evidence,
)
from stock_review.storage.review_run_repository import ReviewRunRepository


MODEL_RESPONSE_TOP_LEVEL_FIELDS = {"steps"}
MODEL_RESPONSE_STEP_FIELDS = (
    "step_number",
    "conclusion",
    "counter_evidence",
    "open_questions",
)


# prepare-model 冻结请求正文和指纹；相同 operation 只返回原请求，不追加第二个事件。
def prepare_model_review(
    run_id: str,
    trade_date: str,
    base_revision_id: str | None,
    expected_current_revision_id: str | None,
    model_id: str,
    operation_id: str,
    repository: ReviewRunRepository,
    now: datetime | None = None,
) -> Mapping[str, Any]:
    _require_operation(operation_id, "prepare-model")
    if base_revision_id != expected_current_revision_id:
        raise ReviewRunDomainError("模型请求的 base revision 必须等于期望当前修订。")

    manifest, projection, evidence_bundle, timestamp = _load_run_context(
        run_id,
        trade_date,
        repository,
        now=now,
    )
    require_repository_authorized(manifest, repository)
    request_id = _stable_id("MODELREQUEST", run_id, operation_id)
    existing = repository.get_model_request(request_id)
    if existing is not None:
        payload = _mapping(existing["payload"], "已保存模型请求")
        _require_request_identity(
            existing,
            payload,
            run_id=run_id,
            trade_date=trade_date,
            base_revision_id=base_revision_id,
            expected_current_revision_id=expected_current_revision_id,
            model_id=model_id,
            operation_id=operation_id,
            manifest_hash=manifest.content_hash,
        )
        _append_model_request_event(repository, manifest, payload)
        _append_model_audit(
            repository,
            "daily prepare-model",
            manifest,
            request_id,
            {
                "request_id": request_id,
                "request_fingerprint": payload["request_fingerprint"],
                "model_id": payload["model_id"],
            },
        )
        return payload

    if projection.state not in {"evidence_ready", "draft_ready"}:
        raise ReviewRunDomainError("prepare-model 只能在证据封存后、发布前执行。")
    actual_revision_id = projection.current_review_revision_id or None
    if actual_revision_id != expected_current_revision_id:
        raise ReviewRunDomainError("prepare-model 的期望当前修订已经过期。")
    if manifest.model_use.mode == "none" or model_id not in manifest.model_use.model_ids:
        raise ReviewRunDomainError("模型请求没有得到当前 RunManifest 授权。")
    if len(projection.model_requests) >= manifest.model_use.max_requests:
        raise ReviewRunDomainError("模型请求数量超过 RunManifest 授权上限。")

    base_revision = _load_base_revision(
        repository,
        base_revision_id,
        run_id,
        trade_date,
        evidence_bundle,
    )
    request_content = {
        "schema_version": 2,
        "request_id": request_id,
        "run_id": run_id,
        "trade_date": trade_date,
        "operation_id": operation_id,
        "model_id": model_id,
        "manifest_hash": manifest.content_hash,
        "evidence_bundle_id": evidence_bundle.evidence_bundle_id,
        "evidence_bundle_hash": evidence_bundle.content_hash,
        "base_revision_id": base_revision_id,
        "expected_current_revision_id": expected_current_revision_id,
        "created_at": timestamp.isoformat(),
        "frozen_input": {
            "evidence_bundle": evidence_bundle.to_dict(),
            "normalized_evidence_by_step": _load_normalized_evidence_by_step(
                evidence_bundle,
                repository,
                base_revision,
            ),
            "base_revision": None if base_revision is None else base_revision.to_dict(),
        },
        "required_output": {
            "top_level_fields": sorted(MODEL_RESPONSE_TOP_LEVEL_FIELDS),
            "step_fields": list(MODEL_RESPONSE_STEP_FIELDS),
            "boundary": (
                "只能改写 STEP 结论、反向证据和待确认问题；每项只能使用 "
                "normalized_evidence_by_step 中同编号 STEP 的冻结视图，不得新增事实字段、对象、阈值或计划项。"
            ),
        },
    }
    fingerprint = canonical_hash(request_content)
    request_payload = {**request_content, "request_fingerprint": fingerprint}
    repository.save_model_request(
        request_id,
        run_id,
        evidence_bundle.evidence_bundle_id,
        base_revision_id,
        expected_current_revision_id,
        fingerprint,
        request_payload,
    )
    _append_model_request_event(repository, manifest, request_payload)
    _append_model_audit(
        repository,
        "daily prepare-model",
        manifest,
        request_id,
        {
            "request_id": request_id,
            "request_fingerprint": fingerprint,
            "model_id": model_id,
        },
    )
    return request_payload


# complete-model 先验证响应和请求时版本，再保存响应、模型完成事件及唯一的新修订。
def complete_model_review(
    run_id: str,
    trade_date: str,
    request_id: str,
    base_revision_id: str | None,
    expected_current_revision_id: str | None,
    response: Mapping[str, Any],
    manifest_hash: str,
    operation_id: str,
    repository: ReviewRunRepository,
    *,
    framework_path: Path = Path("stock-review.md"),
    now: datetime | None = None,
) -> RevisionBundle:
    _require_operation(operation_id, "complete-model")
    if not isinstance(response, Mapping):
        raise ReviewRunDomainError("模型响应必须是 JSON 对象。")
    validate_model_response_shape(response)
    unexpected = set(response) - MODEL_RESPONSE_TOP_LEVEL_FIELDS
    if unexpected:
        raise ReviewRunDomainError(
            "模型响应只能修改 STEP 字段，不能修改：" + ", ".join(sorted(unexpected))
        )
    if base_revision_id != expected_current_revision_id:
        raise ReviewRunDomainError("模型响应的 base revision 必须等于期望当前修订。")

    manifest, projection, evidence_bundle, command_timestamp = _load_run_context(
        run_id,
        trade_date,
        repository,
        now=now,
    )
    require_repository_authorized(manifest, repository)
    if manifest.content_hash != manifest_hash:
        raise ReviewRunDomainError("complete-model 的 manifest hash 与 ReviewRun 不一致。")
    request_record = repository.get_model_request(request_id)
    if request_record is None:
        raise ReviewRunDomainError(f"未找到模型请求：{request_id}")
    request_payload = _mapping(request_record["payload"], "模型请求")
    _require_request_identity(
        request_record,
        request_payload,
        run_id=run_id,
        trade_date=trade_date,
        base_revision_id=base_revision_id,
        expected_current_revision_id=expected_current_revision_id,
        model_id=str(request_payload.get("model_id") or ""),
        operation_id=str(request_payload.get("operation_id") or ""),
        manifest_hash=manifest_hash,
    )
    fingerprint = str(request_record["request_fingerprint"])
    response_hash = canonical_hash(response)
    response_id = _stable_id("MODELRESPONSE", run_id, operation_id)
    revision_id = _stable_id("REV", run_id, operation_id)

    existing_revision = repository.get_review_revision(revision_id)
    if existing_revision is not None:
        replayed_revision = _validate_completed_replay(
            repository,
            manifest,
            existing_revision,
            request_payload=request_payload,
            response_id=response_id,
            request_id=request_id,
            response_hash=response_hash,
            base_revision_id=base_revision_id,
            fingerprint=fingerprint,
            operation_id=operation_id,
        )
        _append_model_audit(
            repository,
            "daily complete-model",
            manifest,
            response_id,
            {
                "request_id": request_id,
                "response_hash": response_hash,
                "review_revision_id": replayed_revision.review_revision_id,
                "review_revision_hash": replayed_revision.content_hash,
            },
        )
        return replayed_revision

    if projection.state not in {"evidence_ready", "draft_ready"}:
        raise ReviewRunDomainError("complete-model 只能在证据封存后、发布前执行。")
    actual_revision_id = projection.current_review_revision_id or None
    if actual_revision_id != expected_current_revision_id:
        raise ReviewRunDomainError("模型响应到达时基础修订已经变化，拒绝迟到响应。")
    request_state = next(
        (item for item in projection.model_requests if item.request_id == request_id),
        None,
    )
    if request_state is None:
        raise ReviewRunDomainError("模型响应没有对应的 model_requested 事件。")
    if request_state.status != "pending":
        raise ReviewRunDomainError("模型请求已经结束，不能重复提交响应。")
    if (
        request_state.request_hash != fingerprint
        or request_state.model_id != request_payload["model_id"]
        or request_state.evidence_bundle_id != evidence_bundle.evidence_bundle_id
        or request_state.evidence_bundle_hash != evidence_bundle.content_hash
        or request_state.base_review_revision_id != base_revision_id
        or request_state.expected_current_revision_id != expected_current_revision_id
    ):
        raise ReviewRunDomainError("模型请求事件与保存的请求指纹或冻结输入不一致。")

    base_revision = _load_base_revision(
        repository,
        base_revision_id,
        run_id,
        trade_date,
        evidence_bundle,
    )
    existing_response = repository.get_model_response(response_id)
    if existing_response is not None:
        response_payload = _mapping(existing_response["payload"], "已保存模型响应")
        _require_response_identity(
            existing_response,
            response_payload,
            request_id=request_id,
            response_hash=response_hash,
            fingerprint=fingerprint,
            operation_id=operation_id,
        )
        timestamp = _timestamp_from_payload(response_payload)
    else:
        timestamp = command_timestamp

    if base_revision is None:
        framework = parse_framework_file(Path(framework_path))
        if tuple(step.number for step in framework.steps) != tuple(range(1, 11)):
            raise ReviewRunDomainError("正式 ReviewRun 框架必须按顺序完整包含 STEP 1-10。")
        base_revision = build_local_revision(
            manifest,
            evidence_bundle,
            framework,
            repository,
            review_revision_id=revision_id,
            replaces_review_revision_id=None,
            created_at=timestamp,
        )
    validate_model_narrative_facts(
        response,
        base_revision,
        evidence_bundle,
        repository,
    )
    revision = _project_model_response(
        base_revision,
        response,
        revision_id=revision_id,
        base_revision_id=base_revision_id,
        created_at=timestamp,
        response_hash=response_hash,
    )
    validate_revision_evidence(revision, evidence_bundle, repository)

    if existing_response is None:
        response_payload = {
            "schema_version": 1,
            "model_response_id": response_id,
            "request_id": request_id,
            "request_fingerprint": fingerprint,
            "response_hash": response_hash,
            "operation_id": operation_id,
            "completed_at": timestamp.isoformat(),
            "response": response,
        }
        repository.save_model_response(
            response_id,
            request_id,
            fingerprint,
            response_payload,
        )
    repository.save_review_revision(
        revision.review_revision_id,
        revision.run_id,
        revision.evidence_bundle_id,
        revision.to_dict(),
        supersedes_review_revision_id=base_revision_id,
    )
    _append_model_completed_event(
        repository,
        manifest,
        request_payload,
        operation_id,
        response_hash,
        timestamp,
    )
    _append_revision_event(repository, manifest, revision, operation_id, timestamp)
    _append_model_audit(
        repository,
        "daily complete-model",
        manifest,
        response_id,
        {
            "request_id": request_id,
            "response_hash": response_hash,
            "review_revision_id": revision.review_revision_id,
            "review_revision_hash": revision.content_hash,
        },
    )
    return revision


# 模型阶段审计使用稳定 subject ID，重放只会补齐同一 outbox 记录。
def _append_model_audit(
    repository: ReviewRunRepository,
    command: str,
    manifest: Any,
    subject_id: str,
    details: Mapping[str, Any],
) -> None:
    audit_suffix = canonical_hash({"command": command, "subject_id": subject_id})[:24]
    repository.append_audit_event(
        audit_event_id=f"AUDIT-{audit_suffix}",
        event_type=command,
        payload={
            "command": command,
            "run_id": manifest.run_id,
            "trade_date": manifest.trade_date,
            "manifest_id": manifest.manifest_id,
            "manifest_hash": manifest.content_hash,
            **dict(details),
            "status": "completed",
        },
        outbox_id=f"OUTBOX-{audit_suffix}",
        outbox_payload={
            "command": command,
            "run_id": manifest.run_id,
            "trade_date": manifest.trade_date,
            "subject_id": subject_id,
            "status": "completed",
        },
        run_id=manifest.run_id,
    )


# 模型字段按 STEP 编号投影；未授权字段均沿用基础修订。
def _project_model_response(
    base: RevisionBundle,
    response: Mapping[str, Any],
    *,
    revision_id: str,
    base_revision_id: str | None,
    created_at: datetime,
    response_hash: str,
) -> RevisionBundle:
    patches = {
        int(item["step_number"]): item
        for item in response["steps"]
    }
    unknown_steps = set(patches) - {step.step_number for step in base.steps}
    if unknown_steps:
        raise ReviewRunDomainError(
            "模型响应引用了基础修订不存在的 STEP："
            + ", ".join(str(item) for item in sorted(unknown_steps))
        )
    steps: list[StepAssessment] = []
    for step in base.steps:
        patch = patches.get(step.step_number)
        if patch is None:
            steps.append(step)
            continue
        steps.append(
            replace(
                step,
                conclusion=str(patch["conclusion"]).strip(),
                counter_evidence=(
                    tuple(str(item).strip() for item in patch["counter_evidence"])
                    if "counter_evidence" in patch
                    else step.counter_evidence
                ),
                open_questions=(
                    tuple(str(item).strip() for item in patch["open_questions"])
                    if "open_questions" in patch
                    else step.open_questions
                ),
            )
        )
    hashes = base.model_response_hashes
    if response_hash not in hashes:
        hashes = (*hashes, response_hash)
    return RevisionBundle.create(
        review_revision_id=revision_id,
        run_id=base.run_id,
        trade_date=base.trade_date,
        evidence_bundle_id=base.evidence_bundle_id,
        evidence_bundle_hash=base.evidence_bundle_hash,
        evidence_cutoff_at=base.evidence_cutoff_at,
        framework_version=base.framework_version,
        framework_hash=base.framework_hash,
        program_version=base.program_version,
        renderer_version=base.renderer_version,
        created_at=created_at.isoformat(),
        revision_kind=base.revision_kind,
        participation_decision=base.participation_decision,
        replaces_review_revision_id=base_revision_id,
        steps=tuple(steps),
        candidate_plan_items=base.candidate_plan_items,
        approved_plan_items=base.approved_plan_items,
        rejected_plan_items=base.rejected_plan_items,
        decision_note=base.decision_note,
        model_response_hashes=hashes,
    )


def _load_run_context(
    run_id: str,
    trade_date: str,
    repository: ReviewRunRepository,
    *,
    now: datetime | None = None,
) -> tuple[Any, Any, EvidenceBundle, datetime]:
    manifest = load_run_manifest(repository, run_id)
    timestamp = require_manifest_active(manifest, now=now)
    if manifest.trade_date != trade_date:
        raise ReviewRunDomainError("模型阶段的交易日期与 ReviewRun 不一致。")
    projection = load_run_projection(repository, run_id)
    if not projection.evidence_bundle_id:
        raise ReviewRunDomainError("ReviewRun 尚未绑定 EvidenceBundle。")
    record = repository.get_evidence_bundle(projection.evidence_bundle_id)
    if record is None:
        raise ReviewRunDomainError("ReviewRun 指向的 EvidenceBundle 不存在。")
    evidence_bundle = EvidenceBundle.from_dict(record["payload"])
    evidence_bundle.validate_against_manifest(manifest)
    if evidence_bundle.content_hash != projection.evidence_bundle_hash:
        raise ReviewRunDomainError("ReviewRun 投影中的 EvidenceBundle 哈希不一致。")
    return manifest, projection, evidence_bundle, timestamp


def _load_base_revision(
    repository: ReviewRunRepository,
    revision_id: str | None,
    run_id: str,
    trade_date: str,
    evidence_bundle: EvidenceBundle,
) -> RevisionBundle | None:
    if revision_id is None:
        return None
    record = repository.get_review_revision(revision_id)
    if record is None:
        raise ReviewRunDomainError(f"未找到基础修订：{revision_id}")
    revision = RevisionBundle.from_dict(record["payload"])
    if (
        revision.run_id != run_id
        or revision.trade_date != trade_date
        or revision.evidence_bundle_id != evidence_bundle.evidence_bundle_id
        or revision.evidence_bundle_hash != evidence_bundle.content_hash
    ):
        raise ReviewRunDomainError("基础修订不属于当前 ReviewRun 或 EvidenceBundle。")
    return revision


# 请求按 STEP 保存每个 capture 的裁剪正文和引用；不再向模型暴露完整合并快照。
def _load_normalized_evidence_by_step(
    evidence_bundle: EvidenceBundle,
    repository: ReviewRunRepository,
    base_revision: RevisionBundle | None,
) -> dict[str, list[dict[str, Any]]]:
    captures: list[tuple[Any, Mapping[str, Any], str]] = []
    for capture in evidence_bundle.captures:
        if capture.status != "available":
            continue
        record = repository.get_evidence_capture(f"CAPTURE-{capture.operation_id}")
        if record is None:
            raise ReviewRunDomainError(f"EvidenceCapture 不存在：{capture.slot_id}")
        payload = read_capture_payload(
            Path(record["payload"]["output_path"]), capture
        )
        reference = f"capture:{capture.slot_id}:{capture.normalized_evidence_hash}"
        captures.append((capture, payload, reference))

    references_by_step = (
        {
            step.step_number: set(step.evidence_references)
            for step in base_revision.steps
        }
        if base_revision is not None
        else {}
    )
    views: dict[str, list[dict[str, Any]]] = {}
    for step_number in range(1, 11):
        step_views: list[dict[str, Any]] = []
        for capture, payload, reference in captures:
            if base_revision is not None:
                if reference not in references_by_step[step_number]:
                    continue
            elif not capture_supports_step(capture, payload, step_number):
                continue
            step_views.append(
                {
                    "evidence_reference": reference,
                    "normalized": project_normalized_evidence_for_step(
                        capture,
                        payload.get("normalized"),
                        step_number,
                    ),
                }
            )
        views[str(step_number)] = step_views
    return views


def _append_model_request_event(
    repository: ReviewRunRepository,
    manifest: Any,
    payload: Mapping[str, Any],
) -> None:
    timestamp = _timestamp_from_payload(payload, "created_at")
    event = ReviewRunEvent.create(
        event_id=_stable_id("EVENT-model-request", str(payload["run_id"]), str(payload["operation_id"])),
        run_id=str(payload["run_id"]),
        event_type="model_requested",
        occurred_at=timestamp.isoformat(),
        operation_id=str(payload["operation_id"]),
        subject_id=str(payload["request_id"]),
        evidence_bundle_id=str(payload["evidence_bundle_id"]),
        evidence_bundle_hash=str(payload["evidence_bundle_hash"]),
        base_review_revision_id=_optional_text(payload.get("base_revision_id")),
        expected_current_revision_id=_optional_text(payload.get("expected_current_revision_id")),
        model_id=str(payload["model_id"]),
        model_request_hash=str(payload["request_fingerprint"]),
    )
    _append_event_once(repository, manifest, event)


def _append_model_completed_event(
    repository: ReviewRunRepository,
    manifest: Any,
    request: Mapping[str, Any],
    operation_id: str,
    response_hash: str,
    timestamp: datetime,
) -> None:
    event = ReviewRunEvent.create(
        event_id=_stable_id("EVENT-model-response", str(request["run_id"]), operation_id),
        run_id=str(request["run_id"]),
        event_type="model_completed",
        occurred_at=timestamp.isoformat(),
        operation_id=operation_id,
        subject_id=str(request["request_id"]),
        evidence_bundle_id=str(request["evidence_bundle_id"]),
        evidence_bundle_hash=str(request["evidence_bundle_hash"]),
        base_review_revision_id=_optional_text(request.get("base_revision_id")),
        expected_current_revision_id=_optional_text(request.get("expected_current_revision_id")),
        model_id=str(request["model_id"]),
        model_request_hash=str(request["request_fingerprint"]),
        model_response_hash=response_hash,
    )
    _append_event_once(repository, manifest, event)


def _append_revision_event(
    repository: ReviewRunRepository,
    manifest: Any,
    revision: RevisionBundle,
    operation_id: str,
    timestamp: datetime,
) -> None:
    event = ReviewRunEvent.create(
        event_id=_stable_id("EVENT-revision", revision.run_id, operation_id),
        run_id=revision.run_id,
        event_type="revision_added",
        occurred_at=timestamp.isoformat(),
        operation_id=operation_id,
        review_revision_id=revision.review_revision_id,
        expected_current_revision_id=revision.replaces_review_revision_id,
    )
    _append_event_once(repository, manifest, event)


def _append_event_once(
    repository: ReviewRunRepository,
    manifest: Any,
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
    # 先在读取事件 head 的同一快照上预演；之后的并发追加由 repository head 比较拒绝。
    project_review_run(manifest, (*projected_events, event))
    repository.append_review_run_event(
        event.run_id,
        event.event_id,
        event.event_type,
        event.to_dict(),
        events[-1]["event_id"] if events else None,
    )


def _require_request_identity(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    run_id: str,
    trade_date: str,
    base_revision_id: str | None,
    expected_current_revision_id: str | None,
    model_id: str,
    operation_id: str,
    manifest_hash: str,
) -> None:
    expected = {
        "run_id": run_id,
        "trade_date": trade_date,
        "base_revision_id": base_revision_id,
        "expected_current_revision_id": expected_current_revision_id,
        "model_id": model_id,
        "operation_id": operation_id,
        "manifest_hash": manifest_hash,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ReviewRunDomainError("相同模型请求 operation 使用了不同输入。")
    if (
        record.get("model_request_id") != payload.get("request_id")
        or record.get("run_id") != run_id
        or record.get("evidence_bundle_id") != payload.get("evidence_bundle_id")
        or record.get("base_review_revision_id") != base_revision_id
        or record.get("expected_current_revision_id") != expected_current_revision_id
    ):
        raise ReviewRunDomainError("模型请求正文与不可变关联字段不一致。")
    fingerprint = str(record.get("request_fingerprint") or "")
    if payload.get("request_fingerprint") != fingerprint:
        raise ReviewRunDomainError("模型请求正文与保存的请求指纹不一致。")
    unsigned = {key: value for key, value in payload.items() if key != "request_fingerprint"}
    if canonical_hash(unsigned) != fingerprint:
        raise ReviewRunDomainError("模型请求正文与请求指纹不一致。")


def _require_response_identity(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    request_id: str,
    response_hash: str,
    fingerprint: str,
    operation_id: str,
) -> None:
    if (
        record.get("model_request_id") != request_id
        or record.get("request_fingerprint") != fingerprint
        or payload.get("request_id") != request_id
        or payload.get("request_fingerprint") != fingerprint
        or payload.get("response_hash") != response_hash
        or payload.get("operation_id") != operation_id
        or canonical_hash(payload.get("response")) != response_hash
    ):
        raise ReviewRunDomainError("相同 complete-model operation 使用了不同响应或请求。")


def _validate_completed_replay(
    repository: ReviewRunRepository,
    manifest: Any,
    revision_record: Mapping[str, Any],
    *,
    request_payload: Mapping[str, Any],
    response_id: str,
    request_id: str,
    response_hash: str,
    base_revision_id: str | None,
    fingerprint: str,
    operation_id: str,
) -> RevisionBundle:
    response_record = repository.get_model_response(response_id)
    if response_record is None:
        raise ReviewRunDomainError("已存在模型修订，但缺少对应的不可变模型响应。")
    response_payload = _mapping(response_record["payload"], "已保存模型响应")
    _require_response_identity(
        response_record,
        response_payload,
        request_id=request_id,
        response_hash=response_hash,
        fingerprint=fingerprint,
        operation_id=operation_id,
    )
    revision = RevisionBundle.from_dict(revision_record["payload"])
    if (
        revision.replaces_review_revision_id != base_revision_id
        or response_hash not in revision.model_response_hashes
    ):
        raise ReviewRunDomainError("相同 complete-model operation 已绑定不同基础修订或响应。")
    timestamp = _timestamp_from_payload(response_payload)
    _append_model_completed_event(
        repository,
        manifest,
        request_payload,
        operation_id,
        response_hash,
        timestamp,
    )
    _append_revision_event(repository, manifest, revision, operation_id, timestamp)
    return revision


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewRunDomainError(f"{label}必须是 JSON 对象。")
    return value


def _timestamp_from_payload(
    payload: Mapping[str, Any],
    field: str = "completed_at",
) -> datetime:
    try:
        timestamp = datetime.fromisoformat(str(payload[field]))
    except (KeyError, TypeError, ValueError) as error:
        raise ReviewRunDomainError(f"模型记录的 {field} 不是合法时间。") from error
    return _timestamp(timestamp)


def _timestamp(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ReviewRunDomainError("业务时间必须包含时区。")
    return timestamp.astimezone(timezone.utc).replace(microsecond=0)


def _stable_id(prefix: str, run_id: str, operation_id: str) -> str:
    digest = canonical_hash({"run_id": run_id, "operation_id": operation_id})[:24]
    return f"{prefix}-{digest}"


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _require_operation(operation_id: str, command: str) -> None:
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ReviewRunDomainError(f"{command} 必须提供 operation ID。")
