# 本文件生成并提交 Observation 结果修订，基础 Observation 保持不可变且不保存当前状态。

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from stock_review.review_runs.canonical_hash import canonical_hash
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.action_set import validate_observation_result_fields
from stock_review.review_runs.run_daily_review import (
    load_run_manifest,
    require_repository_authorized,
)
from stock_review.storage.review_run_repository import ReviewRunRepository


DEFAULT_RESULT_ACTION_LIFETIME = timedelta(minutes=20)


# propose-result 只读目标 Observation 和当前结果头，固定用户即将确认的差异与内容哈希。
def propose_observation_result(
    observation_id: str,
    expected_result_revision_id: str | None,
    status: str,
    actual_result: str,
    reason: str,
    evidence_references: tuple[str, ...],
    repository: ReviewRunRepository,
    *,
    note: str = "",
    now: datetime | None = None,
    lifetime: timedelta = DEFAULT_RESULT_ACTION_LIFETIME,
) -> dict[str, Any]:
    observation = repository.get_observation(observation_id)
    if observation is None:
        raise ReviewRunDomainError(f"未找到 Observation：{observation_id}")
    current = repository.get_current_observation_result(observation_id)
    actual_expected = current["result_revision_id"] if current is not None else None
    if actual_expected != expected_result_revision_id:
        raise ReviewRunDomainError("Observation 的期望结果修订已经过期。")
    validate_observation_result(status, actual_result, reason, evidence_references)
    timestamp = _timestamp(now)
    if lifetime <= timedelta(0):
        raise ReviewRunDomainError("ObservationResultAction 有效期必须大于 0。")
    expires_at = timestamp + lifetime
    identity_hash = canonical_hash(
        {
            "observation_id": observation_id,
            "expected_result_revision_id": expected_result_revision_id,
            "status": status,
            "actual_result": actual_result.strip(),
            "reason": reason.strip(),
            "evidence_references": list(evidence_references),
            "note": note.strip(),
            # 确认窗口进入身份，失败暂存的旧动作过期后不会阻塞同内容重新提案。
            "created_at": timestamp.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
    )
    payload = {
        "action_id": f"OBS-RESULT-ACTION-{identity_hash[:20]}",
        "action_set_id": f"ACTIONSET-OBS-RESULT-{identity_hash[:20]}",
        "result_revision_id": f"OBS-RESULT-{identity_hash[:20]}",
        "observation_id": observation_id,
        "expected_result_revision_id": expected_result_revision_id,
        "status": status,
        "actual_result": actual_result.strip(),
        "reason": reason.strip(),
        "evidence_references": list(evidence_references),
        "note": note.strip(),
        "created_at": timestamp.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    return {**payload, "content_hash": canonical_hash(payload)}


# record-result 把结果修订与审计 outbox 放进一个存储 ActionSet，版本冲突时整体失败。
def record_observation_result(
    action: Mapping[str, Any],
    action_hash: str,
    repository: ReviewRunRepository,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    supplied_hash = str(action.get("content_hash") or "")
    calculated_hash = canonical_hash(
        {key: value for key, value in action.items() if key != "content_hash"}
    )
    if supplied_hash != calculated_hash or supplied_hash != action_hash:
        raise ReviewRunDomainError("ObservationResultAction 内容与哈希不一致。")
    action_set_id = str(action.get("action_set_id") or "")
    applied_audit = repository.get_audit_event(f"AUDIT-{action_set_id}")
    if applied_audit is not None:
        stored_action_set = repository.get_action_set(action_set_id)
        stored_action_hash = (
            stored_action_set.get("payload", {})
            .get("audit", {})
            .get("payload", {})
            .get("action_hash")
            if stored_action_set is not None
            else None
        )
        if stored_action_hash != action_hash:
            raise ReviewRunDomainError("已应用 ActionSet 与当前结果 action 不一致。")
        # 已提交动作的重放用于恢复调用方状态，不再受首次提交的有效期限制。
        return repository.apply_action_set(action_set_id)
    timestamp = _timestamp(now)
    created_at = _parse_action_timestamp(action.get("created_at"), "创建时间")
    expires_at = _parse_action_timestamp(action.get("expires_at"), "过期时间")
    if created_at >= expires_at:
        raise ReviewRunDomainError("ObservationResultAction 创建时间必须早于过期时间。")
    if timestamp < created_at:
        raise ReviewRunDomainError("ObservationResultAction 尚未到创建时间。")
    if timestamp >= expires_at:
        raise ReviewRunDomainError("ObservationResultAction 已过期。")
    observation_id = str(action.get("observation_id") or "")
    observation = repository.get_observation(observation_id)
    if observation is None:
        raise ReviewRunDomainError(f"未找到 Observation：{observation_id}")
    status = str(action.get("status") or "")
    actual_result = str(action.get("actual_result") or "")
    reason = str(action.get("reason") or "")
    references_value = action.get("evidence_references")
    if not isinstance(references_value, list):
        raise ReviewRunDomainError("回填证据引用必须是 JSON 数组。")
    references = tuple(str(item) for item in references_value)
    validate_observation_result(status, actual_result, reason, references)
    run_id = observation["run_id"]
    revision_id = observation["review_revision_id"]
    manifest = load_run_manifest(repository, run_id)
    require_repository_authorized(manifest, repository)
    result_revision_id = str(action.get("result_revision_id") or "")
    expected_id = action.get("expected_result_revision_id")
    # 结果修订属于用户已确认 action 的不可变内容，重放时不能混入本次执行时钟。
    result_payload = {
        "observation_id": observation_id,
        "result_revision_id": result_revision_id,
        "status": status,
        "actual_result": actual_result.strip(),
        "reason": reason.strip(),
        "evidence_references": list(references),
        "note": str(action.get("note") or "").strip(),
        "recorded_at": created_at.isoformat(),
        "supersedes_result_revision_id": expected_id,
    }
    repository.save_action_set(
        action_set_id,
        run_id,
        revision_id,
        manifest.manifest_id,
        {
            "publication": None,
            "pool_actions": [],
            "observation_creates": [],
            "observation_result_actions": [
                {
                    "observation_id": observation_id,
                    "result_revision_id": result_revision_id,
                    "status": status,
                    "payload": result_payload,
                    "expected_result_revision_id": expected_id,
                }
            ],
            "audit": {
                "audit_event_id": f"AUDIT-{action_set_id}",
                "event_type": "observation record-result",
                "payload": {
                    "command": "observation record-result",
                    "run_id": run_id,
                    "observation_id": observation_id,
                    "result_revision_id": result_revision_id,
                    "action_hash": action_hash,
                    "status": "completed",
                },
                "outbox_id": f"OUTBOX-{action_set_id}",
                "outbox_payload": {
                    "command": "observation record-result",
                    "run_id": run_id,
                    "observation_id": observation_id,
                    "status": "completed",
                },
            },
        },
    )
    return repository.apply_action_set(action_set_id)


def list_due_observations(
    repository: ReviewRunRepository,
    due_on_or_before: str,
) -> list[dict[str, Any]]:
    return repository.list_observations(due_on_or_before=due_on_or_before)


def validate_observation_result(
    status: str,
    actual_result: str,
    reason: str,
    evidence_references: tuple[str, ...],
) -> None:
    validate_observation_result_fields(
        status,
        actual_result,
        reason,
        evidence_references,
    )


def _timestamp(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ReviewRunDomainError("业务时间必须包含时区。")
    return timestamp.astimezone(timezone.utc).replace(microsecond=0)


# 提交阶段重新校验已确认 action 的业务时间，防止重算公开哈希后写入非法不可变时间。
def _parse_action_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ReviewRunDomainError(f"ObservationResultAction {label}无效。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ReviewRunDomainError(f"ObservationResultAction {label}无效。") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewRunDomainError(f"ObservationResultAction {label}必须包含时区。")
    normalized = parsed.astimezone(timezone.utc).replace(microsecond=0)
    if value != normalized.isoformat():
        raise ReviewRunDomainError(
            f"ObservationResultAction {label}必须使用 UTC 秒级 ISO 8601 格式。"
        )
    return normalized
