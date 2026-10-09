# 本文件定义最终修订与全部业务动作的联合确认对象，并校验乐观版本期望。

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from stock_review.review_runs.canonical_hash import (
    canonical_hash,
    canonicalize,
    require_sha256,
)
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.review_revision import (
    PlanItem,
    RevisionBundle,
    validate_revision_bundle,
)
from stock_review.review_runs.run_manifest import RunManifest


ACTION_TARGET_TYPES = {
    "publish_review": "review_publication",
    "update_pool_status": "pool_item",
    "create_observation": "observation",
    "record_observation_result": "observation_result",
}

FINALIZATION_DATABASE_EFFECTS = (
    "review_revisions",
    "finalization_proposals",
    "action_sets",
    "review_publications",
    "current_review_publications",
    "pool_status_events",
    "current_pool_status",
    "observations",
    "observation_result_revisions",
    "current_observation_results",
    "action_set_applications",
    "review_run_events",
    "audit_events",
    "audit_outbox",
)

POOL_TYPES = frozenset({"watch", "hot"})
POOL_STATUSES = frozenset({"active", "paused", "removed"})
OBSERVATION_RESULT_STATUSES = frozenset({"hit", "miss", "invalid"})
OBSERVATION_RESULT_REASONS = frozenset(
    {
        "judgment_error",
        "insufficient_evidence",
        "observation_or_execution_deviation",
        "invalid_sample",
    }
)


@dataclass(frozen=True)
class VersionExpectation:
    target_type: str
    target_id: str
    expected_version: str | None

    def __post_init__(self) -> None:
        _require_text(self.target_type, "版本目标类型")
        _require_text(self.target_id, "版本目标 ID")
        if self.expected_version is not None:
            _require_text(self.expected_version, "期望旧版本")

    @property
    def target_key(self) -> tuple[str, str]:
        return (self.target_type, self.target_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "expected_version": self.expected_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VersionExpectation:
        expected_version = value.get("expected_version")
        return cls(
            target_type=str(value.get("target_type", "")),
            target_id=str(value.get("target_id", "")),
            expected_version=(
                None if expected_version is None else str(expected_version)
            ),
        )


@dataclass(frozen=True)
class BusinessAction:
    action_id: str
    action_type: str
    target_type: str
    target_id: str
    payload: Mapping[str, Any]
    content_hash: str

    def __post_init__(self) -> None:
        _require_text(self.action_id, "业务动作 ID")
        _require_text(self.action_type, "业务动作类型")
        _require_text(self.target_type, "业务动作目标类型")
        _require_text(self.target_id, "业务动作目标 ID")
        # 动作集合是正式提交协议；未知类型或错误目标必须在生成哈希前拒绝，不能由落库翻译静默忽略。
        expected_target_type = ACTION_TARGET_TYPES.get(self.action_type)
        if expected_target_type is None:
            raise ReviewRunDomainError(f"未知业务动作类型：{self.action_type}")
        if self.target_type != expected_target_type:
            raise ReviewRunDomainError(
                f"业务动作 {self.action_type} 必须使用 {expected_target_type} 目标。"
            )
        normalized_payload = canonicalize(self.payload)
        if not isinstance(normalized_payload, dict):
            raise ReviewRunDomainError("业务动作 payload 必须是 JSON 对象。")
        # 保存规范化副本，避免调用方后续修改原始 Mapping 影响动作哈希。
        object.__setattr__(self, "payload", normalized_payload)
        require_sha256(self.content_hash, "业务动作哈希", allow_empty=True)

    @classmethod
    def create(
        cls,
        *,
        action_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        payload: Mapping[str, Any],
    ) -> BusinessAction:
        action = cls(
            action_id=action_id,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
            content_hash="",
        )
        return replace(action, content_hash=action.calculate_hash())

    @property
    def target_key(self) -> tuple[str, str]:
        return (self.target_type, self.target_id)

    def calculate_hash(self) -> str:
        return canonical_hash(self._hash_payload())

    def validate_hash(self) -> None:
        require_sha256(self.content_hash, "业务动作哈希")
        if self.content_hash != self.calculate_hash():
            raise ReviewRunDomainError(f"业务动作 {self.action_id} 内容与哈希不一致。")

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BusinessAction:
        action = cls(
            action_id=str(value.get("action_id", "")),
            action_type=str(value.get("action_type", "")),
            target_type=str(value.get("target_type", "")),
            target_id=str(value.get("target_id", "")),
            payload=_require_mapping(value.get("payload", {}), "业务动作 payload"),
            content_hash=str(value.get("content_hash", "")),
        )
        action.validate_hash()
        return action

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "payload": canonicalize(self.payload),
        }


@dataclass(frozen=True)
class ActionSet:
    action_set_id: str
    run_id: str
    trade_date: str
    manifest_id: str
    manifest_hash: str
    review_revision_id: str
    review_revision_hash: str
    report_hash: str
    created_at: str
    expires_at: str
    actions: tuple[BusinessAction, ...]
    version_expectations: tuple[VersionExpectation, ...]
    file_effects: tuple[str, ...]
    database_effects: tuple[str, ...]
    content_hash: str

    def __post_init__(self) -> None:
        for label, value in (
            ("ActionSet ID", self.action_set_id),
            ("ReviewRun ID", self.run_id),
            ("交易日期", self.trade_date),
            ("RunManifest ID", self.manifest_id),
            ("复盘修订 ID", self.review_revision_id),
        ):
            _require_text(value, label)
        require_sha256(self.manifest_hash, "RunManifest 哈希")
        require_sha256(self.review_revision_hash, "复盘修订哈希")
        require_sha256(self.report_hash, "正式报告哈希")
        created_at = _require_datetime(self.created_at, "ActionSet 创建时间")
        expires_at = _require_datetime(self.expires_at, "ActionSet 过期时间")
        if expires_at <= created_at:
            raise ReviewRunDomainError("ActionSet 过期时间必须晚于创建时间。")
        if not self.actions:
            raise ReviewRunDomainError("ActionSet 至少必须包含正式发布动作。")
        _require_unique_values((action.action_id for action in self.actions), "业务动作 ID")
        _require_unique_values((action.target_key for action in self.actions), "业务动作目标")
        _require_unique_values(
            (expectation.target_key for expectation in self.version_expectations),
            "版本期望目标",
        )
        _require_unique_texts(self.file_effects, "文件影响")
        _require_unique_texts(self.database_effects, "数据库影响")
        require_sha256(self.content_hash, "ActionSet 哈希", allow_empty=True)

    @classmethod
    def create(
        cls,
        *,
        action_set_id: str,
        run_id: str,
        trade_date: str,
        manifest_id: str,
        manifest_hash: str,
        review_revision_id: str,
        review_revision_hash: str,
        report_hash: str,
        created_at: str,
        expires_at: str,
        actions: tuple[BusinessAction, ...],
        version_expectations: tuple[VersionExpectation, ...],
        file_effects: tuple[str, ...] = (),
        database_effects: tuple[str, ...] = (),
    ) -> ActionSet:
        action_set = cls(
            action_set_id=action_set_id,
            run_id=run_id,
            trade_date=trade_date,
            manifest_id=manifest_id,
            manifest_hash=manifest_hash,
            review_revision_id=review_revision_id,
            review_revision_hash=review_revision_hash,
            report_hash=report_hash,
            created_at=created_at,
            expires_at=expires_at,
            actions=actions,
            version_expectations=version_expectations,
            file_effects=file_effects,
            database_effects=database_effects,
            content_hash="",
        )
        action_set._validate_target_expectations()
        for action in action_set.actions:
            action.validate_hash()
        return replace(action_set, content_hash=action_set.calculate_hash())

    def calculate_hash(self) -> str:
        return canonical_hash(self._hash_payload())

    def validate_hash(self) -> None:
        require_sha256(self.content_hash, "ActionSet 哈希")
        for action in self.actions:
            action.validate_hash()
        if self.content_hash != self.calculate_hash():
            raise ReviewRunDomainError("ActionSet 内容与哈希不一致。")
        self._validate_target_expectations()

    def validate_against_manifest(self, manifest: RunManifest) -> None:
        self.validate_hash()
        manifest.validate_hash()
        if self.run_id != manifest.run_id or self.trade_date != manifest.trade_date:
            raise ReviewRunDomainError("ActionSet 与 RunManifest 的运行或交易日期不一致。")
        if self.manifest_id != manifest.manifest_id or self.manifest_hash != manifest.content_hash:
            raise ReviewRunDomainError("ActionSet 未绑定当前 RunManifest。")
        # Manifest 文件影响项定义可写根目录；规范化路径后再判断，阻止 `..` 和符号链接逃逸。
        allowed_roots = tuple(Path(item).resolve() for item in manifest.file_effects)
        for file_effect in self.file_effects:
            resolved_effect = Path(file_effect).resolve()
            if not any(resolved_effect.is_relative_to(root) for root in allowed_roots):
                raise ReviewRunDomainError(
                    f"ActionSet 文件影响不在 RunManifest 授权根内：{file_effect}"
                )
        # ActionSet 只声明表名；RunManifest 以“数据库路径:表名”绑定实际目标库。
        authorized_tables = {
            effect.rsplit(":", 1)[1] if ":" in effect else effect
            for effect in manifest.database_effects
        }
        unauthorized_tables = sorted(set(self.database_effects) - authorized_tables)
        if unauthorized_tables:
            raise ReviewRunDomainError(
                "ActionSet 数据库影响超出 RunManifest 授权："
                + ", ".join(unauthorized_tables)
            )

    # 提交前逐项比较用户预览时的版本；缺少当前版本也视为并发冲突。
    def validate_expected_versions(
        self,
        current_versions: Mapping[tuple[str, str], str | None],
    ) -> None:
        for expectation in self.version_expectations:
            if expectation.target_key not in current_versions:
                raise ReviewRunDomainError(
                    f"缺少目标 {expectation.target_type}/{expectation.target_id} 的当前版本。"
                )
            if current_versions[expectation.target_key] != expectation.expected_version:
                raise ReviewRunDomainError(
                    f"目标 {expectation.target_type}/{expectation.target_id} 的期望版本已经过期。"
                )

    def is_expired_at(self, timestamp: str) -> bool:
        return _require_datetime(timestamp, "校验时间") >= _require_datetime(
            self.expires_at,
            "ActionSet 过期时间",
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ActionSet:
        action_set = cls(
            action_set_id=str(value.get("action_set_id", "")),
            run_id=str(value.get("run_id", "")),
            trade_date=str(value.get("trade_date", "")),
            manifest_id=str(value.get("manifest_id", "")),
            manifest_hash=str(value.get("manifest_hash", "")),
            review_revision_id=str(value.get("review_revision_id", "")),
            review_revision_hash=str(value.get("review_revision_hash", "")),
            report_hash=str(value.get("report_hash", "")),
            created_at=str(value.get("created_at", "")),
            expires_at=str(value.get("expires_at", "")),
            actions=tuple(
                BusinessAction.from_dict(_require_mapping(item, "业务动作"))
                for item in value.get("actions", ())
            ),
            version_expectations=tuple(
                VersionExpectation.from_dict(_require_mapping(item, "版本期望"))
                for item in value.get("version_expectations", ())
            ),
            file_effects=_text_tuple(value, "file_effects"),
            database_effects=_text_tuple(value, "database_effects"),
            content_hash=str(value.get("content_hash", "")),
        )
        action_set.validate_hash()
        return action_set

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "action_set_id": self.action_set_id,
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "manifest_id": self.manifest_id,
            "manifest_hash": self.manifest_hash,
            "review_revision_id": self.review_revision_id,
            "review_revision_hash": self.review_revision_hash,
            "report_hash": self.report_hash,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "actions": [action.to_dict() for action in self.actions],
            "version_expectations": [
                expectation.to_dict() for expectation in self.version_expectations
            ],
            "file_effects": list(self.file_effects),
            "database_effects": list(self.database_effects),
        }

    def _validate_target_expectations(self) -> None:
        action_targets = {action.target_key for action in self.actions}
        expectation_targets = {
            expectation.target_key for expectation in self.version_expectations
        }
        if action_targets != expectation_targets:
            missing = sorted(action_targets - expectation_targets)
            extra = sorted(expectation_targets - action_targets)
            details = []
            if missing:
                details.append(f"缺少版本期望 {missing}")
            if extra:
                details.append(f"包含无动作目标 {extra}")
            raise ReviewRunDomainError("ActionSet 的版本期望必须逐项覆盖业务动作：" + "；".join(details))


@dataclass(frozen=True)
class FinalizationProposal:
    proposal_id: str
    run_id: str
    trade_date: str
    manifest_id: str
    manifest_hash: str
    base_review_revision_id: str
    expected_current_review_revision_id: str
    final_revision: RevisionBundle
    action_set: ActionSet
    created_at: str
    expires_at: str
    content_hash: str

    def __post_init__(self) -> None:
        for label, value in (
            ("FinalizationProposal ID", self.proposal_id),
            ("ReviewRun ID", self.run_id),
            ("交易日期", self.trade_date),
            ("RunManifest ID", self.manifest_id),
            ("基础复盘修订 ID", self.base_review_revision_id),
            ("期望当前复盘修订 ID", self.expected_current_review_revision_id),
        ):
            _require_text(value, label)
        require_sha256(self.manifest_hash, "RunManifest 哈希")
        created_at = _require_datetime(self.created_at, "FinalizationProposal 创建时间")
        expires_at = _require_datetime(self.expires_at, "FinalizationProposal 过期时间")
        if expires_at <= created_at:
            raise ReviewRunDomainError("FinalizationProposal 过期时间必须晚于创建时间。")
        require_sha256(self.content_hash, "FinalizationProposal 哈希", allow_empty=True)

    @classmethod
    def create(
        cls,
        *,
        proposal_id: str,
        run_id: str,
        trade_date: str,
        manifest_id: str,
        manifest_hash: str,
        base_review_revision_id: str,
        expected_current_review_revision_id: str,
        final_revision: RevisionBundle,
        action_set: ActionSet,
        created_at: str,
        expires_at: str,
    ) -> FinalizationProposal:
        proposal = cls(
            proposal_id=proposal_id,
            run_id=run_id,
            trade_date=trade_date,
            manifest_id=manifest_id,
            manifest_hash=manifest_hash,
            base_review_revision_id=base_review_revision_id,
            expected_current_review_revision_id=expected_current_review_revision_id,
            final_revision=final_revision,
            action_set=action_set,
            created_at=created_at,
            expires_at=expires_at,
            content_hash="",
        )
        finalized = replace(proposal, content_hash=proposal.calculate_hash())
        finalized.validate()
        return finalized

    def calculate_hash(self) -> str:
        return canonical_hash(self._hash_payload())

    # 联合校验确保正式报告、RevisionBundle 与数据库动作不能各自换版。
    def validate(self) -> None:
        require_sha256(self.content_hash, "FinalizationProposal 哈希")
        if self.content_hash != self.calculate_hash():
            raise ReviewRunDomainError("FinalizationProposal 内容与组合哈希不一致。")
        validate_revision_bundle(self.final_revision)
        self.action_set.validate_hash()
        self._validate_bindings()
        self._validate_effect_declarations()
        self._validate_publication_action()
        self._validate_pool_actions()
        self._validate_observation_actions()
        self._validate_observation_result_actions()

    def validate_against_manifest(self, manifest: RunManifest) -> None:
        self.validate()
        self.action_set.validate_against_manifest(manifest)
        if self.manifest_id != manifest.manifest_id or self.manifest_hash != manifest.content_hash:
            raise ReviewRunDomainError("FinalizationProposal 未绑定当前 RunManifest。")
        if len(manifest.file_effects) != 2:
            raise ReviewRunDomainError("RunManifest 必须依次授权 revisions 与 blobs 两个报告目录。")
        expected_paths = (
            Path(manifest.file_effects[0]).resolve()
            / f"{self.final_revision.review_revision_id}.md",
            Path(manifest.file_effects[1]).resolve()
            / f"{self.action_set.report_hash}.md",
        )
        actual_paths = tuple(Path(item).resolve() for item in self.action_set.file_effects)
        if actual_paths != expected_paths:
            raise ReviewRunDomainError(
                "ActionSet 文件影响必须精确指向当前 revision 与内容寻址 blob。"
            )

    def validate_current_state(
        self,
        *,
        current_review_revision_id: str,
        current_versions: Mapping[tuple[str, str], str | None],
        checked_at: str,
    ) -> None:
        self.validate()
        if self.expected_current_review_revision_id != current_review_revision_id:
            raise ReviewRunDomainError("FinalizationProposal 的基础复盘修订已经过期。")
        if self.is_expired_at(checked_at) or self.action_set.is_expired_at(checked_at):
            raise ReviewRunDomainError("FinalizationProposal 或 ActionSet 已经过期。")
        self.action_set.validate_expected_versions(current_versions)

    def is_expired_at(self, timestamp: str) -> bool:
        return _require_datetime(timestamp, "校验时间") >= _require_datetime(
            self.expires_at,
            "FinalizationProposal 过期时间",
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FinalizationProposal:
        proposal = cls(
            proposal_id=str(value.get("proposal_id", "")),
            run_id=str(value.get("run_id", "")),
            trade_date=str(value.get("trade_date", "")),
            manifest_id=str(value.get("manifest_id", "")),
            manifest_hash=str(value.get("manifest_hash", "")),
            base_review_revision_id=str(value.get("base_review_revision_id", "")),
            expected_current_review_revision_id=str(
                value.get("expected_current_review_revision_id", "")
            ),
            final_revision=RevisionBundle.from_dict(
                _require_mapping(value.get("final_revision", {}), "最终 RevisionBundle")
            ),
            action_set=ActionSet.from_dict(
                _require_mapping(value.get("action_set", {}), "ActionSet")
            ),
            created_at=str(value.get("created_at", "")),
            expires_at=str(value.get("expires_at", "")),
            content_hash=str(value.get("content_hash", "")),
        )
        proposal.validate()
        return proposal

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "manifest_id": self.manifest_id,
            "manifest_hash": self.manifest_hash,
            "base_review_revision_id": self.base_review_revision_id,
            "expected_current_review_revision_id": self.expected_current_review_revision_id,
            "final_revision": self.final_revision.to_dict(),
            "action_set": self.action_set.to_dict(),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    def _validate_bindings(self) -> None:
        revision = self.final_revision
        action_set = self.action_set
        if self.base_review_revision_id != self.expected_current_review_revision_id:
            raise ReviewRunDomainError("定稿基础修订必须等于预览时的期望当前修订。")
        if revision.replaces_review_revision_id != self.base_review_revision_id:
            raise ReviewRunDomainError("最终 RevisionBundle 未声明替代定稿基础修订。")
        if revision.revision_kind != "final":
            raise ReviewRunDomainError("FinalizationProposal 必须包含 final RevisionBundle。")
        identity = (self.run_id, self.trade_date)
        if (revision.run_id, revision.trade_date) != identity:
            raise ReviewRunDomainError("最终 RevisionBundle 与 FinalizationProposal 跨 run 或跨日。")
        if (action_set.run_id, action_set.trade_date) != identity:
            raise ReviewRunDomainError("ActionSet 与 FinalizationProposal 跨 run 或跨日。")
        if self.manifest_id != action_set.manifest_id:
            raise ReviewRunDomainError("ActionSet 与 FinalizationProposal 的 manifest ID 不一致。")
        if self.manifest_hash != action_set.manifest_hash:
            raise ReviewRunDomainError("ActionSet 与 FinalizationProposal 的 manifest 哈希不一致。")
        if action_set.review_revision_id != revision.review_revision_id:
            raise ReviewRunDomainError("ActionSet 未绑定最终复盘修订 ID。")
        if action_set.review_revision_hash != revision.content_hash:
            raise ReviewRunDomainError("ActionSet 未绑定最终复盘修订哈希。")
        if action_set.created_at != self.created_at or action_set.expires_at != self.expires_at:
            raise ReviewRunDomainError("联合生成的 RevisionBundle 与 ActionSet 必须使用同一确认窗口。")
        if revision.created_at != self.created_at:
            raise ReviewRunDomainError("最终 RevisionBundle 必须与 ActionSet 联合生成。")

    def _validate_publication_action(self) -> None:
        publish_actions = [
            action for action in self.action_set.actions if action.action_type == "publish_review"
        ]
        if len(publish_actions) != 1:
            raise ReviewRunDomainError("ActionSet 必须且只能包含一个 publish_review 动作。")
        action = publish_actions[0]
        if action.target_type != "review_publication" or action.target_id != self.trade_date:
            raise ReviewRunDomainError("publish_review 必须锁定当前交易日 publication。")
        expected_payload = {
            "trade_date": self.trade_date,
            "run_id": self.run_id,
            "review_revision_id": self.final_revision.review_revision_id,
            "review_revision_hash": self.final_revision.content_hash,
            "report_hash": self.action_set.report_hash,
        }
        if action.payload != expected_payload:
            raise ReviewRunDomainError("publish_review payload 与最终修订或报告哈希不一致。")

    def _validate_effect_declarations(self) -> None:
        if self.action_set.database_effects != FINALIZATION_DATABASE_EFFECTS:
            raise ReviewRunDomainError(
                "ActionSet 数据库影响必须完整声明最终发布协议的全部写入。"
            )
        expected_names = (
            f"{self.final_revision.review_revision_id}.md",
            f"{self.action_set.report_hash}.md",
        )
        if tuple(Path(item).name for item in self.action_set.file_effects) != expected_names:
            raise ReviewRunDomainError(
                "ActionSet 文件影响必须使用 revision ID 与报告哈希作为文件名。"
            )

    def _validate_observation_actions(self) -> None:
        observation_actions = [
            action
            for action in self.action_set.actions
            if action.action_type == "create_observation"
        ]
        plans = {item.plan_item_id: item for item in self.final_revision.approved_plan_items}
        expectations = {
            expectation.target_key: expectation.expected_version
            for expectation in self.action_set.version_expectations
        }
        actions_by_plan: dict[str, list[BusinessAction]] = {}
        for action in observation_actions:
            if action.target_type != "observation":
                raise ReviewRunDomainError("create_observation 必须使用 observation 目标。")
            if expectations[action.target_key] is not None:
                raise ReviewRunDomainError("create_observation 的期望旧版本必须是 null。")
            plan_item_id = str(action.payload.get("plan_item_id", ""))
            actions_by_plan.setdefault(plan_item_id, []).append(action)
        if set(actions_by_plan) != set(plans):
            raise ReviewRunDomainError("每个且只有 approved PlanItem 必须创建对应 Observation。")
        for plan_item_id, plan in plans.items():
            actions = actions_by_plan[plan_item_id]
            if len(actions) != 1:
                raise ReviewRunDomainError(
                    f"计划项 {plan_item_id} 必须且只能创建一个 Observation。"
                )
            self._validate_observation_snapshot(actions[0], plan)

    def _validate_pool_actions(self) -> None:
        for action in self.action_set.actions:
            if action.action_type != "update_pool_status":
                continue
            pool_type = action.payload.get("pool_type")
            code = action.payload.get("code")
            status = action.payload.get("status")
            if not isinstance(pool_type, str) or pool_type not in POOL_TYPES:
                raise ReviewRunDomainError("池动作 pool_type 必须是 watch 或 hot。")
            if not isinstance(code, str) or not code.strip():
                raise ReviewRunDomainError("池动作 code 不能为空。")
            if not isinstance(status, str) or status not in POOL_STATUSES:
                raise ReviewRunDomainError("池动作 status 必须是 active、paused 或 removed。")
            if action.target_id != f"{pool_type}:{code}":
                raise ReviewRunDomainError("池动作目标与 payload 的 pool_type/code 不一致。")

    def _validate_observation_result_actions(self) -> None:
        for action in self.action_set.actions:
            if action.action_type != "record_observation_result":
                continue
            payload = action.payload
            observation_id = payload.get("observation_id")
            status = payload.get("status")
            actual_result = payload.get("actual_result")
            reason = payload.get("reason")
            evidence_references = payload.get("evidence_references")
            if not isinstance(observation_id, str) or observation_id != action.target_id:
                raise ReviewRunDomainError(
                    "Observation 结果动作目标与 payload 的 observation_id 不一致。"
                )
            if not isinstance(status, str):
                raise ReviewRunDomainError("Observation 结果 status 必须是字符串。")
            if not isinstance(actual_result, str):
                raise ReviewRunDomainError("Observation 实际结果必须是字符串。")
            if not isinstance(reason, str):
                raise ReviewRunDomainError("Observation 结果原因必须是字符串。")
            if not isinstance(evidence_references, list) or any(
                not isinstance(item, str) for item in evidence_references
            ):
                raise ReviewRunDomainError(
                    "Observation 结果动作的 evidence_references 必须是字符串数组。"
                )
            validate_observation_result_fields(
                status,
                actual_result,
                reason,
                tuple(evidence_references),
            )

    def _validate_observation_snapshot(
        self,
        action: BusinessAction,
        plan: PlanItem,
    ) -> None:
        expected_payload = {
            "observation_id": action.target_id,
            "review_revision_id": self.final_revision.review_revision_id,
            "review_revision_hash": self.final_revision.content_hash,
            "plan_item_id": plan.plan_item_id,
            "hypothesis": plan.hypothesis,
            "confirmation_condition": plan.confirmation_condition,
            "invalidation_condition": plan.invalidation_condition,
            "evidence_references": list(plan.evidence_references),
            "review_deadline": plan.review_deadline,
            "created_at": self.action_set.created_at,
        }
        if action.payload != expected_payload:
            raise ReviewRunDomainError(
                f"Observation {action.target_id} 的字段快照与计划项 {plan.plan_item_id} 不一致。"
            )


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ReviewRunDomainError(f"{label}不能为空。")


# Observation 结果规则属于 ActionSet 提交协议；预览生成和最终提交必须复用同一校验。
def validate_observation_result_fields(
    status: str,
    actual_result: str,
    reason: str,
    evidence_references: tuple[str, ...],
) -> None:
    if status not in OBSERVATION_RESULT_STATUSES:
        raise ReviewRunDomainError("Observation 结果必须是 hit、miss 或 invalid。")
    if not actual_result.strip():
        raise ReviewRunDomainError("Observation 实际结果不能为空。")
    if not evidence_references or any(not item.strip() for item in evidence_references):
        raise ReviewRunDomainError("Observation 回填必须包含非空证据引用。")
    if status in {"miss", "invalid"} and reason not in OBSERVATION_RESULT_REASONS:
        raise ReviewRunDomainError("miss 或 invalid 必须使用固定原因分类。")
    if status == "hit" and reason:
        raise ReviewRunDomainError("hit 结果不能填写失败原因。")


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


def _text_tuple(value: Mapping[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(item) for item in value.get(key, ()))
