# 本文件定义单次复盘运行的授权清单与采集槽位，不执行联网或文件写入。

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Mapping

from stock_review.review_runs.canonical_hash import canonical_hash, require_sha256
from stock_review.review_runs.domain_error import ReviewRunDomainError


VALID_MODEL_MODES = {"none", "optional", "required"}

# RunManifest 固定一次日复盘完整生命周期可能写入的业务表和当前投影。
DAILY_REVIEW_DATABASE_TABLES = (
    "schema_versions",
    "review_runs",
    "review_run_events",
    "run_manifests",
    "evidence_captures",
    "evidence_bundles",
    "review_revisions",
    "model_requests",
    "model_responses",
    "finalization_proposals",
    "action_sets",
    "review_publications",
    "current_review_publications",
    "pool_status_events",
    "current_pool_status",
    "observations",
    "observation_result_revisions",
    "current_observation_results",
    "audit_events",
    "audit_outbox",
    "action_set_applications",
)

@dataclass(frozen=True)
class ModelUse:
    mode: str
    model_ids: tuple[str, ...] = ()
    max_requests: int = 0

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODEL_MODES:
            raise ReviewRunDomainError("模型使用模式必须是 none、optional 或 required。")
        _require_unique_texts(self.model_ids, "模型标识")
        if self.max_requests < 0:
            raise ReviewRunDomainError("模型请求上限不能小于 0。")
        if self.mode == "none" and (self.model_ids or self.max_requests):
            raise ReviewRunDomainError("禁用模型时不能声明模型标识或请求次数。")
        if self.mode != "none" and self.max_requests < 1:
            raise ReviewRunDomainError("启用模型时请求上限必须大于 0。")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "model_ids": list(self.model_ids),
            "max_requests": self.max_requests,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelUse:
        return cls(
            mode=str(value.get("mode", "")),
            model_ids=tuple(str(item) for item in value.get("model_ids", ())),
            max_requests=int(value.get("max_requests", 0)),
        )


@dataclass(frozen=True)
class CaptureSlot:
    slot_id: str
    operation_id: str
    source: str
    scope: str
    input_location: str
    output_location: str
    network_required: bool
    write_effects: tuple[str, ...] = ()
    predicate: str = ""
    allowed_objects: tuple[str, ...] = ()
    allowed_dates: tuple[str, ...] = ()
    allowed_fields: tuple[str, ...] = ()
    max_records: int | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("采集槽位 ID", self.slot_id),
            ("采集 operation ID", self.operation_id),
            ("数据源", self.source),
            ("采集范围", self.scope),
            ("输入位置", self.input_location),
            ("输出位置", self.output_location),
        ):
            _require_text(value, label)
        if not isinstance(self.network_required, bool):
            raise ReviewRunDomainError("network_required 必须是布尔值。")
        _require_unique_texts(self.write_effects, "写入影响")
        _require_unique_texts(self.allowed_objects, "允许对象")
        _require_unique_texts(self.allowed_fields, "允许字段")
        _require_unique_texts(self.allowed_dates, "允许日期")
        for allowed_date in self.allowed_dates:
            _require_date(allowed_date, "允许日期")
        if self.max_records is not None and self.max_records < 1:
            raise ReviewRunDomainError("采集记录上限必须大于 0。")

    @property
    def is_conditional(self) -> bool:
        return bool(self.predicate.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "operation_id": self.operation_id,
            "source": self.source,
            "scope": self.scope,
            "input_location": self.input_location,
            "output_location": self.output_location,
            "network_required": self.network_required,
            "write_effects": list(self.write_effects),
            "predicate": self.predicate,
            "allowed_objects": list(self.allowed_objects),
            "allowed_dates": list(self.allowed_dates),
            "allowed_fields": list(self.allowed_fields),
            "max_records": self.max_records,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CaptureSlot:
        max_records = value.get("max_records")
        return cls(
            slot_id=str(value.get("slot_id", "")),
            operation_id=str(value.get("operation_id", "")),
            source=str(value.get("source", "")),
            scope=str(value.get("scope", "")),
            input_location=str(value.get("input_location", "")),
            output_location=str(value.get("output_location", "")),
            network_required=value.get("network_required", False),
            write_effects=tuple(str(item) for item in value.get("write_effects", ())),
            predicate=str(value.get("predicate", "")),
            allowed_objects=tuple(str(item) for item in value.get("allowed_objects", ())),
            allowed_dates=tuple(str(item) for item in value.get("allowed_dates", ())),
            allowed_fields=tuple(str(item) for item in value.get("allowed_fields", ())),
            max_records=None if max_records is None else int(max_records),
        )


@dataclass(frozen=True)
class RunManifest:
    manifest_id: str
    run_id: str
    trade_date: str
    created_at: str
    expires_at: str
    captures: tuple[CaptureSlot, ...]
    model_use: ModelUse
    file_effects: tuple[str, ...]
    database_effects: tuple[str, ...]
    content_hash: str

    def __post_init__(self) -> None:
        _require_text(self.manifest_id, "RunManifest ID")
        _require_text(self.run_id, "ReviewRun ID")
        _require_date(self.trade_date, "交易日期")
        created_at = _require_datetime(self.created_at, "RunManifest 创建时间")
        expires_at = _require_datetime(self.expires_at, "RunManifest 过期时间")
        if expires_at <= created_at:
            raise ReviewRunDomainError("RunManifest 过期时间必须晚于创建时间。")
        if not self.captures:
            raise ReviewRunDomainError("RunManifest 至少需要一个采集槽位。")
        _require_unique_values((item.slot_id for item in self.captures), "采集槽位 ID")
        _require_unique_values((item.operation_id for item in self.captures), "采集 operation ID")
        _require_unique_texts(self.file_effects, "文件影响")
        _require_unique_texts(self.database_effects, "数据库影响")
        require_sha256(self.content_hash, "RunManifest 哈希", allow_empty=True)

    @classmethod
    def create(
        cls,
        *,
        manifest_id: str,
        run_id: str,
        trade_date: str,
        created_at: str,
        expires_at: str,
        captures: tuple[CaptureSlot, ...],
        model_use: ModelUse,
        file_effects: tuple[str, ...] = (),
        database_effects: tuple[str, ...] = (),
    ) -> RunManifest:
        manifest = cls(
            manifest_id=manifest_id,
            run_id=run_id,
            trade_date=trade_date,
            created_at=created_at,
            expires_at=expires_at,
            captures=captures,
            model_use=model_use,
            file_effects=file_effects,
            database_effects=database_effects,
            content_hash="",
        )
        return replace(manifest, content_hash=manifest.calculate_hash())

    def calculate_hash(self) -> str:
        return canonical_hash(self._hash_payload())

    def validate_hash(self) -> None:
        require_sha256(self.content_hash, "RunManifest 哈希")
        if self.content_hash != self.calculate_hash():
            raise ReviewRunDomainError("RunManifest 内容与哈希不一致。")

    def is_expired_at(self, timestamp: str) -> bool:
        return _require_datetime(timestamp, "校验时间") >= _require_datetime(
            self.expires_at,
            "RunManifest 过期时间",
        )

    def capture_by_id(self, slot_id: str) -> CaptureSlot:
        for capture in self.captures:
            if capture.slot_id == slot_id:
                return capture
        raise ReviewRunDomainError(f"RunManifest 不包含采集槽位：{slot_id}")

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RunManifest:
        manifest = cls(
            manifest_id=str(value.get("manifest_id", "")),
            run_id=str(value.get("run_id", "")),
            trade_date=str(value.get("trade_date", "")),
            created_at=str(value.get("created_at", "")),
            expires_at=str(value.get("expires_at", "")),
            captures=tuple(
                CaptureSlot.from_dict(_require_mapping(item, "采集槽位"))
                for item in value.get("captures", ())
            ),
            model_use=ModelUse.from_dict(
                _require_mapping(value.get("model_use", {}), "模型使用")
            ),
            file_effects=tuple(str(item) for item in value.get("file_effects", ())),
            database_effects=tuple(
                str(item) for item in value.get("database_effects", ())
            ),
            content_hash=str(value.get("content_hash", "")),
        )
        manifest.validate_hash()
        return manifest

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "captures": [capture.to_dict() for capture in self.captures],
            "model_use": self.model_use.to_dict(),
            "file_effects": list(self.file_effects),
            "database_effects": list(self.database_effects),
        }


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
