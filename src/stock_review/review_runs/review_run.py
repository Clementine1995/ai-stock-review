# 本文件按追加式事件重建 ReviewRun 当前投影，不执行持久化或外部调用。

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.review_run_event import ReviewRunEvent
from stock_review.review_runs.run_manifest import RunManifest


REVIEW_RUN_STATES = {"collecting", "evidence_ready", "draft_ready", "published"}
MODEL_REQUEST_STATUSES = {"pending", "completed", "unavailable"}


@dataclass(frozen=True)
class CaptureSlotState:
    slot_id: str
    operation_id: str
    status: str

    def to_dict(self) -> dict[str, str]:
        return {
            "slot_id": self.slot_id,
            "operation_id": self.operation_id,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CaptureSlotState:
        return cls(
            slot_id=str(value.get("slot_id", "")),
            operation_id=str(value.get("operation_id", "")),
            status=str(value.get("status", "")),
        )


@dataclass(frozen=True)
class ModelRequestState:
    request_id: str
    operation_id: str
    model_id: str
    evidence_bundle_id: str
    evidence_bundle_hash: str
    base_review_revision_id: str | None
    expected_current_revision_id: str | None
    request_hash: str
    status: str
    response_hash: str = ""

    def __post_init__(self) -> None:
        if self.status not in MODEL_REQUEST_STATUSES:
            raise ReviewRunDomainError(f"未知模型请求状态：{self.status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "model_id": self.model_id,
            "evidence_bundle_id": self.evidence_bundle_id,
            "evidence_bundle_hash": self.evidence_bundle_hash,
            "base_review_revision_id": self.base_review_revision_id,
            "expected_current_revision_id": self.expected_current_revision_id,
            "request_hash": self.request_hash,
            "status": self.status,
            "response_hash": self.response_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelRequestState:
        base_revision = value.get("base_review_revision_id")
        expected_revision = value.get("expected_current_revision_id")
        return cls(
            request_id=str(value.get("request_id", "")),
            operation_id=str(value.get("operation_id", "")),
            model_id=str(value.get("model_id", "")),
            evidence_bundle_id=str(value.get("evidence_bundle_id", "")),
            evidence_bundle_hash=str(value.get("evidence_bundle_hash", "")),
            base_review_revision_id=(
                None if base_revision is None else str(base_revision)
            ),
            expected_current_revision_id=(
                None if expected_revision is None else str(expected_revision)
            ),
            request_hash=str(value.get("request_hash", "")),
            status=str(value.get("status", "")),
            response_hash=str(value.get("response_hash", "")),
        )


@dataclass(frozen=True)
class ReviewRun:
    run_id: str
    trade_date: str
    manifest_hash: str
    state: str
    capture_slots: tuple[CaptureSlotState, ...]
    evidence_bundle_id: str = ""
    evidence_bundle_hash: str = ""
    current_review_revision_id: str = ""
    publication_id: str = ""
    model_state: str = "not_requested"
    model_requests: tuple[ModelRequestState, ...] = ()
    failed_operation_ids: tuple[str, ...] = ()
    applied_event_ids: tuple[str, ...] = ()

    def capture_status(self, slot_id: str) -> str:
        for slot in self.capture_slots:
            if slot.slot_id == slot_id:
                return slot.status
        raise ReviewRunDomainError(f"ReviewRun 不包含采集槽位：{slot_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "manifest_hash": self.manifest_hash,
            "state": self.state,
            "capture_slots": [slot.to_dict() for slot in self.capture_slots],
            "evidence_bundle_id": self.evidence_bundle_id,
            "evidence_bundle_hash": self.evidence_bundle_hash,
            "current_review_revision_id": self.current_review_revision_id,
            "publication_id": self.publication_id,
            "model_state": self.model_state,
            "model_requests": [request.to_dict() for request in self.model_requests],
            "failed_operation_ids": list(self.failed_operation_ids),
            "applied_event_ids": list(self.applied_event_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReviewRun:
        state = str(value.get("state", ""))
        if state not in REVIEW_RUN_STATES:
            raise ReviewRunDomainError(f"未知 ReviewRun 状态：{state}")
        model_state = str(value.get("model_state", "not_requested"))
        if model_state not in {"not_requested", *MODEL_REQUEST_STATUSES}:
            raise ReviewRunDomainError(f"未知模型总状态：{model_state}")
        return cls(
            run_id=str(value.get("run_id", "")),
            trade_date=str(value.get("trade_date", "")),
            manifest_hash=str(value.get("manifest_hash", "")),
            state=state,
            capture_slots=tuple(
                CaptureSlotState.from_dict(_require_mapping(item, "采集槽位投影"))
                for item in value.get("capture_slots", ())
            ),
            evidence_bundle_id=str(value.get("evidence_bundle_id", "")),
            evidence_bundle_hash=str(value.get("evidence_bundle_hash", "")),
            current_review_revision_id=str(value.get("current_review_revision_id", "")),
            publication_id=str(value.get("publication_id", "")),
            model_state=model_state,
            model_requests=tuple(
                ModelRequestState.from_dict(_require_mapping(item, "模型请求投影"))
                for item in value.get("model_requests", ())
            ),
            failed_operation_ids=tuple(
                str(item) for item in value.get("failed_operation_ids", ())
            ),
            applied_event_ids=tuple(str(item) for item in value.get("applied_event_ids", ())),
        )


# 投影严格按事件流顺序执行；相同事件 ID 的完全相同重放忽略，内容不同则视为污染。
def project_review_run(
    manifest: RunManifest,
    events: Iterable[ReviewRunEvent],
) -> ReviewRun:
    manifest.validate_hash()
    state = ""
    capture_statuses: dict[str, str] = {}
    evidence_bundle_id = ""
    evidence_bundle_hash = ""
    current_revision_id = ""
    publication_id = ""
    failed_operation_ids: list[str] = []
    model_requests: dict[str, ModelRequestState] = {}
    applied_events: list[ReviewRunEvent] = []
    seen_events: dict[str, ReviewRunEvent] = {}

    for event in events:
        event.validate_hash()
        existing = seen_events.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise ReviewRunDomainError(f"事件 ID {event.event_id} 对应了不同内容。")
            continue
        seen_events[event.event_id] = event
        applied_events.append(event)

        if event.run_id != manifest.run_id:
            raise ReviewRunDomainError("事件流包含其它 ReviewRun 的事件。")
        if event.event_type == "run_started":
            if state:
                raise ReviewRunDomainError("ReviewRun 只能开始一次。")
            if event.manifest_hash != manifest.content_hash:
                raise ReviewRunDomainError("run_started 未绑定当前 RunManifest。")
            state = "collecting"
            continue
        if not state:
            raise ReviewRunDomainError("run_started 必须是 ReviewRun 的首个有效事件。")

        if event.event_type == "capture_completed":
            if state != "collecting":
                raise ReviewRunDomainError("EvidenceBundle 封存后不能继续完成 capture slot。")
            slot = manifest.capture_by_id(event.capture_slot_id)
            if event.operation_id != slot.operation_id:
                raise ReviewRunDomainError(
                    f"采集槽位 {slot.slot_id} 的 operation ID 与 RunManifest 不一致。"
                )
            if slot.slot_id in capture_statuses:
                raise ReviewRunDomainError(f"采集槽位 {slot.slot_id} 已经完成。")
            if event.capture_status == "skipped" and not slot.is_conditional:
                raise ReviewRunDomainError(f"非条件采集槽位 {slot.slot_id} 不能跳过。")
            capture_statuses[slot.slot_id] = event.capture_status
        elif event.event_type == "evidence_bundle_sealed":
            if state != "collecting":
                raise ReviewRunDomainError("EvidenceBundle 只能在 collecting 状态封存。")
            incomplete = [
                slot.slot_id for slot in manifest.captures if slot.slot_id not in capture_statuses
            ]
            if incomplete:
                raise ReviewRunDomainError(
                    f"以下采集槽位没有终态，不能封存 EvidenceBundle：{','.join(incomplete)}"
                )
            evidence_bundle_id = event.evidence_bundle_id
            evidence_bundle_hash = event.evidence_bundle_hash
            state = "evidence_ready"
        elif event.event_type == "model_requested":
            if state not in {"evidence_ready", "draft_ready"}:
                raise ReviewRunDomainError("模型事件只能发生在证据封存后、正式发布前。")
            if manifest.model_use.mode == "none":
                raise ReviewRunDomainError("RunManifest 没有授权模型请求。")
            if event.model_id not in manifest.model_use.model_ids:
                raise ReviewRunDomainError("模型请求使用了 RunManifest 之外的模型。")
            if len(model_requests) >= manifest.model_use.max_requests:
                raise ReviewRunDomainError("模型请求数量超过 RunManifest 授权上限。")
            if event.subject_id in model_requests:
                raise ReviewRunDomainError(f"模型请求 {event.subject_id} 已存在。")
            if (
                event.evidence_bundle_id != evidence_bundle_id
                or event.evidence_bundle_hash != evidence_bundle_hash
            ):
                raise ReviewRunDomainError("模型请求未绑定当前 EvidenceBundle。")
            expected_revision = current_revision_id or None
            if event.expected_current_revision_id != expected_revision:
                raise ReviewRunDomainError("模型请求的期望当前修订已经过期。")
            model_requests[event.subject_id] = ModelRequestState(
                request_id=event.subject_id,
                operation_id=event.operation_id,
                model_id=event.model_id,
                evidence_bundle_id=event.evidence_bundle_id,
                evidence_bundle_hash=event.evidence_bundle_hash,
                base_review_revision_id=event.base_review_revision_id,
                expected_current_revision_id=event.expected_current_revision_id,
                request_hash=event.model_request_hash,
                status="pending",
            )
        elif event.event_type == "model_completed":
            if state not in {"evidence_ready", "draft_ready"}:
                raise ReviewRunDomainError("模型事件只能发生在证据封存后、正式发布前。")
            request = model_requests.get(event.subject_id)
            if request is None:
                raise ReviewRunDomainError("模型响应没有对应的请求事件。")
            if request.status != "pending":
                raise ReviewRunDomainError("模型请求已经结束，不能重复提交响应。")
            if (
                event.model_id != request.model_id
                or event.evidence_bundle_id != request.evidence_bundle_id
                or event.evidence_bundle_hash != request.evidence_bundle_hash
                or event.base_review_revision_id != request.base_review_revision_id
                or event.expected_current_revision_id
                != request.expected_current_revision_id
                or event.model_request_hash != request.request_hash
            ):
                raise ReviewRunDomainError("模型响应与请求指纹或版本基础不一致。")
            current_revision = current_revision_id or None
            if current_revision != request.expected_current_revision_id:
                raise ReviewRunDomainError("模型响应到达时基础修订已经变化。")
            model_requests[event.subject_id] = replace(
                request,
                status="completed",
                response_hash=event.model_response_hash,
            )
        elif event.event_type == "revision_added":
            if state not in {"evidence_ready", "draft_ready"}:
                raise ReviewRunDomainError("复盘修订只能在证据封存后、正式发布前追加。")
            expected_revision = current_revision_id or None
            if event.expected_current_revision_id != expected_revision:
                raise ReviewRunDomainError("复盘修订的期望当前版本已经过期。")
            current_revision_id = event.review_revision_id
            state = "draft_ready"
        elif event.event_type == "publication_completed":
            if state != "draft_ready":
                raise ReviewRunDomainError("正式发布只能从 draft_ready 状态完成。")
            if not current_revision_id or event.review_revision_id != current_revision_id:
                raise ReviewRunDomainError("publication 未引用当前复盘修订。")
            if event.expected_current_revision_id != current_revision_id:
                raise ReviewRunDomainError("publication 的期望复盘修订已经过期。")
            if manifest.model_use.mode == "required" and not any(
                request.status == "completed" for request in model_requests.values()
            ):
                raise ReviewRunDomainError(
                    "RunManifest 要求模型参与，正式发布前必须完成授权模型请求。"
                )
            publication_id = event.publication_id
            state = "published"
        elif event.event_type == "operation_failed":
            if event.operation_id not in failed_operation_ids:
                failed_operation_ids.append(event.operation_id)
            for request_id, request in tuple(model_requests.items()):
                if request.status != "pending":
                    continue
                if event.subject_id == request_id or event.operation_id == request.operation_id:
                    model_requests[request_id] = replace(request, status="unavailable")

    if any(request.status == "pending" for request in model_requests.values()):
        model_state = "pending"
    elif any(request.status == "completed" for request in model_requests.values()):
        model_state = "completed"
    elif model_requests:
        model_state = "unavailable"
    else:
        model_state = "not_requested"

    if not state:
        raise ReviewRunDomainError("ReviewRun 事件流不能为空。")

    slot_states = tuple(
        CaptureSlotState(
            slot_id=slot.slot_id,
            operation_id=slot.operation_id,
            status=capture_statuses.get(slot.slot_id, "pending"),
        )
        for slot in manifest.captures
    )
    return ReviewRun(
        run_id=manifest.run_id,
        trade_date=manifest.trade_date,
        manifest_hash=manifest.content_hash,
        state=state,
        capture_slots=slot_states,
        evidence_bundle_id=evidence_bundle_id,
        evidence_bundle_hash=evidence_bundle_hash,
        current_review_revision_id=current_revision_id,
        publication_id=publication_id,
        model_state=model_state,
        model_requests=tuple(model_requests.values()),
        failed_operation_ids=tuple(failed_operation_ids),
        applied_event_ids=tuple(event.event_id for event in applied_events),
    )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewRunDomainError(f"{label}必须是 JSON 对象。")
    return value
