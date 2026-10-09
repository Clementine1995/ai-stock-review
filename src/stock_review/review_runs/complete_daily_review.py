# 本文件从封存证据生成或追加 RevisionBundle，不负责渲染报告和正式发布。

from __future__ import annotations

from datetime import datetime
import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

from stock_review.evidence.build_evidence_bundle import read_capture_payload
from stock_review.review_framework.parse_framework import ReviewFramework, parse_framework_file
from stock_review.review_runs.canonical_hash import canonical_hash
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.evidence_bundle import EvidenceBundle, EvidenceCapture
from stock_review.review_runs.review_revision import (
    RevisionBundle,
    Step9PatternAssessment,
    StepAssessment,
)
from stock_review.review_runs.review_run import project_review_run
from stock_review.review_runs.review_run_event import ReviewRunEvent
from stock_review.review_runs.run_daily_review import (
    load_run_manifest,
    load_run_projection,
    require_manifest_active,
    require_repository_authorized,
)
from stock_review.review_runs.validate_model_response import (
    capture_evidence_capabilities,
    capture_supports_step,
    step_evidence_gaps,
)
from stock_review.scoring.verify_step9_patterns import STEP9_PATTERN_DEFINITIONS
from stock_review.storage.review_run_repository import ReviewRunRepository


PROGRAM_VERSION = "0.1.0"
RENDERER_VERSION = "review-run-v1"


# 规则版初稿只压缩已经冻结的事实和缺口，不自动挑选对象或产生交易阈值。
def complete_local_review(
    run_id: str,
    trade_date: str,
    expected_current_revision_id: str | None,
    manifest_hash: str,
    operation_id: str,
    repository: ReviewRunRepository,
    *,
    framework_path: Path = Path("stock-review.md"),
    now: datetime | None = None,
) -> RevisionBundle:
    if not operation_id.strip():
        raise ReviewRunDomainError("complete-local 必须提供 operation ID。")
    manifest = load_run_manifest(repository, run_id)
    if manifest.trade_date != trade_date or manifest.content_hash != manifest_hash:
        raise ReviewRunDomainError("complete-local 的日期或 manifest hash 与 ReviewRun 不一致。")
    command_timestamp = require_manifest_active(manifest, now=now)
    require_repository_authorized(manifest, repository)
    projection = load_run_projection(repository, run_id)
    if projection.state not in {"evidence_ready", "draft_ready"}:
        raise ReviewRunDomainError("complete-local 只能在证据封存后、发布前执行。")
    revision_id = _revision_id(run_id, operation_id)
    existing = repository.get_review_revision(revision_id)
    if existing is not None:
        revision = RevisionBundle.from_dict(existing["payload"])
        if revision.replaces_review_revision_id != expected_current_revision_id:
            raise ReviewRunDomainError("相同 operation ID 使用了不同的基础修订。")
        timestamp = datetime.fromisoformat(revision.created_at)
        event = ReviewRunEvent.create(
            event_id=_revision_event_id(run_id, operation_id),
            run_id=run_id,
            event_type="revision_added",
            occurred_at=timestamp.isoformat(),
            operation_id=operation_id,
            review_revision_id=revision.review_revision_id,
            expected_current_revision_id=expected_current_revision_id,
        )
        _append_event_once(repository, manifest, event)
        _append_revision_audit(repository, event, manifest, revision)
        return revision
    actual_revision_id = projection.current_review_revision_id or None
    if actual_revision_id != expected_current_revision_id:
        raise ReviewRunDomainError("complete-local 的期望当前修订已经过期。")
    if not projection.evidence_bundle_id:
        raise ReviewRunDomainError("ReviewRun 尚未绑定 EvidenceBundle。")
    evidence_record = repository.get_evidence_bundle(projection.evidence_bundle_id)
    if evidence_record is None:
        raise ReviewRunDomainError("ReviewRun 指向的 EvidenceBundle 不存在。")
    evidence_bundle = EvidenceBundle.from_dict(evidence_record["payload"])
    evidence_bundle.validate_against_manifest(manifest)
    framework = parse_framework_file(framework_path)
    _require_standard_framework(framework)
    revision = build_local_revision(
        manifest,
        evidence_bundle,
        framework,
        repository,
        review_revision_id=revision_id,
        replaces_review_revision_id=expected_current_revision_id,
        created_at=command_timestamp,
    )
    repository.save_review_revision(
        revision.review_revision_id,
        revision.run_id,
        revision.evidence_bundle_id,
        revision.to_dict(),
        supersedes_review_revision_id=expected_current_revision_id,
    )
    event = ReviewRunEvent.create(
        event_id=_revision_event_id(run_id, operation_id),
        run_id=run_id,
        event_type="revision_added",
        occurred_at=command_timestamp.isoformat(),
        operation_id=operation_id,
        review_revision_id=revision.review_revision_id,
        expected_current_revision_id=expected_current_revision_id,
    )
    _append_event_once(repository, manifest, event)
    _append_revision_audit(repository, event, manifest, revision)
    return revision


def build_local_revision(
    manifest: Any,
    evidence_bundle: EvidenceBundle,
    framework: ReviewFramework,
    repository: ReviewRunRepository,
    *,
    review_revision_id: str,
    replaces_review_revision_id: str | None,
    created_at: datetime,
) -> RevisionBundle:
    captures = _load_capture_context(manifest, evidence_bundle, repository)

    steps: list[StepAssessment] = []
    for framework_step in framework.steps:
        references = tuple(
            f"capture:{capture.slot_id}:{capture.normalized_evidence_hash}"
            for capture, payload in captures
            if capture.status == "available"
            and capture_supports_step(capture, payload, framework_step.number)
        )
        # 只用本步实际引用的 available 正文计算能力，不能把存在引用等同于证据齐备。
        capabilities = {
            capability
            for capture, payload in captures
            if f"capture:{capture.slot_id}:{capture.normalized_evidence_hash}" in references
            for capability in capture_evidence_capabilities(capture, payload)
        }
        scope_gaps = step_evidence_gaps(framework_step.number, capabilities)
        conclusion = (
            "已冻结本步相关事实，尚未形成经用户确认的业务判断。"
            if references
            else "本步缺少可用的直接证据，暂不形成业务判断。"
        )
        steps.append(
            StepAssessment(
                step_number=framework_step.number,
                title=framework_step.title,
                status="incomplete",
                conclusion=conclusion,
                evidence_references=references,
                open_questions=("需要用户结合证据确认本步结论。",),
                next_day_checks=(),
                evidence_gaps=scope_gaps,
                pattern_assessments=(
                    _build_step_nine_pattern_assessments(captures)
                    if framework_step.number == 9
                    else ()
                ),
            )
        )
    framework_content = framework.source_path.read_bytes()
    return RevisionBundle.create(
        review_revision_id=review_revision_id,
        run_id=manifest.run_id,
        trade_date=manifest.trade_date,
        evidence_bundle_id=evidence_bundle.evidence_bundle_id,
        evidence_bundle_hash=evidence_bundle.content_hash,
        evidence_cutoff_at=evidence_bundle.sealed_at,
        framework_version=_framework_version(framework.source_path),
        framework_hash=hashlib.sha256(framework_content).hexdigest(),
        program_version=PROGRAM_VERSION,
        renderer_version=RENDERER_VERSION,
        created_at=created_at.isoformat(),
        revision_kind="draft",
        participation_decision="undecided",
        replaces_review_revision_id=replaces_review_revision_id,
        steps=tuple(steps),
        decision_note="规则版草案未产生行动计划，等待用户定稿。",
    )


# 规则草案先把十二模式展开为诚实的证据状态，具体匹配与对象仍留给后续定稿。
def _build_step_nine_pattern_assessments(
    captures: tuple[tuple[EvidenceCapture, dict[str, Any]], ...],
) -> tuple[Step9PatternAssessment, ...]:
    evidence = tuple(
        (
            f"capture:{capture.slot_id}:{capture.normalized_evidence_hash}",
            capture_evidence_capabilities(capture, payload),
        )
        for capture, payload in captures
        if capture.status == "available"
    )
    assessments = []
    for definition in STEP9_PATTERN_DEFINITIONS:
        required = set(definition.background_evidence + definition.trigger_evidence)
        references = tuple(
            reference
            for reference, capabilities in evidence
            if capabilities.intersection(required)
        )
        capabilities = {
            capability
            for reference, capture_capabilities in evidence
            if reference in references
            for capability in capture_capabilities
        }
        missing_background = set(definition.background_evidence) - capabilities
        missing_triggers = set(definition.trigger_evidence) - capabilities
        if missing_background:
            status = "unavailable"
        elif missing_triggers:
            status = "base_evidence_available_trigger_pending"
        else:
            status = "assessment_pending"
        assessments.append(
            Step9PatternAssessment(
                pattern_number=definition.pattern_number,
                pattern_name=definition.name,
                status=status,
                evidence_references=references,
                missing_evidence=tuple(
                    capability
                    for capability in (
                        *definition.background_evidence,
                        *definition.trigger_evidence,
                    )
                    if capability in missing_background | missing_triggers
                ),
            )
        )
    return tuple(assessments)


def _load_capture_context(
    manifest: Any,
    bundle: EvidenceBundle,
    repository: ReviewRunRepository,
) -> tuple[tuple[EvidenceCapture, dict[str, Any]], ...]:
    result: list[tuple[EvidenceCapture, dict[str, Any]]] = []
    for capture in bundle.captures:
        record = repository.get_evidence_capture(f"CAPTURE-{capture.operation_id}")
        if record is None:
            raise ReviewRunDomainError(f"EvidenceBundle 引用的 capture 不存在：{capture.slot_id}")
        payload = read_capture_payload(Path(record["payload"]["output_path"]), capture)
        result.append((capture, payload))
    return tuple(result)


def _require_standard_framework(framework: ReviewFramework) -> None:
    numbers = tuple(step.number for step in framework.steps)
    if numbers != tuple(range(1, 11)):
        raise ReviewRunDomainError("正式 ReviewRun 框架必须按顺序完整包含 STEP 1-10。")


def _framework_version(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    match = re.search(r"(?m)^version:\s*(\S+)\s*$", content)
    return match.group(1) if match else "content-hash"


def _append_event_once(
    repository: ReviewRunRepository,
    manifest: Any,
    event: ReviewRunEvent,
) -> None:
    events = repository.list_review_run_events(event.run_id)
    for record in events:
        if record["event_id"] == event.event_id:
            existing = ReviewRunEvent.from_dict(record["payload"])
            if existing != event:
                raise ReviewRunDomainError(f"事件 {event.event_id} 已存在且内容不同。")
            return
    projected_events = tuple(
        ReviewRunEvent.from_dict(record["payload"])
        for record in events
    )
    # 新修订必须先在刚读取的完整事件流上通过状态机，再以同一事件头执行 CAS。
    project_review_run(manifest, (*projected_events, event))
    previous_id = events[-1]["event_id"] if events else None
    repository.append_review_run_event(
        event.run_id,
        event.event_id,
        event.event_type,
        event.to_dict(),
        previous_id,
    )


def _append_revision_audit(
    repository: ReviewRunRepository,
    event: ReviewRunEvent,
    manifest: Any,
    revision: RevisionBundle,
) -> None:
    audit_id = f"AUDIT-{event.event_id}"
    existing = repository.get_audit_event(audit_id)
    if existing is not None:
        return
    repository.append_audit_event(
        audit_event_id=audit_id,
        event_type="daily complete-local",
        payload={
            "command": "daily complete-local",
            "run_id": revision.run_id,
            "trade_date": revision.trade_date,
            "manifest_id": manifest.manifest_id,
            "manifest_hash": manifest.content_hash,
            "review_revision_id": revision.review_revision_id,
            "review_revision_hash": revision.content_hash,
            "status": "completed",
        },
        outbox_id=f"OUTBOX-{event.event_id}",
        outbox_payload={
            "event_id": event.event_id,
            "command": "daily complete-local",
            "run_id": revision.run_id,
            "trade_date": revision.trade_date,
            "status": "completed",
        },
        run_id=revision.run_id,
    )


def _revision_id(run_id: str, operation_id: str) -> str:
    return f"REV-{canonical_hash({'run_id': run_id, 'operation_id': operation_id})[:24]}"


def _revision_event_id(run_id: str, operation_id: str) -> str:
    return f"EVENT-revision-{canonical_hash({'run_id': run_id, 'operation_id': operation_id})[:24]}"

