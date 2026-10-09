# 本文件联合生成最终 RevisionBundle、ActionSet 与正式报告，并执行可恢复发布协议。

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Mapping

from stock_review.observations.review_due_observations import (
    validate_observation_result,
)
from stock_review.reports.render_review_run import render_review_run
from stock_review.review_runs.action_set import (
    ActionSet,
    BusinessAction,
    FINALIZATION_DATABASE_EFFECTS,
    FinalizationProposal,
    VersionExpectation,
)
from stock_review.review_runs.canonical_hash import canonical_hash
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.evidence_bundle import EvidenceBundle
from stock_review.review_runs.review_revision import (
    PlanItem,
    QualityAssessment,
    RejectedPlanItem,
    RevisionBundle,
    StepAssessment,
    assess_revision_quality,
    validate_revision_bundle,
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


DEFAULT_PROPOSAL_LIFETIME = timedelta(minutes=20)


# propose-actions 只读当前草案与版本头，输出一个可供最终确认的完整联合对象。
def propose_daily_actions(
    run_id: str,
    review_revision_id: str,
    selections: Mapping[str, Any],
    repository: ReviewRunRepository,
    *,
    output_dir: Path = Path("reports") / "daily",
    now: datetime | None = None,
    proposal_lifetime: timedelta = DEFAULT_PROPOSAL_LIFETIME,
) -> tuple[FinalizationProposal, str]:
    if proposal_lifetime <= timedelta(0):
        raise ReviewRunDomainError("FinalizationProposal 有效期必须大于 0。")
    manifest = load_run_manifest(repository, run_id)
    timestamp = require_manifest_active(manifest, now=now)
    projection = load_run_projection(repository, run_id)
    if projection.state != "draft_ready":
        raise ReviewRunDomainError("propose-actions 只能基于 draft_ready ReviewRun。")
    if projection.current_review_revision_id != review_revision_id:
        raise ReviewRunDomainError("propose-actions 选择的草案不是当前修订。")
    base_record = repository.get_review_revision(review_revision_id)
    if base_record is None:
        raise ReviewRunDomainError(f"未找到复盘修订：{review_revision_id}")
    base = RevisionBundle.from_dict(base_record["payload"])
    if base.revision_kind != "draft":
        raise ReviewRunDomainError("propose-actions 的基础修订必须是 draft。")
    _require_required_model_result(manifest, projection, base)

    expires_at = (timestamp + proposal_lifetime).isoformat()
    selection_hash = canonical_hash(
        {
            "run_id": base.run_id,
            "base_review_revision_id": base.review_revision_id,
            "selections": dict(selections),
            # 确认窗口进入身份，过期提案重新生成时不能复用已落盘的不可变 ID。
            "proposal_created_at": timestamp.isoformat(),
            "proposal_expires_at": expires_at,
        }
    )
    final_revision = _build_final_revision(base, selections, timestamp, selection_hash)
    _validate_current_revision_evidence(
        final_revision,
        manifest,
        projection,
        repository,
    )
    quality = validate_revision_bundle(final_revision)
    publication_id = f"PUB-{base.trade_date.replace('-', '')}-{selection_hash[:12]}"
    action_set_id = f"ACTIONSET-{selection_hash[:20]}"
    proposal_id = f"PROPOSAL-{selection_hash[:20]}"
    observation_actions = _build_observation_actions(final_revision, timestamp, selection_hash)
    pool_actions = _build_pool_actions(selections, repository, selection_hash)
    result_actions = _build_result_actions(selections, repository, selection_hash)
    current_publication = repository.get_current_review_publication(base.trade_date)
    expected_publication_id = (
        current_publication["publication_id"] if current_publication is not None else None
    )
    publication_action = BusinessAction.create(
        action_id=f"ACT-PUBLISH-{selection_hash[:16]}",
        action_type="publish_review",
        target_type="review_publication",
        target_id=base.trade_date,
        payload={
            "trade_date": base.trade_date,
            "run_id": base.run_id,
            "review_revision_id": final_revision.review_revision_id,
            "review_revision_hash": final_revision.content_hash,
            # 正式报告哈希在下方首次渲染后回填，随后重建 publication 动作与 ActionSet。
            "report_hash": "0" * 64,
        },
    )
    preliminary_actions = (publication_action, *pool_actions, *observation_actions, *result_actions)
    expectations = _build_expectations(
        preliminary_actions,
        repository,
        expected_publication_id=expected_publication_id,
    )
    preliminary_projection = _report_projection(
        manifest,
        final_revision,
        quality.to_dict(),
        publication_id,
        action_set_id,
        observation_actions,
        expected_publication_id,
    )
    report_content = render_review_run(preliminary_projection)
    report_hash = hashlib.sha256(report_content.encode("utf-8")).hexdigest()
    publication_action = BusinessAction.create(
        action_id=publication_action.action_id,
        action_type="publish_review",
        target_type="review_publication",
        target_id=base.trade_date,
        payload={
            "trade_date": base.trade_date,
            "run_id": base.run_id,
            "review_revision_id": final_revision.review_revision_id,
            "review_revision_hash": final_revision.content_hash,
            "report_hash": report_hash,
        },
    )
    actions = (publication_action, *pool_actions, *observation_actions, *result_actions)
    run_report_dir = Path(output_dir) / base.trade_date / base.run_id
    blob_path = run_report_dir / "blobs" / f"{report_hash}.md"
    revision_path = run_report_dir / "revisions" / f"{final_revision.review_revision_id}.md"
    action_set = ActionSet.create(
        action_set_id=action_set_id,
        run_id=base.run_id,
        trade_date=base.trade_date,
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.content_hash,
        review_revision_id=final_revision.review_revision_id,
        review_revision_hash=final_revision.content_hash,
        report_hash=report_hash,
        created_at=timestamp.isoformat(),
        expires_at=expires_at,
        actions=actions,
        version_expectations=expectations,
        file_effects=(str(revision_path), str(blob_path)),
        database_effects=FINALIZATION_DATABASE_EFFECTS,
    )
    proposal = FinalizationProposal.create(
        proposal_id=proposal_id,
        run_id=base.run_id,
        trade_date=base.trade_date,
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.content_hash,
        base_review_revision_id=base.review_revision_id,
        expected_current_review_revision_id=base.review_revision_id,
        final_revision=final_revision,
        action_set=action_set,
        created_at=timestamp.isoformat(),
        expires_at=expires_at,
    )
    # 预览也必须遵守 Manifest 的文件授权范围，不能把越权路径留到最终写入时才拒绝。
    proposal.validate_against_manifest(manifest)
    # ActionSet 中写入最终哈希后，报告投影内容仍不依赖该哈希，必须保持字节一致。
    final_content = render_review_run(
        _report_projection(
            manifest,
            final_revision,
            quality.to_dict(),
            publication_id,
            action_set_id,
            observation_actions,
            expected_publication_id,
        )
    )
    if hashlib.sha256(final_content.encode("utf-8")).hexdigest() != action_set.report_hash:
        raise ReviewRunDomainError("FinalizationProposal 内部报告渲染不稳定。")
    return proposal, final_content


# finalize 先准备不可变文件与联合对象，再以单个 SQLite 事务提交事件和全部业务动作。
def finalize_daily_review(
    proposal: FinalizationProposal,
    proposal_hash: str,
    repository: ReviewRunRepository,
    *,
    now: datetime | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    proposal.validate()
    if proposal.content_hash != proposal_hash:
        raise ReviewRunDomainError("finalize 提供的 proposal hash 与内容不一致。")
    manifest = load_run_manifest(repository, proposal.run_id)
    timestamp = require_manifest_active(manifest, now=now)
    require_repository_authorized(manifest, repository)
    proposal.validate_against_manifest(manifest)
    projection = load_run_projection(repository, proposal.run_id)
    _require_required_model_result(manifest, projection, proposal.final_revision)
    _validate_current_revision_evidence(
        proposal.final_revision,
        manifest,
        projection,
        repository,
    )
    current_publication = repository.get_current_review_publication(proposal.trade_date)
    if current_publication is not None and current_publication.get("action_set_id") == proposal.action_set.action_set_id:
        _validate_applied_replay_identity(repository, proposal, current_publication)
        publication_id = current_publication["publication_id"]
        _validate_finalize_recovery_projection(projection, proposal, publication_id)
        _quality, report_content = _render_final_report(manifest, proposal)
        revision_path, blob_path = (Path(item) for item in proposal.action_set.file_effects)
        # 事务已经成功时，重放仍要核验不可变文件，并确定性补回先前中断留下的缺失文件。
        _write_immutable_text(
            blob_path,
            report_content,
            failure_injector=failure_injector,
            stage_prefix="blob",
        )
        _write_immutable_text(
            revision_path,
            report_content,
            failure_injector=failure_injector,
            stage_prefix="revision",
        )
        _verify_report_files(proposal.action_set, revision_path, blob_path)
        if projection.state != "published":
            _append_finalize_events(
                repository,
                manifest,
                proposal,
                publication_id,
                _proposal_timestamp(proposal),
                failure_injector,
            )
        if load_run_projection(repository, proposal.run_id).state == "published":
            publication = _load_finalized_publication(
                repository,
                proposal,
                publication_id,
            )
            return {
                "action_set_id": proposal.action_set.action_set_id,
                "action_set_hash": proposal.action_set.content_hash,
                "publication_id": publication_id,
                "publication_hash": publication["content_hash"],
                "report_hash": publication["report_hash"],
                "report_path": str(blob_path),
                "replayed": True,
            }
    if projection.state != "draft_ready":
        raise ReviewRunDomainError("finalize 只能从 draft_ready 状态执行。")
    current_versions = _current_versions(proposal.action_set, repository)
    proposal.validate_current_state(
        current_review_revision_id=projection.current_review_revision_id,
        current_versions=current_versions,
        checked_at=timestamp.isoformat(),
    )
    quality, report_content = _render_final_report(manifest, proposal)
    publication_id = _proposal_publication_id(proposal)

    _inject(failure_injector, "before_blob_write")
    revision_path, blob_path = (Path(item) for item in proposal.action_set.file_effects)
    _write_immutable_text(
        blob_path,
        report_content,
        failure_injector=failure_injector,
        stage_prefix="blob",
    )
    _inject(failure_injector, "after_blob_write")
    _write_immutable_text(
        revision_path,
        report_content,
        failure_injector=failure_injector,
        stage_prefix="revision",
    )
    _verify_report_files(proposal.action_set, revision_path, blob_path)

    _inject(failure_injector, "before_records")
    repository.save_review_revision(
        proposal.final_revision.review_revision_id,
        proposal.run_id,
        proposal.final_revision.evidence_bundle_id,
        proposal.final_revision.to_dict(),
        supersedes_review_revision_id=proposal.base_review_revision_id,
    )
    repository.save_finalization_proposal(
        proposal.proposal_id,
        proposal.run_id,
        proposal.final_revision.review_revision_id,
        proposal.to_dict(),
    )
    storage_payload = _storage_action_set_payload(
        proposal,
        publication_id,
        blob_path,
    )
    repository.save_action_set(
        proposal.action_set.action_set_id,
        proposal.run_id,
        proposal.final_revision.review_revision_id,
        proposal.manifest_id,
        storage_payload,
        proposal_id=proposal.proposal_id,
    )
    _inject(failure_injector, "before_apply")
    _inject(failure_injector, "before_event")
    result = _append_finalize_events(
        repository,
        manifest,
        proposal,
        publication_id,
        _proposal_timestamp(proposal),
        failure_injector,
    )
    _inject(failure_injector, "after_apply")
    _inject(failure_injector, "after_event")
    publication = _load_finalized_publication(
        repository,
        proposal,
        publication_id,
    )
    return {
        **result,
        "action_set_hash": proposal.action_set.content_hash,
        "publication_hash": publication["content_hash"],
        "report_hash": publication["report_hash"],
        "report_path": str(blob_path),
        "review_revision_id": proposal.final_revision.review_revision_id,
        "quality_level": quality.level,
    }


# 提案和提交都重新读取当前 EvidenceBundle 正文；文件在两步之间变化时必须在写报告前阻断。
def _validate_current_revision_evidence(
    revision: RevisionBundle,
    manifest: Any,
    projection: Any,
    repository: ReviewRunRepository,
) -> None:
    if projection.evidence_bundle_id != revision.evidence_bundle_id:
        raise ReviewRunDomainError("当前 ReviewRun 未绑定最终修订引用的 EvidenceBundle。")
    record = repository.get_evidence_bundle(revision.evidence_bundle_id)
    if record is None:
        raise ReviewRunDomainError("最终修订引用的 EvidenceBundle 不存在。")
    evidence_bundle = EvidenceBundle.from_dict(record["payload"])
    evidence_bundle.validate_against_manifest(manifest)
    validate_revision_evidence(revision, evidence_bundle, repository)


# required 模式不仅要求模型事件完成，当前定稿链还必须实际继承对应响应哈希。
def _require_required_model_result(
    manifest: Any,
    projection: Any,
    revision: RevisionBundle,
) -> None:
    if manifest.model_use.mode != "required":
        return
    completed_hashes = {
        request.response_hash
        for request in projection.model_requests
        if request.status == "completed"
        and request.model_id in manifest.model_use.model_ids
        and request.response_hash
    }
    if not completed_hashes:
        raise ReviewRunDomainError("RunManifest 要求模型参与，但当前运行没有已完成的授权模型响应。")
    if not completed_hashes.intersection(revision.model_response_hashes):
        raise ReviewRunDomainError(
            "RunManifest 要求模型参与，但当前复盘修订没有继承已完成的模型响应。"
        )


# 正式提交和重放共享同一份确定性报告投影，避免成功重放绕过内容哈希检查。
def _render_final_report(
    manifest: Any,
    proposal: FinalizationProposal,
) -> tuple[QualityAssessment, str]:
    quality = validate_revision_bundle(proposal.final_revision)
    observation_actions = tuple(
        action
        for action in proposal.action_set.actions
        if action.action_type == "create_observation"
    )
    expected_publication_id = _expectation_for(
        proposal.action_set,
        "review_publication",
        proposal.trade_date,
    ).expected_version
    report_content = render_review_run(
        _report_projection(
            manifest,
            proposal.final_revision,
            quality.to_dict(),
            _proposal_publication_id(proposal),
            proposal.action_set.action_set_id,
            observation_actions,
            expected_publication_id,
        )
    )
    if (
        hashlib.sha256(report_content.encode("utf-8")).hexdigest()
        != proposal.action_set.report_hash
    ):
        raise ReviewRunDomainError("重新渲染的正式报告与 ActionSet 报告哈希不一致。")
    return quality, report_content


def _build_final_revision(
    base: RevisionBundle,
    selections: Mapping[str, Any],
    timestamp: datetime,
    selection_hash: str,
) -> RevisionBundle:
    decision = str(selections.get("participation_decision") or "")
    decision_note = str(selections.get("decision_note") or "").strip()
    if decision not in {"non_participation", "actionable"}:
        raise ReviewRunDomainError("最终选择必须明确为 non_participation 或 actionable。")
    if not decision_note:
        raise ReviewRunDomainError("最终选择必须填写 decision_note。")
    candidates = {item.plan_item_id: item for item in base.candidate_plan_items}
    approved_ids = tuple(str(item) for item in selections.get("approved_plan_item_ids", ()))
    if len(approved_ids) != len(set(approved_ids)):
        raise ReviewRunDomainError("批准计划项 ID 不能重复。")
    unknown = set(approved_ids) - set(candidates)
    if unknown:
        raise ReviewRunDomainError(f"批准了当前草案之外的计划项：{', '.join(sorted(unknown))}")
    if decision == "non_participation" and approved_ids:
        raise ReviewRunDomainError("明确不参与时不能批准计划项。")
    if decision == "actionable" and not approved_ids:
        raise ReviewRunDomainError("actionable 最终修订至少必须批准一个计划项。")
    rejection_values = selections.get("rejected_plan_items", {})
    if not isinstance(rejection_values, Mapping):
        raise ReviewRunDomainError("rejected_plan_items 必须是计划项 ID 到原因的 JSON 对象。")
    rejected_ids = set(candidates) - set(approved_ids)
    if set(rejection_values) != rejected_ids:
        raise ReviewRunDomainError("每个未批准候选都必须提供且只提供一个拒绝原因。")
    rejected = tuple(
        RejectedPlanItem(plan_item_id=item_id, reason=str(rejection_values[item_id]))
        for item_id in sorted(rejected_ids)
    )
    approved = tuple(candidates[item_id] for item_id in approved_ids)
    object_ids = tuple(item.object_id for item in approved)
    steps = tuple(
        _finalize_step(step, decision, object_ids, approved_ids)
        for step in base.steps
    )
    return RevisionBundle.create(
        review_revision_id=f"REV-FINAL-{selection_hash[:20]}",
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
        revision_kind="final",
        participation_decision=decision,
        replaces_review_revision_id=base.review_revision_id,
        steps=steps,
        approved_plan_items=approved,
        rejected_plan_items=rejected,
        decision_note=decision_note,
        model_response_hashes=base.model_response_hashes,
    )


def _finalize_step(
    step: StepAssessment,
    decision: str,
    object_ids: tuple[str, ...],
    plan_item_ids: tuple[str, ...],
) -> StepAssessment:
    if step.step_number not in {7, 8, 10}:
        return step
    if decision == "non_participation":
        return replace(
            step,
            status="not_applicable",
            conclusion="本次提案选择次日不参与，本步行动结构不适用。",
            object_ids=(),
            plan_item_ids=(),
        )
    return replace(step, object_ids=object_ids, plan_item_ids=plan_item_ids)


def _build_observation_actions(
    revision: RevisionBundle,
    timestamp: datetime,
    selection_hash: str,
) -> tuple[BusinessAction, ...]:
    actions: list[BusinessAction] = []
    for plan in revision.approved_plan_items:
        observation_id = (
            f"OBS-{revision.trade_date.replace('-', '')}-"
            f"{canonical_hash({'revision': revision.review_revision_id, 'plan': plan.plan_item_id})[:10]}"
        )
        payload = {
            "observation_id": observation_id,
            "review_revision_id": revision.review_revision_id,
            "review_revision_hash": revision.content_hash,
            "plan_item_id": plan.plan_item_id,
            "hypothesis": plan.hypothesis,
            "confirmation_condition": plan.confirmation_condition,
            "invalidation_condition": plan.invalidation_condition,
            "evidence_references": list(plan.evidence_references),
            "review_deadline": plan.review_deadline,
            "created_at": timestamp.isoformat(),
        }
        actions.append(
            BusinessAction.create(
                action_id=f"ACT-OBS-{selection_hash[:10]}-{len(actions) + 1}",
                action_type="create_observation",
                target_type="observation",
                target_id=observation_id,
                payload=payload,
            )
        )
    return tuple(actions)


def _build_pool_actions(
    selections: Mapping[str, Any],
    repository: ReviewRunRepository,
    selection_hash: str,
) -> tuple[BusinessAction, ...]:
    raw_actions = selections.get("pool_actions", ())
    if not isinstance(raw_actions, (list, tuple)):
        raise ReviewRunDomainError("pool_actions 必须是 JSON 数组。")
    actions: list[BusinessAction] = []
    for position, value in enumerate(raw_actions, start=1):
        if not isinstance(value, Mapping):
            raise ReviewRunDomainError("pool_actions 中每一项必须是 JSON 对象。")
        pool_type = str(value.get("pool_type") or "").strip()
        code = str(value.get("code") or "").strip()
        status = str(value.get("status") or "").strip()
        if not pool_type or not code or not status:
            raise ReviewRunDomainError("池动作必须包含 pool_type、code 和 status。")
        actions.append(
            BusinessAction.create(
                action_id=f"ACT-POOL-{selection_hash[:10]}-{position}",
                action_type="update_pool_status",
                target_type="pool_item",
                target_id=f"{pool_type}:{code}",
                payload={**dict(value), "pool_type": pool_type, "code": code, "status": status},
            )
        )
    return tuple(actions)


def _build_result_actions(
    selections: Mapping[str, Any],
    repository: ReviewRunRepository,
    selection_hash: str,
) -> tuple[BusinessAction, ...]:
    raw_actions = selections.get("observation_result_actions", ())
    if not isinstance(raw_actions, (list, tuple)):
        raise ReviewRunDomainError("observation_result_actions 必须是 JSON 数组。")
    actions: list[BusinessAction] = []
    for position, value in enumerate(raw_actions, start=1):
        if not isinstance(value, Mapping):
            raise ReviewRunDomainError("Observation 结果动作必须是 JSON 对象。")
        observation_id = str(value.get("observation_id") or "").strip()
        if not observation_id or repository.get_observation(observation_id) is None:
            raise ReviewRunDomainError(f"待回填 Observation 不存在：{observation_id or '空'}")
        references_value = value.get("evidence_references")
        if not isinstance(references_value, (list, tuple)):
            raise ReviewRunDomainError("Observation 结果动作的 evidence_references 必须是数组。")
        references = tuple(str(item) for item in references_value)
        validate_observation_result(
            str(value.get("status") or ""),
            str(value.get("actual_result") or ""),
            str(value.get("reason") or ""),
            references,
        )
        actions.append(
            BusinessAction.create(
                action_id=f"ACT-RESULT-{selection_hash[:10]}-{position}",
                action_type="record_observation_result",
                target_type="observation_result",
                target_id=observation_id,
                payload={**dict(value), "evidence_references": list(references)},
            )
        )
    return tuple(actions)


def _build_expectations(
    actions: tuple[BusinessAction, ...],
    repository: ReviewRunRepository,
    *,
    expected_publication_id: str | None,
) -> tuple[VersionExpectation, ...]:
    expectations: list[VersionExpectation] = []
    for action in actions:
        if action.target_type == "review_publication":
            expected = expected_publication_id
        elif action.target_type == "pool_item":
            pool_type, code = action.target_id.split(":", 1)
            current = repository.get_current_pool_status(pool_type, code)
            expected = current["event_id"] if current is not None else None
        elif action.target_type == "observation":
            existing = repository.get_observation(action.target_id)
            if existing is not None:
                raise ReviewRunDomainError(f"Observation ID 已存在：{action.target_id}")
            expected = None
        elif action.target_type == "observation_result":
            current = repository.get_current_observation_result(action.target_id)
            expected = current["result_revision_id"] if current is not None else None
        else:
            raise ReviewRunDomainError(f"未知 ActionSet 目标类型：{action.target_type}")
        expectations.append(
            VersionExpectation(
                target_type=action.target_type,
                target_id=action.target_id,
                expected_version=expected,
            )
        )
    return tuple(expectations)


def _current_versions(
    action_set: ActionSet,
    repository: ReviewRunRepository,
) -> dict[tuple[str, str], str | None]:
    versions: dict[tuple[str, str], str | None] = {}
    for expectation in action_set.version_expectations:
        key = expectation.target_key
        if expectation.target_type == "review_publication":
            current = repository.get_current_review_publication(expectation.target_id)
            versions[key] = current["publication_id"] if current is not None else None
        elif expectation.target_type == "pool_item":
            pool_type, code = expectation.target_id.split(":", 1)
            current = repository.get_current_pool_status(pool_type, code)
            versions[key] = current["event_id"] if current is not None else None
        elif expectation.target_type == "observation":
            current = repository.get_observation(expectation.target_id)
            versions[key] = current["content_hash"] if current is not None else None
        elif expectation.target_type == "observation_result":
            current = repository.get_current_observation_result(expectation.target_id)
            versions[key] = current["result_revision_id"] if current is not None else None
    return versions


def _storage_action_set_payload(
    proposal: FinalizationProposal,
    publication_id: str,
    blob_path: Path,
) -> dict[str, Any]:
    expectations = {item.target_key: item.expected_version for item in proposal.action_set.version_expectations}
    publication_action = _action_by_type(proposal.action_set, "publish_review")
    pool_actions = []
    observation_creates = []
    result_actions = []
    for action in proposal.action_set.actions:
        if action.action_type == "publish_review":
            continue
        if action.action_type == "update_pool_status":
            pool_type, code = action.target_id.split(":", 1)
            pool_actions.append(
                {
                    "event_id": f"POOL-EVENT-{action.action_id}",
                    "pool_type": pool_type,
                    "code": code,
                    "status": str(action.payload["status"]),
                    "payload": dict(action.payload),
                    "expected_event_id": expectations[action.target_key],
                }
            )
        elif action.action_type == "create_observation":
            observation_creates.append(
                {
                    "observation_id": action.target_id,
                    "review_revision_id": proposal.final_revision.review_revision_id,
                    "plan_item_id": str(action.payload["plan_item_id"]),
                    "payload": dict(action.payload),
                }
            )
        elif action.action_type == "record_observation_result":
            status = str(action.payload.get("status") or "")
            result_actions.append(
                {
                    "observation_id": action.target_id,
                    "result_revision_id": f"RESULT-{action.action_id}",
                    "status": status,
                    "payload": dict(action.payload),
                    "expected_result_revision_id": expectations[action.target_key],
                }
            )
        else:
            raise ReviewRunDomainError(f"未知业务动作类型：{action.action_type}")
    return {
        "domain_action_set": proposal.action_set.to_dict(),
        "publication": {
            "publication_id": publication_id,
            "trade_date": proposal.trade_date,
            "run_id": proposal.run_id,
            "review_revision_id": proposal.final_revision.review_revision_id,
            "report_hash": proposal.action_set.report_hash,
            "payload": {
                "report_path": str(blob_path),
                "review_revision_hash": proposal.final_revision.content_hash,
                "proposal_id": proposal.proposal_id,
                "proposal_hash": proposal.content_hash,
            },
            "expected_publication_id": expectations[publication_action.target_key],
        },
        "pool_actions": pool_actions,
        "observation_creates": observation_creates,
        "observation_result_actions": result_actions,
        "audit": {
            "audit_event_id": f"AUDIT-{proposal.action_set.action_set_id}",
            "event_type": "daily finalize",
            "payload": {
                "command": "daily finalize",
                "run_id": proposal.run_id,
                "trade_date": proposal.trade_date,
                "manifest_id": proposal.manifest_id,
                "manifest_hash": proposal.manifest_hash,
                "action_set_id": proposal.action_set.action_set_id,
                "action_set_hash": proposal.action_set.content_hash,
                "proposal_id": proposal.proposal_id,
                "proposal_hash": proposal.content_hash,
                "output": str(blob_path),
                "status": "completed",
            },
            "outbox_id": f"OUTBOX-{proposal.action_set.action_set_id}",
            "outbox_payload": {
                "command": "daily finalize",
                "run_id": proposal.run_id,
                "trade_date": proposal.trade_date,
                "output": str(blob_path),
                "status": "completed",
            },
        },
    }


def _report_projection(
    manifest: Any,
    revision: RevisionBundle,
    quality: Mapping[str, Any],
    publication_id: str,
    action_set_id: str,
    observation_actions: tuple[BusinessAction, ...],
    supersedes_publication_id: str | None,
) -> dict[str, Any]:
    plans = revision.approved_plan_items
    return {
        "run_id": revision.run_id,
        "trade_date": revision.trade_date,
        "publication_id": publication_id,
        "review_revision": revision.to_dict(),
        "decision": {
            # 提案预览和正式 blob 字节相同；这里只声明内容已定稿，权威发布状态由数据库指针决定。
            "publication_status": "finalized（是否已提交及是否为当前版本，以 publication 投影为准）",
            "participation_decision": revision.participation_decision,
            "total_risk": str(sum((item.max_risk for item in plans), Decimal("0"))),
            "summary": revision.decision_note,
            "focus_targets": [f"{item.object_code} {item.object_name}".strip() for item in plans],
            "risk_boundary": [item.invalidation_condition for item in plans],
        },
        "quality": {
            **dict(quality),
            "disposition": (
                "允许正式发布" if quality.get("publishable") else "保持草案并修复阻断项"
            ),
            "blocking_items": [
                issue
                for gate in quality.get("gates", [])
                if gate.get("status") == "failed"
                for issue in gate.get("issues", [])
            ],
            "notes": [],
        },
        "provenance": {
            "publication_id": publication_id,
            # ActionSet 与 publication 的最终哈希都包裹报告哈希，不能再自嵌入被哈希的正文。
            "publication_hash_locator": (
                f"review_publications[{publication_id}].content_hash"
                "（daily finalize 回执与数据库正式记录核验）"
            ),
            "report_hash_locator": (
                f"review_publications[{publication_id}].report_hash"
                "（daily finalize 回执、blob 文件名与文件 SHA-256 核验）"
            ),
            "supersedes_publication_id": supersedes_publication_id,
            "manifest_id": manifest.manifest_id,
            "manifest_hash": manifest.content_hash,
            "evidence_bundle_id": revision.evidence_bundle_id,
            "evidence_bundle_hash": revision.evidence_bundle_hash,
            "evidence_cutoff_at": revision.evidence_cutoff_at,
            "review_revision_id": revision.review_revision_id,
            "review_revision_hash": revision.content_hash,
            "framework_version": revision.framework_version,
            "framework_hash": revision.framework_hash,
            "program_version": revision.program_version,
            "renderer_version": revision.renderer_version,
            "model_response_hash": list(revision.model_response_hashes),
            "action_set_id": action_set_id,
            "action_set_hash_locator": (
                f"action_sets[{action_set_id}].content_hash"
                "（FinalizationProposal、daily finalize 回执与数据库记录核验）"
            ),
        },
        "observations": [
            {
                **dict(action.payload),
                "status": "pending",
                "actual_result": "尚未回填",
                "reason": "尚未回填",
            }
            for action in observation_actions
        ],
        "appendix": {
            "candidate_plan_items": [],
            "rejected_plan_items": [item.to_dict() for item in revision.rejected_plan_items],
            "evidence_references": sorted(
                {reference for step in revision.steps for reference in step.evidence_references}
            ),
        },
    }


# finalize 回执只接受本次提案实际创建的 publication，防止返回错日或错动作集的哈希。
def _load_finalized_publication(
    repository: ReviewRunRepository,
    proposal: FinalizationProposal,
    publication_id: str,
) -> Mapping[str, Any]:
    publication = repository.get_current_review_publication(proposal.trade_date)
    if (
        publication is None
        or publication.get("publication_id") != publication_id
        or publication.get("action_set_id") != proposal.action_set.action_set_id
        or publication.get("report_hash") != proposal.action_set.report_hash
    ):
        raise ReviewRunDomainError("finalize 完成后未找到与当前提案一致的正式 publication。")
    return publication


# 已应用重放必须同时绑定提案、ActionSet、修订、报告和 publication，不能只复用一个动作集 ID。
def _validate_applied_replay_identity(
    repository: ReviewRunRepository,
    proposal: FinalizationProposal,
    publication: Mapping[str, Any],
) -> None:
    stored_action_set = repository.get_action_set(proposal.action_set.action_set_id)
    if (
        stored_action_set is None
        or stored_action_set.get("run_id") != proposal.run_id
        or stored_action_set.get("review_revision_id")
        != proposal.final_revision.review_revision_id
        or stored_action_set.get("manifest_id") != proposal.manifest_id
        or stored_action_set.get("proposal_id") != proposal.proposal_id
        or stored_action_set.get("payload", {}).get("domain_action_set")
        != proposal.action_set.to_dict()
    ):
        raise ReviewRunDomainError("已存 ActionSet 与当前提案的完整身份不一致。")

    stored_proposal = repository.get_finalization_proposal(proposal.proposal_id)
    if (
        stored_proposal is None
        or stored_proposal.get("run_id") != proposal.run_id
        or stored_proposal.get("review_revision_id")
        != proposal.final_revision.review_revision_id
        or stored_proposal.get("payload") != proposal.to_dict()
    ):
        raise ReviewRunDomainError("已存 FinalizationProposal 与当前提案不一致。")

    expected_publication = {
        "publication_id": _proposal_publication_id(proposal),
        "trade_date": proposal.trade_date,
        "run_id": proposal.run_id,
        "review_revision_id": proposal.final_revision.review_revision_id,
        "action_set_id": proposal.action_set.action_set_id,
        "report_hash": proposal.action_set.report_hash,
    }
    if any(publication.get(key) != value for key, value in expected_publication.items()):
        raise ReviewRunDomainError("已存 publication 与当前提案的完整身份不一致。")
    expected_payload = {
        "report_path": _blob_path(proposal.action_set),
        "review_revision_hash": proposal.final_revision.content_hash,
        "proposal_id": proposal.proposal_id,
        "proposal_hash": proposal.content_hash,
    }
    if publication.get("payload") != expected_payload:
        raise ReviewRunDomainError("已存 publication payload 与当前提案不一致。")


# 业务事务完成后只允许从原 base 或已追加的 final revision 恢复；其他 head 必须先验拒绝。
def _validate_finalize_recovery_projection(
    projection: Any,
    proposal: FinalizationProposal,
    publication_id: str,
) -> None:
    if projection.state == "published":
        if (
            projection.current_review_revision_id
            != proposal.final_revision.review_revision_id
            or projection.publication_id != publication_id
        ):
            raise ReviewRunDomainError("finalize 重放的已发布事件投影与当前提案不一致。")
        return
    if (
        projection.state != "draft_ready"
        or projection.current_review_revision_id
        not in {
            proposal.base_review_revision_id,
            proposal.final_revision.review_revision_id,
        }
    ):
        raise ReviewRunDomainError("finalize 恢复时当前复盘修订已经变化，拒绝追加旧事件。")


def _proposal_publication_id(proposal: FinalizationProposal) -> str:
    return (
        f"PUB-{proposal.trade_date.replace('-', '')}-"
        f"{proposal.proposal_id.removeprefix('PROPOSAL-')[:12]}"
    )


def _write_immutable_text(
    path: Path,
    content: str,
    *,
    failure_injector: Callable[[str], None] | None = None,
    stage_prefix: str = "report",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content_bytes = content.encode("utf-8")
    if path.exists():
        if path.read_bytes() != content_bytes:
            raise ReviewRunDomainError(f"不可变报告文件已存在且内容不同：{path}")
        return
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content_bytes)
        _inject(failure_injector, f"after_{stage_prefix}_temporary_write")
        try:
            # 硬链接创建只在目标不存在时成功，避免并发写者覆盖先落盘的不可变正文。
            _inject(failure_injector, f"before_{stage_prefix}_immutable_publish")
            os.link(temporary_path, path)
        except FileExistsError:
            if path.read_bytes() != content_bytes:
                raise ReviewRunDomainError(f"不可变报告文件已存在且内容不同：{path}")
        except OSError as error:
            raise ReviewRunDomainError(f"不可变报告文件写入失败：{path}：{error}") from error
        _inject(failure_injector, f"after_{stage_prefix}_immutable_publish")
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


# 两个正式文件都必须与 ActionSet 的内容寻址哈希一致。
def _verify_report_files(
    action_set: ActionSet,
    revision_path: Path,
    blob_path: Path,
) -> None:
    for path in (revision_path, blob_path):
        if hashlib.sha256(path.read_bytes()).hexdigest() != action_set.report_hash:
            raise ReviewRunDomainError(f"正式报告文件落盘后哈希校验失败：{path}")


def _action_by_type(action_set: ActionSet, action_type: str) -> BusinessAction:
    matches = [action for action in action_set.actions if action.action_type == action_type]
    if len(matches) != 1:
        raise ReviewRunDomainError(f"ActionSet 必须且只能包含一个 {action_type} 动作。")
    return matches[0]


def _expectation_for(
    action_set: ActionSet,
    target_type: str,
    target_id: str,
) -> VersionExpectation:
    for item in action_set.version_expectations:
        if item.target_key == (target_type, target_id):
            return item
    raise ReviewRunDomainError(f"ActionSet 缺少 {target_type}/{target_id} 的版本期望。")


# 两个发布事件逐步预演后，与 ActionSet 在同一仓储事务内提交。
def _append_finalize_events(
    repository: ReviewRunRepository,
    manifest: Any,
    proposal: FinalizationProposal,
    publication_id: str,
    timestamp: datetime,
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    revision_event = ReviewRunEvent.create(
        event_id=f"EVENT-final-revision-{proposal.action_set.action_set_id}",
        run_id=proposal.run_id,
        event_type="revision_added",
        occurred_at=timestamp.isoformat(),
        operation_id=f"FINAL-REV-{proposal.action_set.action_set_id}",
        review_revision_id=proposal.final_revision.review_revision_id,
        expected_current_revision_id=proposal.base_review_revision_id,
    )
    initial_records = repository.list_review_run_events(proposal.run_id)
    initial_events = tuple(
        ReviewRunEvent.from_dict(record["payload"])
        for record in initial_records
    )
    project_review_run(manifest, (*initial_events, revision_event))
    publication_event = ReviewRunEvent.create(
        event_id=f"EVENT-publication-{proposal.action_set.action_set_id}",
        run_id=proposal.run_id,
        event_type="publication_completed",
        occurred_at=timestamp.isoformat(),
        operation_id=proposal.action_set.action_set_id,
        review_revision_id=proposal.final_revision.review_revision_id,
        expected_current_revision_id=proposal.final_revision.review_revision_id,
        publication_id=publication_id,
    )
    project_review_run(
        manifest,
        (*initial_events, revision_event, publication_event),
    )
    records = initial_records
    records_by_id = {record["event_id"]: record for record in records}
    planned_head = records[-1]["event_id"] if records else None
    event_appends = []
    for event in (revision_event, publication_event):
        existing = records_by_id.get(event.event_id)
        expected_previous_event_id = (
            existing["previous_event_id"]
            if existing is not None
            else planned_head
        )
        event_appends.append(
            (
                event.event_id,
                event.event_type,
                event.to_dict(),
                expected_previous_event_id,
            )
        )
        if existing is None:
            planned_head = event.event_id
    # 此 hook 位于语义预演与原子事务之间，用于确定性验证事件头 CAS。
    _inject(failure_injector, "after_final_revision_event")
    return repository.apply_action_set_with_review_run_events(
        proposal.action_set.action_set_id,
        proposal.run_id,
        tuple(event_appends),
        failure_injector=failure_injector,
    )


def _proposal_timestamp(proposal: FinalizationProposal) -> datetime:
    try:
        return _timestamp(datetime.fromisoformat(proposal.created_at))
    except ValueError as error:
        raise ReviewRunDomainError("FinalizationProposal 创建时间无效。") from error


def _blob_path(action_set: ActionSet) -> str:
    if len(action_set.file_effects) != 2:
        raise ReviewRunDomainError("ActionSet 必须声明 revision 与 blob 两个报告路径。")
    return action_set.file_effects[1]


def _inject(hook: Callable[[str], None] | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _timestamp(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ReviewRunDomainError("业务时间必须包含时区。")
    return timestamp.astimezone(timezone.utc).replace(microsecond=0)
