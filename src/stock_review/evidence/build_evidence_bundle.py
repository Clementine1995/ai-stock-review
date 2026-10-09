# 本文件负责把已授权采集信封规范化为不可变捕获记录，并以内容哈希保存证据正文。

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping

from stock_review.review_runs.canonical_hash import canonical_hash, canonical_json
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.evidence_bundle import EvidenceBundle, EvidenceCapture
from stock_review.review_runs.run_manifest import CaptureSlot, RunManifest


CAPTURE_PAYLOAD_SCHEMA_VERSION = 1


# 外部信封只能填充 RunManifest 中已声明的槽位，日期、来源、范围和 operation 均须完全一致。
def build_evidence_capture(
    manifest: RunManifest,
    slot_id: str,
    operation_id: str,
    envelope: Mapping[str, Any],
    *,
    captured_at: datetime | None = None,
) -> tuple[EvidenceCapture, dict[str, Any]]:
    manifest.validate_hash()
    slot = manifest.capture_by_id(slot_id)
    if operation_id != slot.operation_id:
        raise ReviewRunDomainError(f"采集槽位 {slot_id} 的 operation ID 不匹配。")
    _require_envelope_identity(manifest, slot, envelope)
    status = str(envelope.get("status") or "available")
    if status not in {"available", "unavailable", "skipped"}:
        raise ReviewRunDomainError("采集信封状态必须是 available、unavailable 或 skipped。")
    if status == "skipped" and not slot.is_conditional:
        raise ReviewRunDomainError("只有条件采集槽位可以标记为 skipped。")

    normalized = envelope.get("normalized")
    failure_reason = str(envelope.get("failure_reason") or "").strip()
    missing_fields = _normalize_unique_texts(envelope.get("missing_fields", ()), "缺失字段")
    if status == "available":
        if not isinstance(normalized, (Mapping, list)):
            raise ReviewRunDomainError("可用采集信封必须包含 normalized JSON 对象或数组。")
        _validate_normalized_scope(slot, normalized)
    else:
        if not failure_reason:
            raise ReviewRunDomainError("不可用或跳过的采集信封必须填写 failure_reason。")
        normalized = None

    sample_date = str(envelope.get("sample_date") or "")
    if status == "available" and sample_date != manifest.trade_date:
        raise ReviewRunDomainError("可用采集信封的 sample_date 必须等于交易日期。")
    stored_payload = {
        "schema_version": CAPTURE_PAYLOAD_SCHEMA_VERSION,
        "run_id": manifest.run_id,
        "manifest_hash": manifest.content_hash,
        "slot_id": slot.slot_id,
        "operation_id": slot.operation_id,
        "trade_date": manifest.trade_date,
        "sample_date": sample_date,
        "source": slot.source,
        "scope": slot.scope,
        "status": status,
        "normalized": normalized,
        "missing_fields": list(missing_fields),
        "failure_reason": failure_reason,
    }
    raw_payload = envelope.get("raw", envelope)
    capture = EvidenceCapture(
        slot_id=slot.slot_id,
        operation_id=slot.operation_id,
        source=slot.source,
        scope=slot.scope,
        status=status,
        captured_at=_timestamp(captured_at).isoformat(),
        sample_date=sample_date,
        raw_response_hash=(canonical_hash(raw_payload) if status == "available" else ""),
        normalized_evidence_hash=(canonical_hash(normalized) if status == "available" else ""),
        missing_fields=missing_fields,
        failure_reason=failure_reason,
    )
    stored_payload["capture"] = capture.to_dict()
    stored_payload["payload_hash"] = canonical_hash(
        {key: value for key, value in stored_payload.items() if key != "payload_hash"}
    )
    return capture, stored_payload


# EvidenceBundle 只引用已保存正文的哈希；保存文件必须能反向证明与捕获记录一致。
def verify_capture_payload(payload: Mapping[str, Any], capture: EvidenceCapture) -> None:
    supplied_hash = str(payload.get("payload_hash") or "")
    calculated_hash = canonical_hash(
        {key: value for key, value in payload.items() if key != "payload_hash"}
    )
    if supplied_hash != calculated_hash:
        raise ReviewRunDomainError("采集证据文件内容与 payload_hash 不一致。")
    if payload.get("capture") != capture.to_dict():
        raise ReviewRunDomainError("采集证据文件中的捕获元数据与数据库记录不一致。")
    if capture.status == "available":
        if canonical_hash(payload.get("normalized")) != capture.normalized_evidence_hash:
            raise ReviewRunDomainError("采集证据正文与 EvidenceCapture 哈希不一致。")


# 同卷临时文件写完后以硬链接原子占位；目标已存在时只接受逐字节相同内容。
def write_capture_payload(output_path: Path, payload: Mapping[str, Any]) -> Path:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_json(payload) + "\n"
    encoded = content.encode("utf-8")
    if target.exists():
        if target.read_bytes() != encoded:
            raise ReviewRunDomainError(f"不可变采集证据已存在且内容不同：{target}")
        return target
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(encoded)
        try:
            # link 的目标创建具有 O_EXCL 语义，不会像 replace 一样覆盖并发胜出的不可变文件。
            os.link(temporary_path, target)
        except FileExistsError:
            try:
                existing_content = target.read_bytes()
            except OSError as error:
                raise ReviewRunDomainError(
                    f"不可变采集证据并发发布后无法读取：{target}：{error}"
                ) from error
            if existing_content != encoded:
                raise ReviewRunDomainError(f"不可变采集证据已存在且内容不同：{target}")
        except OSError as error:
            raise ReviewRunDomainError(
                f"不可变采集证据无法原子发布：{target}：{error}"
            ) from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return target


def read_capture_payload(input_path: Path, capture: EvidenceCapture) -> dict[str, Any]:
    try:
        value = json.loads(Path(input_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewRunDomainError(f"采集证据文件读取失败：{input_path}：{error}") from error
    if not isinstance(value, dict):
        raise ReviewRunDomainError("采集证据文件顶层必须是 JSON 对象。")
    verify_capture_payload(value, capture)
    return value


def seal_evidence_bundle(
    manifest: RunManifest,
    captures: tuple[EvidenceCapture, ...],
    *,
    evidence_bundle_id: str,
    sealed_at: datetime | None = None,
) -> EvidenceBundle:
    return EvidenceBundle.seal(
        evidence_bundle_id=evidence_bundle_id,
        manifest=manifest,
        captures=captures,
        sealed_at=_timestamp(sealed_at).isoformat(),
    )


def _require_envelope_identity(
    manifest: RunManifest,
    slot: CaptureSlot,
    envelope: Mapping[str, Any],
) -> None:
    expected = {
        "run_id": manifest.run_id,
        "trade_date": manifest.trade_date,
        "source": slot.source,
        "scope": slot.scope,
        "operation_id": slot.operation_id,
    }
    for field_name, expected_value in expected.items():
        if str(envelope.get(field_name) or "") != expected_value:
            raise ReviewRunDomainError(f"采集信封的 {field_name} 与 RunManifest 不一致。")


# 对象和记录数量属于 manifest 授权边界；字段白名单按 normalized 顶层或点路径首段检查。
def _validate_normalized_scope(slot: CaptureSlot, normalized: Mapping[str, Any] | list[Any]) -> None:
    records = _scope_records(slot, normalized)
    if slot.max_records is not None:
        if records is not None and len(records) > slot.max_records:
            raise ReviewRunDomainError(f"采集槽位 {slot.slot_id} 的记录数超过 RunManifest 上限。")
    if slot.allowed_objects:
        if records is None:
            raise ReviewRunDomainError("限制对象的采集结果必须包含 records 数组。")
        allowed = set(slot.allowed_objects)
        for record in records:
            if not isinstance(record, Mapping):
                raise ReviewRunDomainError("采集 records 中每一项都必须是 JSON 对象。")
            identity = str(
                record.get("code")
                or record.get("sector_id")
                or record.get("name")
                or ""
            )
            if identity not in allowed:
                raise ReviewRunDomainError(f"采集对象超出 RunManifest 范围：{identity or '未提供标识'}")
    if slot.allowed_fields and isinstance(normalized, Mapping):
        _validate_allowed_fields(normalized, slot.allowed_fields, slot.scope)


def _scope_records(
    slot: CaptureSlot,
    normalized: Mapping[str, Any] | list[Any],
) -> list[Any] | None:
    if isinstance(normalized, list):
        return normalized
    normalized_scope = slot.scope.replace("-", "_")
    for field_name in (
        "records",
        slot.scope,
        normalized_scope,
        "stocks" if slot.scope == "snapshot" else "",
    ):
        if field_name and isinstance(normalized.get(field_name), list):
            return normalized[field_name]
    return None


# 点路径白名单不仅限制顶层，也限制对象记录和嵌套 mapping 的实际字段。
def _validate_allowed_fields(
    normalized: Mapping[str, Any],
    allowed_fields: tuple[str, ...],
    scope: str,
) -> None:
    metadata_fields = {
        "schema_version",
        "trade_date",
        "sample_date",
        "source",
        "quality",
    }
    path_map: dict[str, list[tuple[str, ...]]] = {}
    for field in allowed_fields:
        parts = tuple(part for part in field.split(".") if part)
        if parts:
            path_map.setdefault(parts[0], []).append(parts[1:])
    unexpected = set(normalized) - set(path_map) - metadata_fields - {"records"}
    if unexpected:
        raise ReviewRunDomainError(
            f"采集字段超出 RunManifest 范围：{', '.join(sorted(unexpected))}"
        )
    for root, tails in path_map.items():
        if root not in normalized or () in tails:
            continue
        _validate_nested_paths(normalized[root], tails, root)
    if "records" in normalized:
        record_root = scope if scope in path_map else (next(iter(path_map)) if len(path_map) == 1 else "")
        if not record_root:
            raise ReviewRunDomainError("采集 records 无法映射到 RunManifest 字段白名单。")
        tails = path_map[record_root]
        if () not in tails:
            _validate_nested_paths(normalized["records"], tails, record_root)


def _validate_nested_paths(
    value: Any,
    allowed_paths: list[tuple[str, ...]],
    prefix: str,
) -> None:
    if isinstance(value, list):
        for position, item in enumerate(value):
            _validate_nested_paths(item, allowed_paths, f"{prefix}[{position}]")
        return
    if not isinstance(value, Mapping):
        raise ReviewRunDomainError(f"采集字段 {prefix} 无法满足 RunManifest 点路径白名单。")
    allowed_children = {path[0] for path in allowed_paths if path}
    unexpected = set(value) - allowed_children
    if unexpected:
        names = ", ".join(sorted(f"{prefix}.{name}" for name in unexpected))
        raise ReviewRunDomainError(f"采集字段超出 RunManifest 范围：{names}")
    for child in allowed_children:
        child_paths = [path[1:] for path in allowed_paths if path and path[0] == child]
        if child in value and () not in child_paths:
            _validate_nested_paths(value[child], child_paths, f"{prefix}.{child}")


def _normalize_unique_texts(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ReviewRunDomainError(f"{label}必须是 JSON 数组。")
    normalized = tuple(str(item).strip() for item in value)
    if any(not item for item in normalized):
        raise ReviewRunDomainError(f"{label}不能包含空值。")
    if len(normalized) != len(set(normalized)):
        raise ReviewRunDomainError(f"{label}不能重复。")
    return normalized


def _timestamp(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ReviewRunDomainError("业务时间必须包含时区。")
    return timestamp.astimezone(timezone.utc).replace(microsecond=0)
