# 本文件把用户结构化编辑追加为新的联合草案修订，并重新执行证据与对象链校验。

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from stock_review.review_runs.canonical_hash import canonical_hash
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.evidence_bundle import EvidenceBundle
from stock_review.review_runs.review_revision import (
    PlanItem,
    RevisionBundle,
    StepAssessment,
    assess_revision_quality,
)
from stock_review.review_runs.review_run import project_review_run
from stock_review.review_runs.review_run_event import ReviewRunEvent
from stock_review.review_runs.run_daily_review import (
    load_run_manifest,
    load_run_projection,
    require_manifest_active,
    require_repository_authorized,
)
from stock_review.review_runs.validate_model_response import validate_revision_evidence
from stock_review.storage.review_run_repository import ReviewRunRepository


EDIT_FIELDS = {"steps", "candidate_plan_items", "decision_note"}


# revise 使用全量 STEP 与 PlanItem 替换，避免局部 patch 让 STEP 8/10 和计划项分裂换版。
def revise_daily_review(
    run_id: str,
    base_review_revision_id: str,
    expected_current_revision_id: str,
    edits: Mapping[str, Any],
    operation_id: str,
    repository: ReviewRunRepository,
    *,
    now: datetime | None = None,
) -> RevisionBundle:
    if not operation_id.strip():
        raise ReviewRunDomainError("daily revise 必须提供 operation ID。")
    unexpected = set(edits) - EDIT_FIELDS
    if unexpected:
        raise ReviewRunDomainError(f"修订输入包含未知字段：{', '.join(sorted(unexpected))}")
    if base_review_revision_id != expected_current_revision_id:
        raise ReviewRunDomainError("revise 的 base revision 必须等于期望当前修订。")
    steps_value = edits.get("steps")
    plans_value = edits.get("candidate_plan_items")
    if not isinstance(steps_value, list) or not isinstance(plans_value, list):
        raise ReviewRunDomainError("revise 必须提供全量 steps 与 candidate_plan_items 数组。")
    requested_steps = tuple(
        StepAssessment.from_dict(_mapping(item, "STEP")) for item in steps_value
    )
    requested_plans = tuple(
        PlanItem.from_dict(_mapping(item, "PlanItem")) for item in plans_value
    )
    requested_decision_note = str(edits.get("decision_note") or "").strip()
    manifest = load_run_manifest(repository, run_id)
    timestamp = require_manifest_active(manifest, now=now)
    require_repository_authorized(manifest, repository)
    projection = load_run_projection(repository, run_id)
    if projection.state != "draft_ready":
        raise ReviewRunDomainError("daily revise 只能在 draft_ready 状态执行。")
    revision_id = f"REV-{canonical_hash({'run_id': run_id, 'operation_id': operation_id})[:24]}"
    existing = repository.get_review_revision(revision_id)
    if existing is not None:
        revision = RevisionBundle.from_dict(existing["payload"])
        if (
            revision.replaces_review_revision_id != base_review_revision_id
            or revision.steps != requested_steps
            or revision.candidate_plan_items != requested_plans
            or revision.decision_note != requested_decision_note
        ):
            raise ReviewRunDomainError(
                "相同 revise operation 使用了不同基础修订或编辑内容。"
            )
        _append_revision_event_and_audit(
            repository,
            manifest,
            revision,
            operation_id,
        )
        return revision
    if projection.current_review_revision_id != expected_current_revision_id:
        raise ReviewRunDomainError("daily revise 的期望当前修订已经过期。")
    base_record = repository.get_review_revision(base_review_revision_id)
    if base_record is None:
        raise ReviewRunDomainError(f"未找到基础修订：{base_review_revision_id}")
    base = RevisionBundle.from_dict(base_record["payload"])
    if base.revision_kind != "draft":
        raise ReviewRunDomainError("只能编辑 draft RevisionBundle。")
    revision = RevisionBundle.create(
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
        created_at=timestamp.isoformat(),
        revision_kind="draft",
        participation_decision="undecided",
        replaces_review_revision_id=base.review_revision_id,
        steps=requested_steps,
        candidate_plan_items=requested_plans,
        decision_note=requested_decision_note,
        model_response_hashes=base.model_response_hashes,
    )
    bundle_record = repository.get_evidence_bundle(base.evidence_bundle_id)
    if bundle_record is None:
        raise ReviewRunDomainError("基础修订引用的 EvidenceBundle 不存在。")
    evidence_bundle = EvidenceBundle.from_dict(bundle_record["payload"])
    evidence_bundle.validate_against_manifest(manifest)
    validate_revision_evidence(revision, evidence_bundle, repository)
    quality = assess_revision_quality(revision)
    gate_issues = [
        issue
        for gate in quality.gates
        if gate.gate_id in {"G1", "G2", "G3", "G5"} and gate.status == "failed"
        for issue in gate.issues
    ]
    if gate_issues:
        raise ReviewRunDomainError("修订破坏了发布边界：" + "；".join(gate_issues))
    repository.save_review_revision(
        revision.review_revision_id,
        revision.run_id,
        revision.evidence_bundle_id,
        revision.to_dict(),
        supersedes_review_revision_id=base.review_revision_id,
    )
    _append_revision_event_and_audit(repository, manifest, revision, operation_id)
    return revision


# 修订记录与事件分属可恢复步骤；事件追加前以同一事件 head 预演领域投影。
def _append_revision_event_and_audit(
    repository: ReviewRunRepository,
    manifest: Any,
    revision: RevisionBundle,
    operation_id: str,
) -> None:
    event = ReviewRunEvent.create(
        event_id=f"EVENT-revision-{canonical_hash({'run_id': revision.run_id, 'operation_id': operation_id})[:24]}",
        run_id=revision.run_id,
        event_type="revision_added",
        occurred_at=revision.created_at,
        operation_id=operation_id,
        review_revision_id=revision.review_revision_id,
        expected_current_revision_id=revision.replaces_review_revision_id,
    )
    records = repository.list_review_run_events(revision.run_id)
    for record in records:
        if record["event_id"] != event.event_id:
            continue
        if ReviewRunEvent.from_dict(record["payload"]) != event:
            raise ReviewRunDomainError(f"事件 {event.event_id} 已存在且内容不同。")
        _append_revision_audit(repository, manifest, revision, event)
        return
    events = tuple(ReviewRunEvent.from_dict(record["payload"]) for record in records)
    project_review_run(manifest, (*events, event))
    repository.append_review_run_event(
        revision.run_id,
        event.event_id,
        event.event_type,
        event.to_dict(),
        records[-1]["event_id"] if records else None,
    )
    _append_revision_audit(repository, manifest, revision, event)


def _append_revision_audit(
    repository: ReviewRunRepository,
    manifest: Any,
    revision: RevisionBundle,
    event: ReviewRunEvent,
) -> None:
    repository.append_audit_event(
        audit_event_id=f"AUDIT-{event.event_id}",
        event_type="daily revise",
        payload={
            "command": "daily revise",
            "run_id": revision.run_id,
            "trade_date": revision.trade_date,
            "manifest_id": manifest.manifest_id,
            "manifest_hash": manifest.content_hash,
            "base_review_revision_id": revision.replaces_review_revision_id,
            "review_revision_id": revision.review_revision_id,
            "review_revision_hash": revision.content_hash,
            "status": "completed",
        },
        outbox_id=f"OUTBOX-{event.event_id}",
        outbox_payload={
            "command": "daily revise",
            "run_id": revision.run_id,
            "trade_date": revision.trade_date,
            "status": "completed",
        },
        run_id=revision.run_id,
    )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewRunDomainError(f"{label} 必须是 JSON 对象。")
    return value

