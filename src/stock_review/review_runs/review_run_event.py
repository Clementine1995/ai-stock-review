# 本文件定义 ReviewRun 的追加式领域事件，事件本身不负责持久化或副作用执行。

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping

from stock_review.review_runs.canonical_hash import canonical_hash, require_sha256
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.evidence_bundle import CAPTURE_STATUSES


REVIEW_RUN_EVENT_TYPES = {
    "run_started",
    "capture_completed",
    "evidence_bundle_sealed",
    "model_requested",
    "model_completed",
    "revision_added",
    "publication_completed",
    "operation_failed",
}


@dataclass(frozen=True)
class ReviewRunEvent:
    event_id: str
    run_id: str
    event_type: str
    occurred_at: str
    operation_id: str = ""
    subject_id: str = ""
    manifest_hash: str = ""
    capture_slot_id: str = ""
    capture_status: str = ""
    evidence_bundle_id: str = ""
    evidence_bundle_hash: str = ""
    review_revision_id: str = ""
    base_review_revision_id: str | None = None
    expected_current_revision_id: str | None = None
    model_id: str = ""
    model_request_hash: str = ""
    model_response_hash: str = ""
    publication_id: str = ""
    failure_code: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        _require_text(self.event_id, "事件 ID")
        _require_text(self.run_id, "ReviewRun ID")
        if self.event_type not in REVIEW_RUN_EVENT_TYPES:
            raise ReviewRunDomainError(f"未知 ReviewRun 事件类型：{self.event_type}")
        _require_datetime(self.occurred_at, "事件时间")
        require_sha256(self.manifest_hash, "RunManifest 哈希", allow_empty=True)
        require_sha256(self.evidence_bundle_hash, "EvidenceBundle 哈希", allow_empty=True)
        require_sha256(self.model_request_hash, "模型请求哈希", allow_empty=True)
        require_sha256(self.model_response_hash, "模型响应哈希", allow_empty=True)
        require_sha256(self.content_hash, "事件哈希", allow_empty=True)
        self._validate_event_fields()

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        run_id: str,
        event_type: str,
        occurred_at: str,
        operation_id: str = "",
        subject_id: str = "",
        manifest_hash: str = "",
        capture_slot_id: str = "",
        capture_status: str = "",
        evidence_bundle_id: str = "",
        evidence_bundle_hash: str = "",
        review_revision_id: str = "",
        base_review_revision_id: str | None = None,
        expected_current_revision_id: str | None = None,
        model_id: str = "",
        model_request_hash: str = "",
        model_response_hash: str = "",
        publication_id: str = "",
        failure_code: str = "",
    ) -> ReviewRunEvent:
        event = cls(
            event_id=event_id,
            run_id=run_id,
            event_type=event_type,
            occurred_at=occurred_at,
            operation_id=operation_id,
            subject_id=subject_id,
            manifest_hash=manifest_hash,
            capture_slot_id=capture_slot_id,
            capture_status=capture_status,
            evidence_bundle_id=evidence_bundle_id,
            evidence_bundle_hash=evidence_bundle_hash,
            review_revision_id=review_revision_id,
            base_review_revision_id=base_review_revision_id,
            expected_current_revision_id=expected_current_revision_id,
            model_id=model_id,
            model_request_hash=model_request_hash,
            model_response_hash=model_response_hash,
            publication_id=publication_id,
            failure_code=failure_code,
            content_hash="",
        )
        return replace(event, content_hash=event.calculate_hash())

    def calculate_hash(self) -> str:
        return canonical_hash(self._hash_payload())

    def validate_hash(self) -> None:
        require_sha256(self.content_hash, "事件哈希")
        if self.content_hash != self.calculate_hash():
            raise ReviewRunDomainError("ReviewRun 事件内容与哈希不一致。")

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReviewRunEvent:
        expected_revision = value.get("expected_current_revision_id")
        base_revision = value.get("base_review_revision_id")
        event = cls(
            event_id=str(value.get("event_id", "")),
            run_id=str(value.get("run_id", "")),
            event_type=str(value.get("event_type", "")),
            occurred_at=str(value.get("occurred_at", "")),
            operation_id=str(value.get("operation_id", "")),
            subject_id=str(value.get("subject_id", "")),
            manifest_hash=str(value.get("manifest_hash", "")),
            capture_slot_id=str(value.get("capture_slot_id", "")),
            capture_status=str(value.get("capture_status", "")),
            evidence_bundle_id=str(value.get("evidence_bundle_id", "")),
            evidence_bundle_hash=str(value.get("evidence_bundle_hash", "")),
            review_revision_id=str(value.get("review_revision_id", "")),
            base_review_revision_id=(
                None if base_revision is None else str(base_revision)
            ),
            expected_current_revision_id=(
                None if expected_revision is None else str(expected_revision)
            ),
            model_id=str(value.get("model_id", "")),
            model_request_hash=str(value.get("model_request_hash", "")),
            model_response_hash=str(value.get("model_response_hash", "")),
            publication_id=str(value.get("publication_id", "")),
            failure_code=str(value.get("failure_code", "")),
            content_hash=str(value.get("content_hash", "")),
        )
        event.validate_hash()
        return event

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "operation_id": self.operation_id,
            "subject_id": self.subject_id,
            "manifest_hash": self.manifest_hash,
            "capture_slot_id": self.capture_slot_id,
            "capture_status": self.capture_status,
            "evidence_bundle_id": self.evidence_bundle_id,
            "evidence_bundle_hash": self.evidence_bundle_hash,
            "review_revision_id": self.review_revision_id,
            "base_review_revision_id": self.base_review_revision_id,
            "expected_current_revision_id": self.expected_current_revision_id,
            "model_id": self.model_id,
            "model_request_hash": self.model_request_hash,
            "model_response_hash": self.model_response_hash,
            "publication_id": self.publication_id,
            "failure_code": self.failure_code,
        }

    # 每类事件只允许携带当前状态转换所需字段，防止存储层自行解释模糊 payload。
    def _validate_event_fields(self) -> None:
        if self.event_type == "run_started":
            require_sha256(self.manifest_hash, "run_started 的 RunManifest 哈希")
        elif self.event_type == "capture_completed":
            _require_text(self.operation_id, "采集 operation ID")
            _require_text(self.capture_slot_id, "采集槽位 ID")
            if self.capture_status not in CAPTURE_STATUSES:
                raise ReviewRunDomainError("capture_completed 必须记录合法采集终态。")
        elif self.event_type == "evidence_bundle_sealed":
            _require_text(self.evidence_bundle_id, "EvidenceBundle ID")
            require_sha256(self.evidence_bundle_hash, "EvidenceBundle 哈希")
        elif self.event_type in {"model_requested", "model_completed"}:
            _require_text(self.operation_id, "模型 operation ID")
            _require_text(self.subject_id, "模型请求 ID")
            _require_text(self.evidence_bundle_id, "模型请求绑定的 EvidenceBundle ID")
            require_sha256(
                self.evidence_bundle_hash,
                "模型请求绑定的 EvidenceBundle 哈希",
            )
            _require_text(self.model_id, "模型标识")
            require_sha256(self.model_request_hash, "模型请求哈希")
            if self.base_review_revision_id != self.expected_current_revision_id:
                raise ReviewRunDomainError("模型请求的基础修订必须等于期望当前修订。")
            if self.event_type == "model_completed":
                require_sha256(self.model_response_hash, "模型响应哈希")
        elif self.event_type == "revision_added":
            _require_text(self.operation_id, "修订 operation ID")
            _require_text(self.review_revision_id, "复盘修订 ID")
        elif self.event_type == "publication_completed":
            _require_text(self.operation_id, "发布 operation ID")
            _require_text(self.review_revision_id, "复盘修订 ID")
            _require_text(self.publication_id, "publication ID")
        elif self.event_type == "operation_failed":
            _require_text(self.operation_id, "失败 operation ID")
            _require_text(self.failure_code, "失败代码")


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReviewRunDomainError(f"{label}不能为空。")


def _require_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ReviewRunDomainError(f"{label}必须是 ISO 8601 时间。") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewRunDomainError(f"{label}必须包含时区。")
    return parsed
