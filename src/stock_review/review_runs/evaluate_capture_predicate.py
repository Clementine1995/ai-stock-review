# 本文件解释 RunManifest 中有限、可审计的条件采集谓词，不执行任意表达式。

from __future__ import annotations

from typing import Any, Mapping

from stock_review.review_runs.domain_error import ReviewRunDomainError


# 条件语法只支持明确缺口与槽位终态，避免清单把可执行代码伪装成谓词。
def evaluate_capture_predicate(
    predicate: str,
    normalized_evidence: Mapping[str, Any],
    capture_statuses: Mapping[str, str],
) -> bool:
    value = predicate.strip()
    if value == "always":
        return True
    if value.startswith("missing:"):
        path = value.removeprefix("missing:").strip()
        if not path:
            raise ReviewRunDomainError("missing 谓词必须指定证据字段路径。")
        resolved, exists = _resolve_path(normalized_evidence, path)
        return not exists or resolved in (None, "", [], {})
    if value.startswith("no_records:"):
        path = value.removeprefix("no_records:").strip()
        resolved, exists = _resolve_path(normalized_evidence, path)
        if not exists:
            return True
        if not isinstance(resolved, list):
            raise ReviewRunDomainError("no_records 谓词目标必须是数组。")
        return not resolved
    if value.startswith("status:") and "=" in value:
        slot_id, expected = value.removeprefix("status:").split("=", 1)
        slot_id = slot_id.strip()
        expected = expected.strip()
        if expected not in {"available", "unavailable", "skipped", "pending"}:
            raise ReviewRunDomainError("status 谓词包含未知采集状态。")
        return capture_statuses.get(slot_id, "pending") == expected
    raise ReviewRunDomainError(f"不支持的条件采集谓词：{predicate}")


def merge_normalized_evidence(payloads: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for payload in payloads:
        normalized = payload.get("normalized")
        if not isinstance(normalized, Mapping):
            continue
        for key, value in normalized.items():
            if key in {"source", "sample_date", "schema_version"}:
                continue
            if isinstance(value, list):
                existing = merged.setdefault(key, [])
                if not isinstance(existing, list):
                    raise ReviewRunDomainError(f"证据字段 {key} 在不同采集结果中的类型不一致。")
                existing.extend(value)
            elif isinstance(value, Mapping):
                existing = merged.setdefault(key, {})
                if not isinstance(existing, dict):
                    raise ReviewRunDomainError(f"证据字段 {key} 在不同采集结果中的类型不一致。")
                for nested_key, nested_value in value.items():
                    if nested_key in existing and existing[nested_key] != nested_value:
                        raise ReviewRunDomainError(
                            f"证据字段 {key}.{nested_key} 在不同来源中存在内容冲突。"
                        )
                    existing[nested_key] = nested_value
            elif key in merged and merged[key] != value:
                raise ReviewRunDomainError(f"证据字段 {key} 在不同来源中存在内容冲突。")
            else:
                merged[key] = value
    return merged


def _resolve_path(value: Mapping[str, Any], path: str) -> tuple[Any, bool]:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None, False
        current = current[part]
    return current, True
