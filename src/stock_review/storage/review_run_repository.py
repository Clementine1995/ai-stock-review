# 本文件负责新版 ReviewRun 的 SQLite 持久化、不可变修订和乐观并发事务边界。

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator


SCHEMA_VERSION = 1
OBSERVATION_RESULT_STATUSES = frozenset({"hit", "miss", "invalid"})


def _inject_failure(
    hook: Callable[[str], None] | None,
    stage: str,
) -> None:
    if hook is not None:
        hook(stage)


class ReviewRunRepositoryError(Exception):
    pass


class OptimisticConcurrencyError(ReviewRunRepositoryError):
    pass


class ImmutableRecordError(ReviewRunRepositoryError):
    pass


class RecordNotFoundError(ReviewRunRepositoryError):
    pass


class InvalidActionSetError(ReviewRunRepositoryError):
    pass


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_runs (
    run_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_runs_trade_date_idx ON review_runs(trade_date);

CREATE TABLE IF NOT EXISTS review_run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    previous_event_id TEXT REFERENCES review_run_events(event_id),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS run_manifests (
    manifest_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_manifests_run_idx ON run_manifests(run_id);

CREATE TABLE IF NOT EXISTS evidence_captures (
    evidence_capture_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    manifest_id TEXT NOT NULL REFERENCES run_manifests(manifest_id),
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_captures_run_idx ON evidence_captures(run_id);

CREATE TABLE IF NOT EXISTS evidence_bundles (
    evidence_bundle_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    manifest_id TEXT NOT NULL REFERENCES run_manifests(manifest_id),
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_bundles_run_idx ON evidence_bundles(run_id);

CREATE TABLE IF NOT EXISTS review_revisions (
    review_revision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    evidence_bundle_id TEXT NOT NULL REFERENCES evidence_bundles(evidence_bundle_id),
    supersedes_review_revision_id TEXT REFERENCES review_revisions(review_revision_id),
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_revisions_run_idx ON review_revisions(run_id);

CREATE TABLE IF NOT EXISTS model_requests (
    model_request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    evidence_bundle_id TEXT NOT NULL REFERENCES evidence_bundles(evidence_bundle_id),
    base_review_revision_id TEXT REFERENCES review_revisions(review_revision_id),
    expected_current_revision_id TEXT REFERENCES review_revisions(review_revision_id),
    request_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS model_requests_run_idx ON model_requests(run_id, created_at);

CREATE TABLE IF NOT EXISTS model_responses (
    model_response_id TEXT PRIMARY KEY,
    model_request_id TEXT NOT NULL UNIQUE REFERENCES model_requests(model_request_id),
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    evidence_bundle_id TEXT NOT NULL REFERENCES evidence_bundles(evidence_bundle_id),
    base_review_revision_id TEXT REFERENCES review_revisions(review_revision_id),
    expected_current_revision_id TEXT REFERENCES review_revisions(review_revision_id),
    request_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS model_responses_run_idx ON model_responses(run_id, created_at);

CREATE TABLE IF NOT EXISTS finalization_proposals (
    proposal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    review_revision_id TEXT NOT NULL REFERENCES review_revisions(review_revision_id),
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS finalization_proposals_run_idx ON finalization_proposals(run_id);

CREATE TABLE IF NOT EXISTS action_sets (
    action_set_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    review_revision_id TEXT NOT NULL REFERENCES review_revisions(review_revision_id),
    manifest_id TEXT NOT NULL REFERENCES run_manifests(manifest_id),
    proposal_id TEXT REFERENCES finalization_proposals(proposal_id),
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS action_sets_run_idx ON action_sets(run_id);

CREATE TABLE IF NOT EXISTS review_publications (
    publication_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    review_revision_id TEXT NOT NULL REFERENCES review_revisions(review_revision_id),
    action_set_id TEXT REFERENCES action_sets(action_set_id),
    supersedes_publication_id TEXT REFERENCES review_publications(publication_id),
    report_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_publications_trade_date_idx
    ON review_publications(trade_date, created_at);

CREATE TABLE IF NOT EXISTS current_review_publications (
    trade_date TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL UNIQUE REFERENCES review_publications(publication_id)
);

CREATE TABLE IF NOT EXISTS pool_status_events (
    event_id TEXT PRIMARY KEY,
    pool_type TEXT NOT NULL,
    code TEXT NOT NULL,
    status TEXT NOT NULL,
    supersedes_event_id TEXT REFERENCES pool_status_events(event_id),
    action_set_id TEXT REFERENCES action_sets(action_set_id),
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pool_status_events_target_idx
    ON pool_status_events(pool_type, code, created_at);

CREATE TABLE IF NOT EXISTS current_pool_status (
    pool_type TEXT NOT NULL,
    code TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE REFERENCES pool_status_events(event_id),
    PRIMARY KEY (pool_type, code)
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    review_revision_id TEXT NOT NULL REFERENCES review_revisions(review_revision_id),
    plan_item_id TEXT NOT NULL,
    action_set_id TEXT REFERENCES action_sets(action_set_id),
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (review_revision_id, plan_item_id)
);
CREATE INDEX IF NOT EXISTS observations_run_idx ON observations(run_id);

CREATE TABLE IF NOT EXISTS observation_result_revisions (
    result_revision_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES observations(observation_id),
    status TEXT NOT NULL CHECK (status IN ('hit', 'miss', 'invalid')),
    supersedes_result_revision_id TEXT REFERENCES observation_result_revisions(result_revision_id),
    action_set_id TEXT REFERENCES action_sets(action_set_id),
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observation_result_revisions_observation_idx
    ON observation_result_revisions(observation_id, created_at);

CREATE TABLE IF NOT EXISTS current_observation_results (
    observation_id TEXT PRIMARY KEY REFERENCES observations(observation_id),
    result_revision_id TEXT NOT NULL UNIQUE REFERENCES observation_result_revisions(result_revision_id)
);

CREATE TABLE IF NOT EXISTS weekly_revisions (
    weekly_revision_id TEXT PRIMARY KEY,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_revision_review_publications (
    weekly_revision_id TEXT NOT NULL REFERENCES weekly_revisions(weekly_revision_id),
    publication_id TEXT NOT NULL REFERENCES review_publications(publication_id),
    PRIMARY KEY (weekly_revision_id, publication_id)
);

CREATE TABLE IF NOT EXISTS weekly_revision_observation_results (
    weekly_revision_id TEXT NOT NULL REFERENCES weekly_revisions(weekly_revision_id),
    result_revision_id TEXT NOT NULL REFERENCES observation_result_revisions(result_revision_id),
    PRIMARY KEY (weekly_revision_id, result_revision_id)
);

CREATE TABLE IF NOT EXISTS weekly_revision_pool_events (
    weekly_revision_id TEXT NOT NULL REFERENCES weekly_revisions(weekly_revision_id),
    event_id TEXT NOT NULL REFERENCES pool_status_events(event_id),
    PRIMARY KEY (weekly_revision_id, event_id)
);

CREATE TABLE IF NOT EXISTS weekly_publications (
    weekly_publication_id TEXT PRIMARY KEY,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    weekly_revision_id TEXT NOT NULL REFERENCES weekly_revisions(weekly_revision_id),
    supersedes_weekly_publication_id TEXT REFERENCES weekly_publications(weekly_publication_id),
    report_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS weekly_publications_period_idx
    ON weekly_publications(period_start, period_end, created_at);

CREATE TABLE IF NOT EXISTS current_weekly_publications (
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    weekly_publication_id TEXT NOT NULL UNIQUE REFERENCES weekly_publications(weekly_publication_id),
    PRIMARY KEY (period_start, period_end)
);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    run_id TEXT REFERENCES review_runs(run_id),
    action_set_id TEXT REFERENCES action_sets(action_set_id),
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_run_idx ON audit_events(run_id, created_at);

CREATE TABLE IF NOT EXISTS audit_outbox (
    outbox_id TEXT PRIMARY KEY,
    audit_event_id TEXT NOT NULL UNIQUE REFERENCES audit_events(audit_event_id),
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS action_set_applications (
    action_set_id TEXT PRIMARY KEY REFERENCES action_sets(action_set_id),
    result_json TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_imports (
    source_kind TEXT NOT NULL,
    source_key TEXT NOT NULL,
    classification TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (source_kind, source_key)
);
"""


_IMMUTABLE_TABLES = (
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
    "pool_status_events",
    "observations",
    "observation_result_revisions",
    "weekly_revisions",
    "weekly_revision_review_publications",
    "weekly_revision_observation_results",
    "weekly_revision_pool_events",
    "weekly_publications",
    "audit_events",
    "action_set_applications",
    "legacy_imports",
)


class ReviewRunRepository:
    # 迁移可绑定已取得写锁的外部事务；普通业务调用仍由仓储自行管理连接。
    def __init__(
        self,
        database_path: Path,
        *,
        transaction_connection: sqlite3.Connection | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self._transaction_connection = transaction_connection

    # 初始化只面向新版空库，并追加记录当前 schema 版本。
    def initialize_schema(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            self._ensure_schema_target(connection)
            _execute_sql_script(connection, _SCHEMA_SQL)
            self._create_immutable_triggers(connection)
            latest = connection.execute(
                "SELECT MAX(version) FROM schema_versions"
            ).fetchone()[0]
            if latest is not None and latest > SCHEMA_VERSION:
                raise ReviewRunRepositoryError(
                    f"数据库 schema 版本 {latest} 高于当前支持版本 {SCHEMA_VERSION}。"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_versions (version, description, applied_at)
                VALUES (?, ?, ?)
                """,
                (SCHEMA_VERSION, "ReviewRun 初始持久化结构", _utc_now()),
            )

    # 返回按版本号排序的 schema 版本历史。
    def list_schema_versions(self) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT version, description, applied_at FROM schema_versions ORDER BY version"
            ).fetchall()
        return [dict(row) for row in rows]

    # 只读迁移先登记旧产物原貌；相同来源键只能幂等重放同一分类和内容。
    def register_legacy_import(
        self,
        source_kind: str,
        source_key: str,
        classification: str,
        payload: Mapping[str, Any],
        imported_at: str | None = None,
    ) -> dict[str, Any]:
        for value, label in (
            (source_kind, "source_kind"),
            (source_key, "source_key"),
            (classification, "classification"),
        ):
            _require_text(value, label)
        payload_json, content_hash = _encode_payload(payload)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM legacy_imports
                WHERE source_kind = ? AND source_key = ?
                """,
                (source_kind, source_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["classification"] == classification
                    and existing["payload_json"] == payload_json
                    and existing["content_hash"] == content_hash
                ):
                    return _decode_payload_row(existing)
                raise ImmutableRecordError(
                    f"legacy_import {source_kind}:{source_key} 已存在且内容不同。"
                )
            connection.execute(
                """
                INSERT INTO legacy_imports (
                    source_kind, source_key, classification, payload_json,
                    content_hash, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_kind,
                    source_key,
                    classification,
                    payload_json,
                    content_hash,
                    imported_at or _utc_now(),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM legacy_imports
                WHERE source_kind = ? AND source_key = ?
                """,
                (source_kind, source_key),
            ).fetchone()
            return _decode_payload_row(row)

    # 按可选来源类型和分类读取 legacy_import 清单，不解释或补造旧关系。
    def list_legacy_imports(
        self,
        source_kind: str | None = None,
        classification: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        values: list[str] = []
        if source_kind is not None:
            conditions.append("source_kind = ?")
            values.append(_require_text(source_kind, "source_kind"))
        if classification is not None:
            conditions.append("classification = ?")
            values.append(_require_text(classification, "classification"))
        where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM legacy_imports
                """
                + where_clause
                + " ORDER BY source_kind, source_key",
                values,
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 创建稳定的 ReviewRun 身份；相同 ID 只允许完全相同的幂等重放。
    def create_review_run(
        self,
        run_id: str,
        trade_date: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        _require_text(run_id, "run_id")
        _require_text(trade_date, "trade_date")
        with self._transaction() as connection:
            return self._save_json_record(
                connection,
                "review_runs",
                "run_id",
                run_id,
                {"trade_date": trade_date},
                payload,
            )

    # 按稳定 ID 读取 ReviewRun，不存在时返回 None。
    def get_review_run(self, run_id: str) -> dict[str, Any] | None:
        return self._get_json_record("review_runs", "run_id", run_id)

    # 追加生命周期事件，并以期望前序事件保护同一运行的事件顺序。
    def append_review_run_event(
        self,
        run_id: str,
        event_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        expected_previous_event_id: str | None,
    ) -> dict[str, Any]:
        _require_text(run_id, "run_id")
        _require_text(event_id, "event_id")
        _require_text(event_type, "event_type")
        with self._transaction() as connection:
            return self._append_review_run_event_in_transaction(
                connection,
                run_id,
                event_id,
                event_type,
                payload,
                expected_previous_event_id,
            )

    # 读取一个 ReviewRun 的完整追加事件流。
    def list_review_run_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_run_events
                WHERE run_id = ?
                ORDER BY sequence_number
                """,
                (run_id,),
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 保存用户批准的运行清单。
    def save_run_manifest(
        self,
        manifest_id: str,
        run_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            self._require_row(connection, "review_runs", "run_id", run_id)
            return self._save_json_record(
                connection,
                "run_manifests",
                "manifest_id",
                manifest_id,
                {"run_id": run_id},
                payload,
            )

    # 读取单个 RunManifest。
    def get_run_manifest(self, manifest_id: str) -> dict[str, Any] | None:
        return self._get_json_record("run_manifests", "manifest_id", manifest_id)

    # 保存单个来源槽位的不可变采集结果。
    def save_evidence_capture(
        self,
        evidence_capture_id: str,
        run_id: str,
        manifest_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            self._require_manifest_run(connection, manifest_id, run_id)
            return self._save_json_record(
                connection,
                "evidence_captures",
                "evidence_capture_id",
                evidence_capture_id,
                {"run_id": run_id, "manifest_id": manifest_id},
                payload,
            )

    # 读取单个 EvidenceCapture。
    def get_evidence_capture(self, evidence_capture_id: str) -> dict[str, Any] | None:
        return self._get_json_record(
            "evidence_captures", "evidence_capture_id", evidence_capture_id
        )

    # 按创建顺序读取运行内的采集结果。
    def list_evidence_captures(self, run_id: str) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_captures WHERE run_id = ? ORDER BY created_at, evidence_capture_id",
                (run_id,),
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 保存封存后的 EvidenceBundle。
    def save_evidence_bundle(
        self,
        evidence_bundle_id: str,
        run_id: str,
        manifest_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            self._require_manifest_run(connection, manifest_id, run_id)
            return self._save_json_record(
                connection,
                "evidence_bundles",
                "evidence_bundle_id",
                evidence_bundle_id,
                {"run_id": run_id, "manifest_id": manifest_id},
                payload,
            )

    # 读取不可变 EvidenceBundle。
    def get_evidence_bundle(self, evidence_bundle_id: str) -> dict[str, Any] | None:
        return self._get_json_record(
            "evidence_bundles", "evidence_bundle_id", evidence_bundle_id
        )

    # 保存模型请求原文及其冻结输入；期望当前修订供应用层提交响应前再次比较。
    def save_model_request(
        self,
        model_request_id: str,
        run_id: str,
        evidence_bundle_id: str,
        base_review_revision_id: str | None,
        expected_current_revision_id: str | None,
        request_fingerprint: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            self._require_evidence_bundle_run(connection, evidence_bundle_id, run_id)
            for revision_id, label in (
                (base_review_revision_id, "base_review_revision_id"),
                (expected_current_revision_id, "expected_current_revision_id"),
            ):
                if revision_id is not None:
                    revision = self._require_revision_run(connection, revision_id, run_id)
                    if revision["evidence_bundle_id"] != evidence_bundle_id:
                        raise ReviewRunRepositoryError(
                            f"{label} 与模型请求的 EvidenceBundle 不一致。"
                        )
            return self._save_json_record(
                connection,
                "model_requests",
                "model_request_id",
                model_request_id,
                {
                    "run_id": run_id,
                    "evidence_bundle_id": evidence_bundle_id,
                    "base_review_revision_id": base_review_revision_id,
                    "expected_current_revision_id": expected_current_revision_id,
                    "request_fingerprint": _require_text(
                        request_fingerprint, "request_fingerprint"
                    ),
                },
                payload,
            )

    # 读取不可变模型请求，调用方据此核对请求时的期望当前修订。
    def get_model_request(self, model_request_id: str) -> dict[str, Any] | None:
        return self._get_json_record(
            "model_requests", "model_request_id", model_request_id
        )

    # 按创建顺序返回运行内全部不可变模型请求，供完整性核验使用。
    def list_model_requests(self, run_id: str) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM model_requests WHERE run_id = ? ORDER BY created_at, model_request_id",
                (run_id,),
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 保存模型响应时核对请求指纹，并把请求的冻结关联复制到不可变响应记录。
    def save_model_response(
        self,
        model_response_id: str,
        model_request_id: str,
        request_fingerprint: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            request = self._require_row(
                connection, "model_requests", "model_request_id", model_request_id
            )
            fingerprint = _require_text(request_fingerprint, "request_fingerprint")
            if request["request_fingerprint"] != fingerprint:
                raise ReviewRunRepositoryError("模型响应的请求指纹与原请求不一致。")
            return self._save_json_record(
                connection,
                "model_responses",
                "model_response_id",
                model_response_id,
                {
                    "model_request_id": model_request_id,
                    "run_id": request["run_id"],
                    "evidence_bundle_id": request["evidence_bundle_id"],
                    "base_review_revision_id": request["base_review_revision_id"],
                    "expected_current_revision_id": request[
                        "expected_current_revision_id"
                    ],
                    "request_fingerprint": fingerprint,
                },
                payload,
            )

    # 读取不可变模型响应及其冻结输入关系和响应内容哈希。
    def get_model_response(self, model_response_id: str) -> dict[str, Any] | None:
        return self._get_json_record(
            "model_responses", "model_response_id", model_response_id
        )

    # 按创建顺序返回运行内全部不可变模型响应，供请求、事件和修订交叉核验。
    def list_model_responses(self, run_id: str) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM model_responses WHERE run_id = ? ORDER BY created_at, model_response_id",
                (run_id,),
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 保存 STEP、PlanItem 和人工选择共版的不可变 RevisionBundle。
    def save_review_revision(
        self,
        review_revision_id: str,
        run_id: str,
        evidence_bundle_id: str,
        payload: Mapping[str, Any],
        supersedes_review_revision_id: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            self._require_evidence_bundle_run(connection, evidence_bundle_id, run_id)
            if supersedes_review_revision_id is not None:
                self._require_revision_run(
                    connection, supersedes_review_revision_id, run_id
                )
            return self._save_json_record(
                connection,
                "review_revisions",
                "review_revision_id",
                review_revision_id,
                {
                    "run_id": run_id,
                    "evidence_bundle_id": evidence_bundle_id,
                    "supersedes_review_revision_id": supersedes_review_revision_id,
                },
                payload,
            )

    # 读取 RevisionBundle。
    def get_review_revision(self, review_revision_id: str) -> dict[str, Any] | None:
        return self._get_json_record(
            "review_revisions", "review_revision_id", review_revision_id
        )

    # 保存最终修订和 ActionSet 的组合提案。
    def save_finalization_proposal(
        self,
        proposal_id: str,
        run_id: str,
        review_revision_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            self._require_revision_run(connection, review_revision_id, run_id)
            return self._save_json_record(
                connection,
                "finalization_proposals",
                "proposal_id",
                proposal_id,
                {"run_id": run_id, "review_revision_id": review_revision_id},
                payload,
            )

    # 读取 FinalizationProposal。
    def get_finalization_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        return self._get_json_record(
            "finalization_proposals", "proposal_id", proposal_id
        )

    # 保存用户待确认的 ActionSet；真正业务写入由 apply_action_set 完成。
    def save_action_set(
        self,
        action_set_id: str,
        run_id: str,
        review_revision_id: str,
        manifest_id: str,
        payload: Mapping[str, Any],
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            self._require_revision_run(connection, review_revision_id, run_id)
            self._require_manifest_run(connection, manifest_id, run_id)
            if proposal_id is not None:
                proposal = self._require_row(
                    connection, "finalization_proposals", "proposal_id", proposal_id
                )
                if (
                    proposal["run_id"] != run_id
                    or proposal["review_revision_id"] != review_revision_id
                ):
                    raise ReviewRunRepositoryError(
                        "FinalizationProposal 与 ActionSet 的运行或修订不一致。"
                    )
            return self._save_json_record(
                connection,
                "action_sets",
                "action_set_id",
                action_set_id,
                {
                    "run_id": run_id,
                    "review_revision_id": review_revision_id,
                    "manifest_id": manifest_id,
                    "proposal_id": proposal_id,
                },
                payload,
            )

    # 读取不可变 ActionSet。
    def get_action_set(self, action_set_id: str) -> dict[str, Any] | None:
        return self._get_json_record("action_sets", "action_set_id", action_set_id)

    # 在单个 IMMEDIATE 事务中校验全部期望版本并应用 ActionSet。
    def apply_action_set(self, action_set_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            return self._apply_action_set_in_transaction(connection, action_set_id)

    # 正式发布把 ReviewRun 事件与 ActionSet 业务效果放入同一事务，避免两套投影分裂。
    def apply_action_set_with_review_run_events(
        self,
        action_set_id: str,
        run_id: str,
        events: Sequence[
            tuple[str, str, Mapping[str, Any], str | None]
        ],
        failure_injector: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        _require_text(action_set_id, "action_set_id")
        _require_text(run_id, "run_id")
        if not events:
            raise ReviewRunRepositoryError("正式发布至少必须包含一个 ReviewRun 事件。")
        with self._transaction(failure_injector=failure_injector) as connection:
            action_set_row = self._require_row(
                connection, "action_sets", "action_set_id", action_set_id
            )
            if action_set_row["run_id"] != run_id:
                raise ReviewRunRepositoryError("ReviewRun 事件与 ActionSet 的运行不一致。")
            for event_id, event_type, payload, expected_previous_event_id in events:
                _require_text(event_id, "event_id")
                _require_text(event_type, "event_type")
                self._append_review_run_event_in_transaction(
                    connection,
                    run_id,
                    event_id,
                    event_type,
                    payload,
                    expected_previous_event_id,
                )
            _inject_failure(failure_injector, "after_review_run_events")
            return self._apply_action_set_in_transaction(
                connection,
                action_set_id,
                action_set_row=action_set_row,
                failure_injector=failure_injector,
            )

    # 调用方持有写事务时复用同一事件追加规则；相同 ID 只接受完全相同的重放。
    def _append_review_run_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        expected_previous_event_id: str | None,
    ) -> dict[str, Any]:
        self._require_row(connection, "review_runs", "run_id", run_id)
        payload_json, content_hash = _encode_payload(payload)
        existing = connection.execute(
            "SELECT * FROM review_run_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing is not None:
            if (
                existing["run_id"] == run_id
                and existing["event_type"] == event_type
                and existing["previous_event_id"] == expected_previous_event_id
                and existing["payload_json"] == payload_json
                and existing["content_hash"] == content_hash
            ):
                return _decode_payload_row(existing)
            raise ImmutableRecordError(f"事件 {event_id} 已存在且内容不同。")
        previous = connection.execute(
            """
            SELECT event_id, sequence_number
            FROM review_run_events
            WHERE run_id = ?
            ORDER BY sequence_number DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        actual_previous = previous["event_id"] if previous is not None else None
        self._assert_expected(
            "ReviewRun 事件",
            run_id,
            expected_previous_event_id,
            actual_previous,
        )
        sequence_number = (previous["sequence_number"] + 1) if previous else 1
        connection.execute(
            """
            INSERT INTO review_run_events (
                event_id, run_id, sequence_number, previous_event_id, event_type,
                payload_json, content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                run_id,
                sequence_number,
                expected_previous_event_id,
                event_type,
                payload_json,
                content_hash,
                _utc_now(),
            ),
        )
        row = connection.execute(
            "SELECT * FROM review_run_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return _decode_payload_row(row)

    # ActionSet 的全部业务动作在调用方已经持有的事务中完成。
    def _apply_action_set_in_transaction(
        self,
        connection: sqlite3.Connection,
        action_set_id: str,
        *,
        action_set_row: sqlite3.Row | None = None,
        failure_injector: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        application = connection.execute(
            "SELECT result_json, applied_at FROM action_set_applications WHERE action_set_id = ?",
            (action_set_id,),
        ).fetchone()
        if application is not None:
            result = json.loads(application["result_json"])
            result["replayed"] = True
            return result

        action_set_row = action_set_row or self._require_row(
            connection, "action_sets", "action_set_id", action_set_id
        )
        payload = json.loads(action_set_row["payload_json"])
        if not isinstance(payload, dict):
            raise InvalidActionSetError("ActionSet payload 必须是 JSON 对象。")

        publication = _optional_action_mapping(payload, "publication")
        pool_actions = _action_mapping_list(payload, "pool_actions")
        observation_creates = _action_mapping_list(payload, "observation_creates")
        result_actions = _action_mapping_list(payload, "observation_result_actions")
        audit = _required_action_mapping(payload, "audit")
        self._validate_action_target_uniqueness(
            pool_actions, observation_creates, result_actions
        )

        publication_id = None
        if publication is not None:
            publication_id = self._apply_publication_action(
                connection, action_set_row, publication
            )["publication_id"]
        _inject_failure(failure_injector, "after_publication_update")
        pool_event_ids = [
            self._apply_pool_action(connection, action_set_id, action)["event_id"]
            for action in pool_actions
        ]
        observation_ids = [
            self._apply_observation_create(connection, action_set_row, action)[
                "observation_id"
            ]
            for action in observation_creates
        ]
        result_revision_ids = [
            self._apply_observation_result_action(connection, action_set_id, action)[
                "result_revision_id"
            ]
            for action in result_actions
        ]
        _inject_failure(failure_injector, "after_business_actions")
        audit_record = self._apply_audit_action(connection, action_set_row, audit)
        _inject_failure(failure_injector, "after_audit_outbox")
        result = {
            "action_set_id": action_set_id,
            "publication_id": publication_id,
            "pool_event_ids": pool_event_ids,
            "observation_ids": observation_ids,
            "result_revision_ids": result_revision_ids,
            "audit_event_id": audit_record["audit_event_id"],
            "outbox_id": audit_record["outbox_id"],
            "replayed": False,
        }
        result_json, _ = _encode_payload(result)
        connection.execute(
            """
            INSERT INTO action_set_applications (action_set_id, result_json, applied_at)
            VALUES (?, ?, ?)
            """,
            (action_set_id, result_json, _utc_now()),
        )
        _inject_failure(failure_injector, "after_action_set_application")
        return result

    # 低层发布接口用 expected publication ID 原子切换同交易日唯一指针。
    def publish_review(
        self,
        publication_id: str,
        trade_date: str,
        run_id: str,
        review_revision_id: str,
        report_hash: str,
        payload: Mapping[str, Any],
        expected_publication_id: str | None,
        action_set_id: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            return self._publish_review_in_transaction(
                connection,
                publication_id,
                trade_date,
                run_id,
                review_revision_id,
                report_hash,
                payload,
                expected_publication_id,
                action_set_id,
            )

    # 读取某交易日当前正式 publication。
    def get_current_review_publication(
        self, trade_date: str
    ) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT publication.*
                FROM current_review_publications AS current
                JOIN review_publications AS publication
                  ON publication.publication_id = current.publication_id
                WHERE current.trade_date = ?
                """,
                (trade_date,),
            ).fetchone()
        return _decode_payload_row(row) if row is not None else None

    # 返回日期范围内每个交易日当前指针指向的正式日报，供周报冻结精确版本。
    def list_current_review_publications(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        values: list[str] = []
        if start_date is not None:
            conditions.append("publication.trade_date >= ?")
            values.append(_require_text(start_date, "start_date"))
        if end_date is not None:
            conditions.append("publication.trade_date <= ?")
            values.append(_require_text(end_date, "end_date"))
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ReviewRunRepositoryError("start_date 不能晚于 end_date。")
        where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT publication.*
                FROM current_review_publications AS current
                JOIN review_publications AS publication
                  ON publication.publication_id = current.publication_id
                """
                + where_clause
                + " ORDER BY publication.trade_date, publication.publication_id",
                values,
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 在单个只读事务中冻结周报需要的来源记录及其完整版本头。
    def load_weekly_source_snapshot(
        self,
        period_start: str,
        period_end: str,
    ) -> dict[str, Any]:
        if period_start > period_end:
            raise ReviewRunRepositoryError("period_start 不能晚于 period_end。")
        with self._read_connection() as connection:
            connection.execute("BEGIN")
            try:
                snapshot = self._load_weekly_source_snapshot_in_transaction(
                    connection,
                    _require_text(period_start, "period_start"),
                    _require_text(period_end, "period_end"),
                )
            finally:
                connection.rollback()
        return snapshot

    # 发布事务复用同一查询，确保冻结输入和提交前 CAS 使用完全相同的来源定义。
    def _load_weekly_source_snapshot_in_transaction(
        self,
        connection: sqlite3.Connection,
        period_start: str,
        period_end: str,
    ) -> dict[str, Any]:
        publication_rows = connection.execute(
            """
            SELECT publication.*
            FROM current_review_publications AS current
            JOIN review_publications AS publication
              ON publication.publication_id = current.publication_id
            WHERE publication.trade_date >= ? AND publication.trade_date <= ?
            ORDER BY publication.trade_date, publication.publication_id
            """,
            (period_start, period_end),
        ).fetchall()
        publications = [_decode_payload_row(row) for row in publication_rows]
        revision_ids = [item["review_revision_id"] for item in publications]
        action_set_ids = sorted(
            {
                item["action_set_id"]
                for item in publications
                if item["action_set_id"] is not None
            }
        )

        revision_rows: list[sqlite3.Row] = []
        if revision_ids:
            placeholders = ", ".join("?" for _ in revision_ids)
            revision_rows = connection.execute(
                f"""
                SELECT * FROM review_revisions
                WHERE review_revision_id IN ({placeholders})
                """,
                revision_ids,
            ).fetchall()
        revisions_by_id = {
            row["review_revision_id"]: _decode_payload_row(row)
            for row in revision_rows
        }
        revisions = [
            revisions_by_id[revision_id]
            for revision_id in revision_ids
            if revision_id in revisions_by_id
        ]

        observation_rows: list[sqlite3.Row] = []
        if revision_ids and action_set_ids:
            revision_placeholders = ", ".join("?" for _ in revision_ids)
            action_placeholders = ", ".join("?" for _ in action_set_ids)
            observation_rows = connection.execute(
                f"""
                SELECT
                    observation.*,
                    run.trade_date AS review_date,
                    result.result_revision_id AS current_result_revision_id,
                    result.status AS current_result_status,
                    result.supersedes_result_revision_id
                        AS current_result_supersedes_revision_id,
                    result.action_set_id AS current_result_action_set_id,
                    result.payload_json AS current_result_payload_json,
                    result.content_hash AS current_result_content_hash,
                    result.created_at AS current_result_created_at
                FROM observations AS observation
                JOIN review_runs AS run ON run.run_id = observation.run_id
                LEFT JOIN current_observation_results AS current
                  ON current.observation_id = observation.observation_id
                LEFT JOIN observation_result_revisions AS result
                  ON result.result_revision_id = current.result_revision_id
                WHERE observation.review_revision_id IN ({revision_placeholders})
                  AND observation.action_set_id IN ({action_placeholders})
                ORDER BY run.trade_date, observation.created_at, observation.observation_id
                """,
                (*revision_ids, *action_set_ids),
            ).fetchall()
        observations = [_decode_weekly_observation_row(row) for row in observation_rows]
        publication_bindings = {
            (
                item["run_id"],
                item["review_revision_id"],
                item["action_set_id"],
            )
            for item in publications
        }
        for observation in observations:
            observation_binding = (
                observation["run_id"],
                observation["review_revision_id"],
                observation["action_set_id"],
            )
            if observation_binding not in publication_bindings:
                raise ReviewRunRepositoryError(
                    "Observation 未绑定同一份正式日报的 "
                    "run_id、review_revision_id 和 action_set_id。"
                )

        pool_event_rows: list[sqlite3.Row] = []
        if action_set_ids:
            placeholders = ", ".join("?" for _ in action_set_ids)
            pool_event_rows = connection.execute(
                f"""
                SELECT * FROM pool_status_events
                WHERE action_set_id IN ({placeholders})
                ORDER BY action_set_id, created_at, event_id
                """,
                action_set_ids,
            ).fetchall()
        pool_events = [_decode_payload_row(row) for row in pool_event_rows]
        pool_heads = []
        for pool_type, code in sorted(
            {(item["pool_type"], item["code"]) for item in pool_events}
        ):
            row = connection.execute(
                """
                SELECT event_id FROM current_pool_status
                WHERE pool_type = ? AND code = ?
                """,
                (pool_type, code),
            ).fetchone()
            pool_heads.append(
                {
                    "pool_type": pool_type,
                    "code": code,
                    "event_id": row["event_id"] if row is not None else None,
                }
            )

        source_heads = {
            "review_publications": [
                {
                    "trade_date": item["trade_date"],
                    "publication_id": item["publication_id"],
                    "content_hash": item["content_hash"],
                }
                for item in publications
            ],
            "observation_results": [
                {
                    "observation_id": item["observation_id"],
                    "run_id": item["run_id"],
                    "review_revision_id": item["review_revision_id"],
                    "action_set_id": item["action_set_id"],
                    "observation_content_hash": item["content_hash"],
                    "result_revision_id": item["current_result"]["result_revision_id"],
                    "result_content_hash": item["current_result"]["content_hash"],
                }
                for item in observations
            ],
            "pool_status": pool_heads,
            "pool_events": [
                {
                    "event_id": item["event_id"],
                    "content_hash": item["content_hash"],
                }
                for item in pool_events
            ],
            "review_revisions": [
                {
                    "review_revision_id": item["review_revision_id"],
                    "content_hash": item["content_hash"],
                }
                for item in revisions
            ],
        }
        return {
            "publications": publications,
            "revisions": revisions,
            "observations": observations,
            "pool_events": pool_events,
            "source_heads": source_heads,
        }

    # 返回某交易日全部不可变 publication 历史。
    def list_review_publications(self, trade_date: str) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_publications
                WHERE trade_date = ?
                ORDER BY created_at, publication_id
                """,
                (trade_date,),
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 低层池状态接口使用事件头 ID 做 CAS，并保留完整追加历史。
    def append_pool_status_event(
        self,
        event_id: str,
        pool_type: str,
        code: str,
        status: str,
        payload: Mapping[str, Any],
        expected_event_id: str | None,
        action_set_id: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            return self._append_pool_status_event_in_transaction(
                connection,
                event_id,
                pool_type,
                code,
                status,
                payload,
                expected_event_id,
                action_set_id,
            )

    # 读取池对象当前状态事件。
    def get_current_pool_status(
        self, pool_type: str, code: str
    ) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT event.*
                FROM current_pool_status AS current
                JOIN pool_status_events AS event ON event.event_id = current.event_id
                WHERE current.pool_type = ? AND current.code = ?
                """,
                (pool_type, code),
            ).fetchone()
        return _decode_payload_row(row) if row is not None else None

    # 返回当前池状态事件投影；历史事件仍通过 list_pool_status_events 单独回查。
    def list_current_pool_statuses(
        self,
        pool_type: str | None = None,
    ) -> list[dict[str, Any]]:
        values: list[str] = []
        where_clause = ""
        if pool_type is not None:
            where_clause = " WHERE event.pool_type = ?"
            values.append(_require_text(pool_type, "pool_type"))
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT event.*
                FROM current_pool_status AS current
                JOIN pool_status_events AS event ON event.event_id = current.event_id
                """
                + where_clause
                + " ORDER BY event.pool_type, event.code",
                values,
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 读取池对象的完整状态事件历史。
    def list_pool_status_events(
        self, pool_type: str, code: str
    ) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pool_status_events
                WHERE pool_type = ? AND code = ?
                ORDER BY created_at, event_id
                """,
                (pool_type, code),
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 周报只读取所选正式日报 ActionSet 产生的池事件，不把其它运行混入冻结输入。
    def list_pool_status_events_for_action_sets(
        self, action_set_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        normalized_ids = _unique_text_values(action_set_ids, "action_set_ids")
        if not normalized_ids:
            return []
        placeholders = ", ".join("?" for _ in normalized_ids)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM pool_status_events
                WHERE action_set_id IN ({placeholders})
                ORDER BY action_set_id, created_at, event_id
                """,
                normalized_ids,
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 创建不含结果状态的 Observation 基础记录。
    def create_observation(
        self,
        observation_id: str,
        run_id: str,
        review_revision_id: str,
        plan_item_id: str,
        payload: Mapping[str, Any],
        action_set_id: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            return self._create_observation_in_transaction(
                connection,
                observation_id,
                run_id,
                review_revision_id,
                plan_item_id,
                payload,
                action_set_id,
            )

    # 读取 Observation 基础记录。
    def get_observation(self, observation_id: str) -> dict[str, Any] | None:
        return self._get_json_record("observations", "observation_id", observation_id)

    # 返回基础记录和当前结果投影；截止日筛选只读取基础 payload 的 review_deadline。
    def list_observations(
        self,
        review_date: str | None = None,
        due_on_or_before: str | None = None,
    ) -> list[dict[str, Any]]:
        values: list[str] = []
        where_clause = ""
        if review_date is not None:
            where_clause = " WHERE run.trade_date = ?"
            values.append(_require_text(review_date, "review_date"))
        due_date = (
            _require_text(due_on_or_before, "due_on_or_before")
            if due_on_or_before is not None
            else None
        )
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    observation.*,
                    run.trade_date AS review_date,
                    result.result_revision_id AS current_result_revision_id,
                    result.status AS current_result_status,
                    result.supersedes_result_revision_id
                        AS current_result_supersedes_revision_id,
                    result.action_set_id AS current_result_action_set_id,
                    result.payload_json AS current_result_payload_json,
                    result.content_hash AS current_result_content_hash,
                    result.created_at AS current_result_created_at
                FROM observations AS observation
                JOIN review_runs AS run ON run.run_id = observation.run_id
                LEFT JOIN current_observation_results AS current
                  ON current.observation_id = observation.observation_id
                LEFT JOIN observation_result_revisions AS result
                  ON result.result_revision_id = current.result_revision_id
                """
                + where_clause
                + " ORDER BY run.trade_date, observation.created_at, observation.observation_id",
                values,
            ).fetchall()

        records: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            deadline = payload.get("review_deadline")
            if due_date is not None and (
                not isinstance(deadline, str) or deadline > due_date
            ):
                continue
            current_result = {
                "observation_id": row["observation_id"],
                "result_revision_id": None,
                "status": "pending",
                "payload": {},
                "content_hash": None,
                "created_at": None,
            }
            if row["current_result_revision_id"] is not None:
                current_result = {
                    "result_revision_id": row["current_result_revision_id"],
                    "observation_id": row["observation_id"],
                    "status": row["current_result_status"],
                    "supersedes_result_revision_id": row[
                        "current_result_supersedes_revision_id"
                    ],
                    "action_set_id": row["current_result_action_set_id"],
                    "payload": json.loads(row["current_result_payload_json"]),
                    "content_hash": row["current_result_content_hash"],
                    "created_at": row["current_result_created_at"],
                }
            records.append(
                {
                    "observation_id": row["observation_id"],
                    "run_id": row["run_id"],
                    "review_date": row["review_date"],
                    "review_revision_id": row["review_revision_id"],
                    "plan_item_id": row["plan_item_id"],
                    "action_set_id": row["action_set_id"],
                    "payload": payload,
                    "content_hash": row["content_hash"],
                    "created_at": row["created_at"],
                    "current_result": current_result,
                }
            )
        return records

    # 追加 Observation 结果修订，并以期望结果头防止并发覆盖。
    def append_observation_result_revision(
        self,
        observation_id: str,
        result_revision_id: str,
        status: str,
        payload: Mapping[str, Any],
        expected_result_revision_id: str | None,
        action_set_id: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            return self._append_observation_result_in_transaction(
                connection,
                observation_id,
                result_revision_id,
                status,
                payload,
                expected_result_revision_id,
                action_set_id,
            )

    # 当前无结果修订时，投影固定返回 pending。
    def get_current_observation_result(
        self, observation_id: str
    ) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            observation = connection.execute(
                "SELECT observation_id FROM observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
            if observation is None:
                return None
            row = connection.execute(
                """
                SELECT revision.*
                FROM current_observation_results AS current
                JOIN observation_result_revisions AS revision
                  ON revision.result_revision_id = current.result_revision_id
                WHERE current.observation_id = ?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            return {
                "observation_id": observation_id,
                "result_revision_id": None,
                "status": "pending",
                "payload": {},
                "content_hash": None,
                "created_at": None,
            }
        return _decode_payload_row(row)

    # 读取 Observation 的全部结果修订。
    def list_observation_result_revisions(
        self, observation_id: str
    ) -> list[dict[str, Any]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM observation_result_revisions
                WHERE observation_id = ?
                ORDER BY created_at, result_revision_id
                """,
                (observation_id,),
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 保存周报修订，并用关联表冻结日报、结果修订和池状态事件引用。
    def save_weekly_revision(
        self,
        weekly_revision_id: str,
        period_start: str,
        period_end: str,
        payload: Mapping[str, Any],
        review_publication_ids: Sequence[str] = (),
        observation_result_revision_ids: Sequence[str] = (),
        pool_status_event_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        publication_ids = _unique_text_values(
            review_publication_ids, "review_publication_ids"
        )
        result_ids = _unique_text_values(
            observation_result_revision_ids, "observation_result_revision_ids"
        )
        pool_event_ids = _unique_text_values(
            pool_status_event_ids, "pool_status_event_ids"
        )
        payload_json, content_hash = _encode_payload(payload)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM weekly_revisions WHERE weekly_revision_id = ?",
                (weekly_revision_id,),
            ).fetchone()
            if existing is not None:
                stored = self._decode_weekly_revision(connection, existing)
                if (
                    existing["period_start"] == period_start
                    and existing["period_end"] == period_end
                    and existing["payload_json"] == payload_json
                    and existing["content_hash"] == content_hash
                    and stored["review_publication_ids"] == list(publication_ids)
                    and stored["observation_result_revision_ids"] == list(result_ids)
                    and stored["pool_status_event_ids"] == list(pool_event_ids)
                ):
                    return stored
                raise ImmutableRecordError(
                    f"周报修订 {weekly_revision_id} 已存在且内容不同。"
                )
            for publication_id in publication_ids:
                self._require_row(
                    connection,
                    "review_publications",
                    "publication_id",
                    publication_id,
                )
            for result_revision_id in result_ids:
                self._require_row(
                    connection,
                    "observation_result_revisions",
                    "result_revision_id",
                    result_revision_id,
                )
            for event_id in pool_event_ids:
                self._require_row(
                    connection, "pool_status_events", "event_id", event_id
                )
            created_at = _utc_now()
            connection.execute(
                """
                INSERT INTO weekly_revisions (
                    weekly_revision_id, period_start, period_end, payload_json,
                    content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    weekly_revision_id,
                    period_start,
                    period_end,
                    payload_json,
                    content_hash,
                    created_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO weekly_revision_review_publications
                    (weekly_revision_id, publication_id)
                VALUES (?, ?)
                """,
                [(weekly_revision_id, value) for value in publication_ids],
            )
            connection.executemany(
                """
                INSERT INTO weekly_revision_observation_results
                    (weekly_revision_id, result_revision_id)
                VALUES (?, ?)
                """,
                [(weekly_revision_id, value) for value in result_ids],
            )
            connection.executemany(
                """
                INSERT INTO weekly_revision_pool_events (weekly_revision_id, event_id)
                VALUES (?, ?)
                """,
                [(weekly_revision_id, value) for value in pool_event_ids],
            )
            row = connection.execute(
                "SELECT * FROM weekly_revisions WHERE weekly_revision_id = ?",
                (weekly_revision_id,),
            ).fetchone()
            return self._decode_weekly_revision(connection, row)

    # 读取周报修订及其所有固定引用。
    def get_weekly_revision(self, weekly_revision_id: str) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM weekly_revisions WHERE weekly_revision_id = ?",
                (weekly_revision_id,),
            ).fetchone()
            return (
                self._decode_weekly_revision(connection, row)
                if row is not None
                else None
            )

    # 发布周报修订，同周期 supersede 也必须携带期望 publication ID。
    def publish_weekly_revision(
        self,
        weekly_publication_id: str,
        period_start: str,
        period_end: str,
        weekly_revision_id: str,
        report_hash: str,
        payload: Mapping[str, Any],
        expected_weekly_publication_id: str | None,
        expected_source_heads: Mapping[str, Any],
        audit: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            revision = self._require_row(
                connection,
                "weekly_revisions",
                "weekly_revision_id",
                weekly_revision_id,
            )
            if (
                revision["period_start"] != period_start
                or revision["period_end"] != period_end
            ):
                raise ReviewRunRepositoryError("周报修订与发布周期不一致。")
            revision_payload = json.loads(revision["payload_json"])
            pinned_source_heads = revision_payload.get("source_heads")
            if not isinstance(expected_source_heads, Mapping):
                raise ReviewRunRepositoryError("发布周报必须显式提供来源版本头。")
            if not isinstance(pinned_source_heads, Mapping):
                raise ReviewRunRepositoryError("周报修订必须固定来源版本头。")
            expected_json, _ = _encode_payload(expected_source_heads)
            pinned_json, _ = _encode_payload(pinned_source_heads)
            if expected_json != pinned_json:
                raise ReviewRunRepositoryError(
                    "发布携带的周报来源版本头与已保存修订不一致。"
                )
            actual_snapshot = self._load_weekly_source_snapshot_in_transaction(
                connection,
                period_start,
                period_end,
            )
            actual_json, _ = _encode_payload(actual_snapshot["source_heads"])
            if pinned_json != actual_json:
                raise OptimisticConcurrencyError(
                    "周报来源指针在冻结后已经变化。"
                )
            current = connection.execute(
                """
                SELECT weekly_publication_id FROM current_weekly_publications
                WHERE period_start = ? AND period_end = ?
                """,
                (period_start, period_end),
            ).fetchone()
            actual_id = current["weekly_publication_id"] if current else None
            self._assert_expected(
                "周报 publication",
                f"{period_start}:{period_end}",
                expected_weekly_publication_id,
                actual_id,
            )
            row = self._save_json_record(
                connection,
                "weekly_publications",
                "weekly_publication_id",
                weekly_publication_id,
                {
                    "period_start": period_start,
                    "period_end": period_end,
                    "weekly_revision_id": weekly_revision_id,
                    "supersedes_weekly_publication_id": expected_weekly_publication_id,
                    "report_hash": _require_text(report_hash, "report_hash"),
                },
                payload,
            )
            self._switch_weekly_publication_pointer(
                connection,
                period_start,
                period_end,
                weekly_publication_id,
                expected_weekly_publication_id,
            )
            if audit is not None:
                self._append_audit_event_in_transaction(
                    connection,
                    _required_action_text(audit, "audit_event_id"),
                    _required_action_text(audit, "event_type"),
                    _required_action_mapping(audit, "payload"),
                    _required_action_text(audit, "outbox_id"),
                    _required_action_mapping(audit, "outbox_payload"),
                    None,
                    None,
                )
            return row

    # 读取周期当前正式周报 publication。
    def get_current_weekly_publication(
        self, period_start: str, period_end: str
    ) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT publication.*
                FROM current_weekly_publications AS current
                JOIN weekly_publications AS publication
                  ON publication.weekly_publication_id = current.weekly_publication_id
                WHERE current.period_start = ? AND current.period_end = ?
                """,
                (period_start, period_end),
            ).fetchone()
        return _decode_payload_row(row) if row is not None else None

    # 原子保存权威审计事件及其待投影 outbox 消息。
    def append_audit_event(
        self,
        audit_event_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        outbox_id: str,
        outbox_payload: Mapping[str, Any],
        run_id: str | None = None,
        action_set_id: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            return self._append_audit_event_in_transaction(
                connection,
                audit_event_id,
                event_type,
                payload,
                outbox_id,
                outbox_payload,
                run_id,
                action_set_id,
            )

    # 读取单个权威审计事件。
    def get_audit_event(self, audit_event_id: str) -> dict[str, Any] | None:
        return self._get_json_record(
            "audit_events", "audit_event_id", audit_event_id
        )

    # 返回尚未成功投影到文件日志的 outbox 项。
    def list_pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit 必须大于 0。")
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM audit_outbox
                WHERE delivered_at IS NULL
                ORDER BY created_at, outbox_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_decode_payload_row(row) for row in rows]

    # 幂等标记 outbox 已完成投影，只改变投递状态，不改写审计内容。
    def mark_outbox_delivered(
        self, outbox_id: str, delivered_at: str | None = None
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            row = self._require_row(
                connection, "audit_outbox", "outbox_id", outbox_id
            )
            if row["delivered_at"] is None:
                connection.execute(
                    "UPDATE audit_outbox SET delivered_at = ? WHERE outbox_id = ? AND delivered_at IS NULL",
                    (delivered_at or _utc_now(), outbox_id),
                )
                row = connection.execute(
                    "SELECT * FROM audit_outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
            return _decode_payload_row(row)

    # ActionSet publication 必须与自身运行和最终修订完全一致。
    def _apply_publication_action(
        self,
        connection: sqlite3.Connection,
        action_set_row: sqlite3.Row,
        action: Mapping[str, Any],
    ) -> dict[str, Any]:
        _require_expected_key(action, "expected_publication_id")
        run_id = _required_action_text(action, "run_id")
        revision_id = _required_action_text(action, "review_revision_id")
        if (
            run_id != action_set_row["run_id"]
            or revision_id != action_set_row["review_revision_id"]
        ):
            raise InvalidActionSetError(
                "publication 与 ActionSet 的 run_id 或 review_revision_id 不一致。"
            )
        return self._publish_review_in_transaction(
            connection,
            _required_action_text(action, "publication_id"),
            _required_action_text(action, "trade_date"),
            run_id,
            revision_id,
            _required_action_text(action, "report_hash"),
            _required_action_mapping(action, "payload"),
            _optional_action_text(action, "expected_publication_id"),
            action_set_row["action_set_id"],
        )

    # ActionSet 池动作强制携带预览时的期望状态事件 ID。
    def _apply_pool_action(
        self,
        connection: sqlite3.Connection,
        action_set_id: str,
        action: Mapping[str, Any],
    ) -> dict[str, Any]:
        _require_expected_key(action, "expected_event_id")
        return self._append_pool_status_event_in_transaction(
            connection,
            _required_action_text(action, "event_id"),
            _required_action_text(action, "pool_type"),
            _required_action_text(action, "code"),
            _required_action_text(action, "status"),
            _required_action_mapping(action, "payload"),
            _optional_action_text(action, "expected_event_id"),
            action_set_id,
        )

    # ActionSet 创建的 Observation 必须引用同一最终 RevisionBundle。
    def _apply_observation_create(
        self,
        connection: sqlite3.Connection,
        action_set_row: sqlite3.Row,
        action: Mapping[str, Any],
    ) -> dict[str, Any]:
        revision_id = _required_action_text(action, "review_revision_id")
        if revision_id != action_set_row["review_revision_id"]:
            raise InvalidActionSetError(
                "Observation 与 ActionSet 的 review_revision_id 不一致。"
            )
        return self._create_observation_in_transaction(
            connection,
            _required_action_text(action, "observation_id"),
            action_set_row["run_id"],
            revision_id,
            _required_action_text(action, "plan_item_id"),
            _required_action_mapping(action, "payload"),
            action_set_row["action_set_id"],
        )

    # ActionSet 结果动作使用与独立回填相同的结果头 CAS。
    def _apply_observation_result_action(
        self,
        connection: sqlite3.Connection,
        action_set_id: str,
        action: Mapping[str, Any],
    ) -> dict[str, Any]:
        _require_expected_key(action, "expected_result_revision_id")
        return self._append_observation_result_in_transaction(
            connection,
            _required_action_text(action, "observation_id"),
            _required_action_text(action, "result_revision_id"),
            _required_action_text(action, "status"),
            _required_action_mapping(action, "payload"),
            _optional_action_text(action, "expected_result_revision_id"),
            action_set_id,
        )

    # ActionSet 的审计和 outbox 与其它业务动作同事务提交。
    def _apply_audit_action(
        self,
        connection: sqlite3.Connection,
        action_set_row: sqlite3.Row,
        action: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._append_audit_event_in_transaction(
            connection,
            _required_action_text(action, "audit_event_id"),
            _required_action_text(action, "event_type"),
            _required_action_mapping(action, "payload"),
            _required_action_text(action, "outbox_id"),
            _required_action_mapping(action, "outbox_payload"),
            action_set_row["run_id"],
            action_set_row["action_set_id"],
        )

    # 同一 ActionSet 不允许重复修改同一可变聚合，避免动作顺序改变语义。
    def _validate_action_target_uniqueness(
        self,
        pool_actions: Sequence[Mapping[str, Any]],
        observation_creates: Sequence[Mapping[str, Any]],
        result_actions: Sequence[Mapping[str, Any]],
    ) -> None:
        pool_targets = [
            (
                _required_action_text(action, "pool_type"),
                _required_action_text(action, "code"),
            )
            for action in pool_actions
        ]
        observation_ids = [
            _required_action_text(action, "observation_id")
            for action in observation_creates
        ]
        result_targets = [
            _required_action_text(action, "observation_id")
            for action in result_actions
        ]
        if len(pool_targets) != len(set(pool_targets)):
            raise InvalidActionSetError("同一 ActionSet 不能重复修改同一池对象。")
        if len(observation_ids) != len(set(observation_ids)):
            raise InvalidActionSetError("同一 ActionSet 不能重复创建同一 Observation。")
        if len(result_targets) != len(set(result_targets)):
            raise InvalidActionSetError(
                "同一 ActionSet 不能重复回填同一 Observation。"
            )

    # 在当前事务内插入日报 publication 并原子切换唯一指针。
    def _publish_review_in_transaction(
        self,
        connection: sqlite3.Connection,
        publication_id: str,
        trade_date: str,
        run_id: str,
        review_revision_id: str,
        report_hash: str,
        payload: Mapping[str, Any],
        expected_publication_id: str | None,
        action_set_id: str | None,
    ) -> dict[str, Any]:
        run = self._require_row(connection, "review_runs", "run_id", run_id)
        if run["trade_date"] != trade_date:
            raise ReviewRunRepositoryError("ReviewRun 与 publication 的交易日不一致。")
        self._require_revision_run(connection, review_revision_id, run_id)
        if action_set_id is not None:
            self._require_row(
                connection, "action_sets", "action_set_id", action_set_id
            )
        current = connection.execute(
            "SELECT publication_id FROM current_review_publications WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
        actual_id = current["publication_id"] if current else None
        self._assert_expected(
            "日报 publication", trade_date, expected_publication_id, actual_id
        )
        record = self._save_json_record(
            connection,
            "review_publications",
            "publication_id",
            publication_id,
            {
                "trade_date": trade_date,
                "run_id": run_id,
                "review_revision_id": review_revision_id,
                "action_set_id": action_set_id,
                "supersedes_publication_id": expected_publication_id,
                "report_hash": _require_text(report_hash, "report_hash"),
            },
            payload,
        )
        self._switch_review_publication_pointer(
            connection,
            trade_date,
            publication_id,
            expected_publication_id,
        )
        return record

    # CAS 切换日报指针，SQL 条件再次防止未来多连接并发覆盖。
    def _switch_review_publication_pointer(
        self,
        connection: sqlite3.Connection,
        trade_date: str,
        publication_id: str,
        expected_publication_id: str | None,
    ) -> None:
        if expected_publication_id is None:
            connection.execute(
                "INSERT INTO current_review_publications (trade_date, publication_id) VALUES (?, ?)",
                (trade_date, publication_id),
            )
            return
        cursor = connection.execute(
            """
            UPDATE current_review_publications
            SET publication_id = ?
            WHERE trade_date = ? AND publication_id = ?
            """,
            (publication_id, trade_date, expected_publication_id),
        )
        if cursor.rowcount != 1:
            raise OptimisticConcurrencyError("日报 publication 指针在提交时已变化。")

    # 在当前事务内追加池状态事件并更新事件头。
    def _append_pool_status_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        pool_type: str,
        code: str,
        status: str,
        payload: Mapping[str, Any],
        expected_event_id: str | None,
        action_set_id: str | None,
    ) -> dict[str, Any]:
        for value, label in (
            (event_id, "event_id"),
            (pool_type, "pool_type"),
            (code, "code"),
            (status, "status"),
        ):
            _require_text(value, label)
        if action_set_id is not None:
            self._require_row(
                connection, "action_sets", "action_set_id", action_set_id
            )
        existing = connection.execute(
            "SELECT * FROM pool_status_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        payload_json, content_hash = _encode_payload(payload)
        if existing is not None:
            if (
                existing["pool_type"] == pool_type
                and existing["code"] == code
                and existing["status"] == status
                and existing["supersedes_event_id"] == expected_event_id
                and existing["action_set_id"] == action_set_id
                and existing["payload_json"] == payload_json
                and existing["content_hash"] == content_hash
            ):
                return _decode_payload_row(existing)
            raise ImmutableRecordError(f"池状态事件 {event_id} 已存在且内容不同。")
        current = connection.execute(
            """
            SELECT event_id FROM current_pool_status
            WHERE pool_type = ? AND code = ?
            """,
            (pool_type, code),
        ).fetchone()
        actual_id = current["event_id"] if current else None
        self._assert_expected(
            "池状态", f"{pool_type}:{code}", expected_event_id, actual_id
        )
        record = self._save_json_record(
            connection,
            "pool_status_events",
            "event_id",
            event_id,
            {
                "pool_type": pool_type,
                "code": code,
                "status": status,
                "supersedes_event_id": expected_event_id,
                "action_set_id": action_set_id,
            },
            payload,
        )
        if expected_event_id is None:
            connection.execute(
                "INSERT INTO current_pool_status (pool_type, code, event_id) VALUES (?, ?, ?)",
                (pool_type, code, event_id),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE current_pool_status SET event_id = ?
                WHERE pool_type = ? AND code = ? AND event_id = ?
                """,
                (event_id, pool_type, code, expected_event_id),
            )
            if cursor.rowcount != 1:
                raise OptimisticConcurrencyError("池状态事件头在提交时已变化。")
        return record

    # 在当前事务内创建 Observation，基础 payload 禁止混入结果字段。
    def _create_observation_in_transaction(
        self,
        connection: sqlite3.Connection,
        observation_id: str,
        run_id: str,
        review_revision_id: str,
        plan_item_id: str,
        payload: Mapping[str, Any],
        action_set_id: str | None,
    ) -> dict[str, Any]:
        forbidden_fields = {"status", "actual_result", "reason", "review_reason"}
        included_forbidden = forbidden_fields.intersection(payload)
        if included_forbidden:
            raise ReviewRunRepositoryError(
                "Observation 基础记录不能包含结果字段："
                + ", ".join(sorted(included_forbidden))
            )
        self._require_revision_run(connection, review_revision_id, run_id)
        if action_set_id is not None:
            self._require_row(
                connection, "action_sets", "action_set_id", action_set_id
            )
        return self._save_json_record(
            connection,
            "observations",
            "observation_id",
            observation_id,
            {
                "run_id": run_id,
                "review_revision_id": review_revision_id,
                "plan_item_id": _require_text(plan_item_id, "plan_item_id"),
                "action_set_id": action_set_id,
            },
            payload,
        )

    # 在当前事务内追加结果修订并切换 Observation 结果头。
    def _append_observation_result_in_transaction(
        self,
        connection: sqlite3.Connection,
        observation_id: str,
        result_revision_id: str,
        status: str,
        payload: Mapping[str, Any],
        expected_result_revision_id: str | None,
        action_set_id: str | None,
    ) -> dict[str, Any]:
        self._require_row(
            connection, "observations", "observation_id", observation_id
        )
        if status not in OBSERVATION_RESULT_STATUSES:
            raise ReviewRunRepositoryError(
                "Observation 结果状态必须是 hit、miss 或 invalid。"
            )
        if action_set_id is not None:
            self._require_row(
                connection, "action_sets", "action_set_id", action_set_id
            )
        payload_json, content_hash = _encode_payload(payload)
        existing = connection.execute(
            "SELECT * FROM observation_result_revisions WHERE result_revision_id = ?",
            (result_revision_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing["observation_id"] == observation_id
                and existing["status"] == status
                and existing["supersedes_result_revision_id"]
                == expected_result_revision_id
                and existing["action_set_id"] == action_set_id
                and existing["payload_json"] == payload_json
                and existing["content_hash"] == content_hash
            ):
                return _decode_payload_row(existing)
            raise ImmutableRecordError(
                f"Observation 结果修订 {result_revision_id} 已存在且内容不同。"
            )
        current = connection.execute(
            """
            SELECT result_revision_id FROM current_observation_results
            WHERE observation_id = ?
            """,
            (observation_id,),
        ).fetchone()
        actual_id = current["result_revision_id"] if current else None
        self._assert_expected(
            "Observation 结果",
            observation_id,
            expected_result_revision_id,
            actual_id,
        )
        record = self._save_json_record(
            connection,
            "observation_result_revisions",
            "result_revision_id",
            result_revision_id,
            {
                "observation_id": observation_id,
                "status": status,
                "supersedes_result_revision_id": expected_result_revision_id,
                "action_set_id": action_set_id,
            },
            payload,
        )
        if expected_result_revision_id is None:
            connection.execute(
                """
                INSERT INTO current_observation_results (observation_id, result_revision_id)
                VALUES (?, ?)
                """,
                (observation_id, result_revision_id),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE current_observation_results SET result_revision_id = ?
                WHERE observation_id = ? AND result_revision_id = ?
                """,
                (result_revision_id, observation_id, expected_result_revision_id),
            )
            if cursor.rowcount != 1:
                raise OptimisticConcurrencyError(
                    "Observation 结果事件头在提交时已变化。"
                )
        return record

    # 在当前事务内保存审计事件和 outbox；相同 ID 允许完全相同的幂等重放。
    def _append_audit_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        audit_event_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        outbox_id: str,
        outbox_payload: Mapping[str, Any],
        run_id: str | None,
        action_set_id: str | None,
    ) -> dict[str, Any]:
        if run_id is not None:
            self._require_row(connection, "review_runs", "run_id", run_id)
        if action_set_id is not None:
            self._require_row(
                connection, "action_sets", "action_set_id", action_set_id
            )
        audit_record = self._save_json_record(
            connection,
            "audit_events",
            "audit_event_id",
            audit_event_id,
            {
                "event_type": _require_text(event_type, "event_type"),
                "run_id": run_id,
                "action_set_id": action_set_id,
            },
            payload,
        )
        outbox_payload_json, outbox_content_hash = _encode_payload(outbox_payload)
        existing_outbox = connection.execute(
            "SELECT * FROM audit_outbox WHERE outbox_id = ?", (outbox_id,)
        ).fetchone()
        if existing_outbox is not None:
            if (
                existing_outbox["audit_event_id"] != audit_event_id
                or existing_outbox["payload_json"] != outbox_payload_json
                or existing_outbox["content_hash"] != outbox_content_hash
            ):
                raise ImmutableRecordError(
                    f"audit_outbox 的 {outbox_id} 已存在且内容不同。"
                )
            outbox_record = _decode_payload_row(existing_outbox)
        else:
            outbox_record = self._save_json_record(
                connection,
                "audit_outbox",
                "outbox_id",
                outbox_id,
                {"audit_event_id": audit_event_id, "delivered_at": None},
                outbox_payload,
            )
        return {
            **audit_record,
            "outbox_id": outbox_record["outbox_id"],
            "outbox_payload": outbox_record["payload"],
        }

    # 周报 current 指针同样使用 expected ID 做 SQL 级 CAS。
    def _switch_weekly_publication_pointer(
        self,
        connection: sqlite3.Connection,
        period_start: str,
        period_end: str,
        weekly_publication_id: str,
        expected_weekly_publication_id: str | None,
    ) -> None:
        if expected_weekly_publication_id is None:
            connection.execute(
                """
                INSERT INTO current_weekly_publications
                    (period_start, period_end, weekly_publication_id)
                VALUES (?, ?, ?)
                """,
                (period_start, period_end, weekly_publication_id),
            )
            return
        cursor = connection.execute(
            """
            UPDATE current_weekly_publications SET weekly_publication_id = ?
            WHERE period_start = ? AND period_end = ? AND weekly_publication_id = ?
            """,
            (
                weekly_publication_id,
                period_start,
                period_end,
                expected_weekly_publication_id,
            ),
        )
        if cursor.rowcount != 1:
            raise OptimisticConcurrencyError("周报 publication 指针在提交时已变化。")

    # 解码周报修订，并按稳定 ID 顺序返回三类固定引用。
    def _decode_weekly_revision(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        record = _decode_payload_row(row)
        weekly_revision_id = row["weekly_revision_id"]
        pin_queries = (
            (
                "review_publication_ids",
                "weekly_revision_review_publications",
                "publication_id",
            ),
            (
                "observation_result_revision_ids",
                "weekly_revision_observation_results",
                "result_revision_id",
            ),
            ("pool_status_event_ids", "weekly_revision_pool_events", "event_id"),
        )
        for output_key, table_name, value_column in pin_queries:
            rows = connection.execute(
                f"SELECT {value_column} FROM {table_name} WHERE weekly_revision_id = ? ORDER BY {value_column}",
                (weekly_revision_id,),
            ).fetchall()
            record[output_key] = [item[value_column] for item in rows]
        return record

    # 校验 manifest 属于指定 ReviewRun。
    def _require_manifest_run(
        self, connection: sqlite3.Connection, manifest_id: str, run_id: str
    ) -> sqlite3.Row:
        row = self._require_row(
            connection, "run_manifests", "manifest_id", manifest_id
        )
        if row["run_id"] != run_id:
            raise ReviewRunRepositoryError("RunManifest 不属于指定 ReviewRun。")
        return row

    # 校验 EvidenceBundle 属于指定 ReviewRun。
    def _require_evidence_bundle_run(
        self, connection: sqlite3.Connection, evidence_bundle_id: str, run_id: str
    ) -> sqlite3.Row:
        row = self._require_row(
            connection,
            "evidence_bundles",
            "evidence_bundle_id",
            evidence_bundle_id,
        )
        if row["run_id"] != run_id:
            raise ReviewRunRepositoryError("EvidenceBundle 不属于指定 ReviewRun。")
        return row

    # 校验 ReviewRevision 属于指定 ReviewRun。
    def _require_revision_run(
        self, connection: sqlite3.Connection, review_revision_id: str, run_id: str
    ) -> sqlite3.Row:
        row = self._require_row(
            connection,
            "review_revisions",
            "review_revision_id",
            review_revision_id,
        )
        if row["run_id"] != run_id:
            raise ReviewRunRepositoryError("ReviewRevision 不属于指定 ReviewRun。")
        return row

    # 读取必需父记录并给出稳定的业务异常。
    def _require_row(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        id_column: str,
        record_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            f"SELECT * FROM {table_name} WHERE {id_column} = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(
                f"{table_name} 中不存在 {id_column}={record_id} 的记录。"
            )
        return row

    # 所有不可变 JSON 记录使用同一规范序列化和完全相同重放规则。
    def _save_json_record(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        id_column: str,
        record_id: str,
        fields: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        _require_text(record_id, id_column)
        payload_json, content_hash = _encode_payload(payload)
        existing = connection.execute(
            f"SELECT * FROM {table_name} WHERE {id_column} = ?", (record_id,)
        ).fetchone()
        if existing is not None:
            if (
                existing["payload_json"] == payload_json
                and all(existing[key] == value for key, value in fields.items())
                and existing["content_hash"] == content_hash
            ):
                return _decode_payload_row(existing)
            raise ImmutableRecordError(
                f"{table_name} 的 {record_id} 已存在且内容不同。"
            )
        created_at = _utc_now()
        columns = [id_column, *fields, "payload_json", "content_hash", "created_at"]
        values = [
            record_id,
            *fields.values(),
            payload_json,
            content_hash,
            created_at,
        ]
        placeholders = ", ".join("?" for _ in columns)
        try:
            connection.execute(
                f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        except sqlite3.IntegrityError as error:
            raise ImmutableRecordError(
                f"无法保存 {table_name} 的 {record_id}：{error}"
            ) from error
        row = connection.execute(
            f"SELECT * FROM {table_name} WHERE {id_column} = ?", (record_id,)
        ).fetchone()
        return _decode_payload_row(row)

    # 通用读取接口只解码 payload，不改变存储内容。
    def _get_json_record(
        self, table_name: str, id_column: str, record_id: str
    ) -> dict[str, Any] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT * FROM {table_name} WHERE {id_column} = ?", (record_id,)
            ).fetchone()
        return _decode_payload_row(row) if row is not None else None

    # 比较用户预览时版本与事务开始后的权威版本。
    def _assert_expected(
        self,
        target_type: str,
        target_id: str,
        expected_id: str | None,
        actual_id: str | None,
    ) -> None:
        if expected_id != actual_id:
            raise OptimisticConcurrencyError(
                f"{target_type} {target_id} 版本冲突：期望 {expected_id!r}，实际 {actual_id!r}。"
            )

    # 数据库连接统一启用外键并返回命名列。
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    # 写操作统一使用 IMMEDIATE 事务，异常时回滚全部可见变化。
    @contextmanager
    def _transaction(
        self,
        *,
        failure_injector: Callable[[str], None] | None = None,
    ) -> Iterator[sqlite3.Connection]:
        if self._transaction_connection is not None:
            if not self._transaction_connection.in_transaction:
                raise ReviewRunRepositoryError("外部 SQLite 连接必须处于显式事务中。")
            yield self._transaction_connection
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _inject_failure(failure_injector, "after_transaction_begin")
            yield connection
            _inject_failure(failure_injector, "before_transaction_commit")
            connection.commit()
            _inject_failure(failure_injector, "after_transaction_commit")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    # 读取不存在的数据库时直接报错，避免查询动作意外创建空库。
    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        if not self.database_path.exists():
            raise RecordNotFoundError(f"数据库不存在：{self.database_path}")
        if self._transaction_connection is not None:
            yield self._transaction_connection
            return
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    # 数据库触发器阻止绕过仓储直接改写或删除不可变业务记录。
    def _create_immutable_triggers(self, connection: sqlite3.Connection) -> None:
        for table_name in _IMMUTABLE_TABLES:
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table_name}_reject_update
                BEFORE UPDATE ON {table_name}
                BEGIN
                    SELECT RAISE(ABORT, '{table_name} 是不可变记录，禁止更新');
                END
                """
            )
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table_name}_reject_delete
                BEFORE DELETE ON {table_name}
                BEGIN
                    SELECT RAISE(ABORT, '{table_name} 是不可变记录，禁止删除');
                END
                """
            )

    # 新仓储不能把旧 Observation 表静默当成新版结构，必须由后续显式迁移承接。
    def _ensure_schema_target(self, connection: sqlite3.Connection) -> None:
        table_names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "observations" not in table_names or "review_runs" in table_names:
            return
        observation_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(observations)")
        }
        new_columns = {"run_id", "review_revision_id", "plan_item_id", "payload_json"}
        if not new_columns.issubset(observation_columns):
            raise ReviewRunRepositoryError(
                "检测到未经迁移的旧 observations 表；请先执行显式 ReviewRun 迁移。"
            )


# executescript 会隐式提交现有事务；逐条执行保证迁移 DDL 与登记记录共同回滚。
def _execute_sql_script(connection: sqlite3.Connection, script: str) -> None:
    statement_lines: list[str] = []
    for line in script.splitlines():
        statement_lines.append(line)
        statement = "\n".join(statement_lines).strip()
        if statement and sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement_lines.clear()
    if any(line.strip() for line in statement_lines):
        raise ReviewRunRepositoryError("ReviewRun schema SQL 包含未完成语句。")


# 结构门禁从正式建表 SQL 和不可变触发器生成参考库，避免维护第二份易漂移清单。
def review_run_schema_table_names() -> tuple[str, ...]:
    contract = _reference_review_run_schema_contract()
    return tuple(contract["tables"])


def review_run_schema_contract_issues(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    expected = _reference_review_run_schema_contract()
    actual_table_names = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
    }
    issues: list[str] = []
    for table_name, expected_table in expected["tables"].items():
        if table_name not in actual_table_names:
            issues.append(f"缺少表 {table_name}")
            continue
        actual_table = _read_table_schema_contract(connection, table_name)
        if actual_table["columns"] != expected_table["columns"]:
            issues.append(f"{table_name} 列定义不匹配")
        if actual_table["table_sql"] != expected_table["table_sql"]:
            issues.append(f"{table_name} 建表约束不匹配")
        if actual_table["foreign_keys"] != expected_table["foreign_keys"]:
            issues.append(f"{table_name} 外键定义不匹配")
        missing_indexes = tuple(
            item
            for item in expected_table["indexes"]
            if item not in actual_table["indexes"]
        )
        if missing_indexes:
            labels = ", ".join(
                str(item[0] or f"{item[2]} constraint") for item in missing_indexes
            )
            issues.append(f"{table_name} 缺少必需索引：{labels}")

    actual_triggers = _read_trigger_contract(connection)
    for trigger_name, expected_sql in expected["triggers"].items():
        if actual_triggers.get(trigger_name) != expected_sql:
            issues.append(f"不可变触发器 {trigger_name} 缺失或定义不匹配")
    return tuple(issues)


def _reference_review_run_schema_contract() -> dict[str, Any]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        _execute_sql_script(connection, _SCHEMA_SQL)
        ReviewRunRepository(Path(":memory:"))._create_immutable_triggers(connection)
        table_names = tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY rowid
                """
            )
        )
        return {
            "tables": {
                table_name: _read_table_schema_contract(connection, table_name)
                for table_name in table_names
            },
            "triggers": _read_trigger_contract(connection),
        }
    finally:
        connection.close()


def _read_table_schema_contract(
    connection: sqlite3.Connection,
    table_name: str,
) -> dict[str, Any]:
    quoted_table = _quote_identifier(table_name)
    table_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    table_sql = " ".join(str(table_row[0] or "").split()).casefold()
    columns = tuple(
        (
            str(row[1]),
            str(row[2]).strip().upper(),
            int(row[3]),
            row[4],
            int(row[5]),
        )
        for row in connection.execute(f"PRAGMA table_info({quoted_table})")
    )
    foreign_keys = tuple(
        tuple(row)
        for row in connection.execute(f"PRAGMA foreign_key_list({quoted_table})")
    )
    indexes = []
    for index_row in connection.execute(f"PRAGMA index_list({quoted_table})"):
        index_name = str(index_row[1])
        origin = str(index_row[3])
        index_columns = tuple(
            tuple(row)
            for row in connection.execute(
                f"PRAGMA index_xinfo({_quote_identifier(index_name)})"
            )
        )
        indexes.append(
            (
                index_name if origin == "c" else None,
                int(index_row[2]),
                origin,
                int(index_row[4]),
                index_columns,
            )
        )
    return {
        "table_sql": table_sql,
        "columns": columns,
        "foreign_keys": foreign_keys,
        "indexes": tuple(sorted(indexes, key=repr)),
    }


def _read_trigger_contract(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): " ".join(str(row[1] or "").split())
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        )
    }


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


# 规范 JSON 禁止 NaN，并用稳定排序生成内容哈希。
def _encode_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload 必须是 mapping。")
    try:
        payload_json = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ReviewRunRepositoryError(f"payload 不能规范序列化为 JSON：{error}") from error
    content_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return payload_json, content_hash


# 将 SQLite 行还原为普通 mapping，并把 payload_json 解码为 payload。
def _decode_payload_row(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    payload_json = record.pop("payload_json")
    record["payload"] = json.loads(payload_json)
    return record


# 周报快照把 Observation 基础记录与同一事务内读取的结果头组合为冻结投影。
def _decode_weekly_observation_row(row: sqlite3.Row) -> dict[str, Any]:
    current_result = {
        "observation_id": row["observation_id"],
        "result_revision_id": None,
        "status": "pending",
        "payload": {},
        "content_hash": None,
        "created_at": None,
    }
    if row["current_result_revision_id"] is not None:
        current_result = {
            "result_revision_id": row["current_result_revision_id"],
            "observation_id": row["observation_id"],
            "status": row["current_result_status"],
            "supersedes_result_revision_id": row[
                "current_result_supersedes_revision_id"
            ],
            "action_set_id": row["current_result_action_set_id"],
            "payload": json.loads(row["current_result_payload_json"]),
            "content_hash": row["current_result_content_hash"],
            "created_at": row["current_result_created_at"],
        }
    return {
        "observation_id": row["observation_id"],
        "run_id": row["run_id"],
        "review_date": row["review_date"],
        "review_revision_id": row["review_revision_id"],
        "plan_item_id": row["plan_item_id"],
        "action_set_id": row["action_set_id"],
        "payload": json.loads(row["payload_json"]),
        "content_hash": row["content_hash"],
        "created_at": row["created_at"],
        "current_result": current_result,
    }


# 所有稳定 ID 和必要文本禁止空字符串。
def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewRunRepositoryError(f"{label} 必须是非空字符串。")
    return value


# ActionSet 必要文本错误归类为无效提案，而不是底层 SQLite 错误。
def _required_action_text(action: Mapping[str, Any], key: str) -> str:
    try:
        return _require_text(action.get(key), key)
    except ReviewRunRepositoryError as error:
        raise InvalidActionSetError(str(error)) from error


# ActionSet 可空期望 ID 只允许 None 或非空字符串。
def _optional_action_text(action: Mapping[str, Any], key: str) -> str | None:
    value = action.get(key)
    if value is None:
        return None
    return _required_action_text(action, key)


# 可变目标必须显式携带 expected 键，即使首次写入时值为 None。
def _require_expected_key(action: Mapping[str, Any], key: str) -> None:
    if key not in action:
        raise InvalidActionSetError(f"ActionSet 动作缺少 {key}。")


# 读取 ActionSet 中必需的对象字段。
def _required_action_mapping(
    action: Mapping[str, Any], key: str
) -> Mapping[str, Any]:
    value = action.get(key)
    if not isinstance(value, Mapping):
        raise InvalidActionSetError(f"ActionSet 的 {key} 必须是 JSON 对象。")
    return value


# 读取 ActionSet 中允许为 null 的对象字段。
def _optional_action_mapping(
    action: Mapping[str, Any], key: str
) -> Mapping[str, Any] | None:
    value = action.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidActionSetError(f"ActionSet 的 {key} 必须是 JSON 对象或 null。")
    return value


# 读取 ActionSet 动作列表，并拒绝字符串等伪序列。
def _action_mapping_list(
    payload: Mapping[str, Any], key: str
) -> list[Mapping[str, Any]]:
    value = payload.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise InvalidActionSetError(f"ActionSet 的 {key} 必须是 JSON 对象数组。")
    return value


# 周报固定引用去重排序，确保相同集合生成稳定记录。
def _unique_text_values(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ReviewRunRepositoryError(f"{label} 必须是字符串序列。")
    normalized = tuple(sorted({_require_text(value, label) for value in values}))
    return normalized


# 时间统一使用带 UTC 时区的微秒 ISO 文本，避免同事务多条记录排序不稳定。
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
