# 本文件定义采集结果与不可变 EvidenceBundle 的封存规则，不读取数据源或写文件。

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Mapping

from stock_review.review_runs.canonical_hash import canonical_hash, require_sha256
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.run_manifest import RunManifest


CAPTURE_STATUSES = {"available", "unavailable", "skipped"}


@dataclass(frozen=True)
class EvidenceCapture:
    slot_id: str
    operation_id: str
    source: str
    scope: str
    status: str
    captured_at: str
    sample_date: str = ""
    raw_response_hash: str = ""
    normalized_evidence_hash: str = ""
    missing_fields: tuple[str, ...] = ()
    failure_reason: str = ""

    def __post_init__(self) -> None:
        for label, value in (
            ("采集槽位 ID", self.slot_id),
            ("采集 operation ID", self.operation_id),
            ("数据源", self.source),
            ("采集范围", self.scope),
        ):
            _require_text(value, label)
        if self.status not in CAPTURE_STATUSES:
            raise ReviewRunDomainError("采集状态必须是 available、unavailable 或 skipped。")
        _require_datetime(self.captured_at, "采集时间")
        if self.sample_date:
            _require_date(self.sample_date, "样本日期")
        require_sha256(self.raw_response_hash, "原始响应哈希", allow_empty=True)
        require_sha256(self.normalized_evidence_hash, "标准化证据哈希", allow_empty=True)
        _require_unique_texts(self.missing_fields, "缺失字段")
        if self.status == "available":
            if not self.sample_date:
                raise ReviewRunDomainError("可用采集结果必须记录样本日期。")
            require_sha256(self.raw_response_hash, "原始响应哈希")
            require_sha256(self.normalized_evidence_hash, "标准化证据哈希")
        elif not self.failure_reason.strip():
            raise ReviewRunDomainError("不可用或跳过的采集结果必须记录原因。")

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "operation_id": self.operation_id,
            "source": self.source,
            "scope": self.scope,
            "status": self.status,
            "captured_at": self.captured_at,
            "sample_date": self.sample_date,
            "raw_response_hash": self.raw_response_hash,
            "normalized_evidence_hash": self.normalized_evidence_hash,
            "missing_fields": list(self.missing_fields),
            "failure_reason": self.failure_reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceCapture:
        return cls(
            slot_id=str(value.get("slot_id", "")),
            operation_id=str(value.get("operation_id", "")),
            source=str(value.get("source", "")),
            scope=str(value.get("scope", "")),
            status=str(value.get("status", "")),
            captured_at=str(value.get("captured_at", "")),
            sample_date=str(value.get("sample_date", "")),
            raw_response_hash=str(value.get("raw_response_hash", "")),
            normalized_evidence_hash=str(value.get("normalized_evidence_hash", "")),
            missing_fields=tuple(str(item) for item in value.get("missing_fields", ())),
            failure_reason=str(value.get("failure_reason", "")),
        )


@dataclass(frozen=True)
class EvidenceBundle:
    evidence_bundle_id: str
    run_id: str
    trade_date: str
    manifest_hash: str
    captures: tuple[EvidenceCapture, ...]
    sealed_at: str
    content_hash: str

    def __post_init__(self) -> None:
        _require_text(self.evidence_bundle_id, "EvidenceBundle ID")
        _require_text(self.run_id, "ReviewRun ID")
        _require_date(self.trade_date, "交易日期")
        require_sha256(self.manifest_hash, "RunManifest 哈希")
        _require_datetime(self.sealed_at, "EvidenceBundle 封存时间")
        _require_unique_values((item.slot_id for item in self.captures), "采集槽位 ID")
        _require_unique_values((item.operation_id for item in self.captures), "采集 operation ID")
        require_sha256(self.content_hash, "EvidenceBundle 哈希", allow_empty=True)

    @classmethod
    def seal(
        cls,
        *,
        evidence_bundle_id: str,
        manifest: RunManifest,
        captures: tuple[EvidenceCapture, ...],
        sealed_at: str,
    ) -> EvidenceBundle:
        manifest.validate_hash()
        capture_by_slot = {capture.slot_id: capture for capture in captures}
        expected_slot_ids = {slot.slot_id for slot in manifest.captures}
        if set(capture_by_slot) != expected_slot_ids or len(captures) != len(manifest.captures):
            missing = sorted(expected_slot_ids - set(capture_by_slot))
            extra = sorted(set(capture_by_slot) - expected_slot_ids)
            details = []
            if missing:
                details.append(f"缺少 {','.join(missing)}")
            if extra:
                details.append(f"多出 {','.join(extra)}")
            raise ReviewRunDomainError(
                "EvidenceBundle 必须为每个 capture slot 提供唯一终态"
                + (f"：{'；'.join(details)}" if details else "。")
            )

        ordered_captures = tuple(capture_by_slot[slot.slot_id] for slot in manifest.captures)
        for slot, capture in zip(manifest.captures, ordered_captures, strict=True):
            _validate_capture_against_slot(manifest, slot, capture)

        bundle = cls(
            evidence_bundle_id=evidence_bundle_id,
            run_id=manifest.run_id,
            trade_date=manifest.trade_date,
            manifest_hash=manifest.content_hash,
            captures=ordered_captures,
            sealed_at=sealed_at,
            content_hash="",
        )
        return replace(bundle, content_hash=bundle.calculate_hash())

    def calculate_hash(self) -> str:
        return canonical_hash(self._hash_payload())

    def validate_hash(self) -> None:
        require_sha256(self.content_hash, "EvidenceBundle 哈希")
        if self.content_hash != self.calculate_hash():
            raise ReviewRunDomainError("EvidenceBundle 内容与哈希不一致。")

    def validate_against_manifest(self, manifest: RunManifest) -> None:
        self.validate_hash()
        manifest.validate_hash()
        if self.run_id != manifest.run_id or self.trade_date != manifest.trade_date:
            raise ReviewRunDomainError("EvidenceBundle 与 RunManifest 的运行或交易日期不一致。")
        if self.manifest_hash != manifest.content_hash:
            raise ReviewRunDomainError("EvidenceBundle 未绑定当前 RunManifest。")
        expected_slot_ids = tuple(slot.slot_id for slot in manifest.captures)
        if tuple(capture.slot_id for capture in self.captures) != expected_slot_ids:
            raise ReviewRunDomainError("EvidenceBundle 的采集槽位顺序或集合与 RunManifest 不一致。")
        for slot, capture in zip(manifest.captures, self.captures, strict=True):
            _validate_capture_against_slot(manifest, slot, capture)

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceBundle:
        bundle = cls(
            evidence_bundle_id=str(value.get("evidence_bundle_id", "")),
            run_id=str(value.get("run_id", "")),
            trade_date=str(value.get("trade_date", "")),
            manifest_hash=str(value.get("manifest_hash", "")),
            captures=tuple(
                EvidenceCapture.from_dict(_require_mapping(item, "EvidenceCapture"))
                for item in value.get("captures", ())
            ),
            sealed_at=str(value.get("sealed_at", "")),
            content_hash=str(value.get("content_hash", "")),
        )
        bundle.validate_hash()
        return bundle

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "evidence_bundle_id": self.evidence_bundle_id,
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "manifest_hash": self.manifest_hash,
            "captures": [capture.to_dict() for capture in self.captures],
            "sealed_at": self.sealed_at,
        }


# 封存时重新核对槽位边界，避免外部信封借合法 slot 注入其它来源或日期。
def _validate_capture_against_slot(
    manifest: RunManifest,
    slot: Any,
    capture: EvidenceCapture,
) -> None:
    if capture.operation_id != slot.operation_id:
        raise ReviewRunDomainError(f"采集槽位 {slot.slot_id} 的 operation ID 不匹配。")
    if capture.source != slot.source or capture.scope != slot.scope:
        raise ReviewRunDomainError(f"采集槽位 {slot.slot_id} 的来源或范围不匹配。")
    if capture.status == "skipped" and not slot.is_conditional:
        raise ReviewRunDomainError(f"非条件采集槽位 {slot.slot_id} 不能标记为 skipped。")
    if capture.status == "available" and slot.allowed_dates:
        if capture.sample_date not in slot.allowed_dates:
            raise ReviewRunDomainError(f"采集槽位 {slot.slot_id} 的样本日期超出授权范围。")
    if capture.status == "available" and not slot.allowed_dates:
        if capture.sample_date != manifest.trade_date:
            raise ReviewRunDomainError(f"采集槽位 {slot.slot_id} 的样本日期与交易日期不一致。")


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReviewRunDomainError(f"{label}不能为空。")


def _require_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ReviewRunDomainError(f"{label}必须是 YYYY-MM-DD。") from error


def _require_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ReviewRunDomainError(f"{label}必须是 ISO 8601 时间。") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewRunDomainError(f"{label}必须包含时区。")
    return parsed


def _require_unique_texts(values: tuple[str, ...], label: str) -> None:
    for value in values:
        _require_text(value, label)
    _require_unique_values(values, label)


def _require_unique_values(values: Any, label: str) -> None:
    normalized = tuple(values)
    if len(normalized) != len(set(normalized)):
        raise ReviewRunDomainError(f"{label}不能重复。")


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewRunDomainError(f"{label}必须是 JSON 对象。")
    return value
