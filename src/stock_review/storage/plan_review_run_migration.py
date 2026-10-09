# 本文件生成只读 ReviewRun 迁移计划，并在计划哈希获得显式确认后迁移旧数据登记。

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from stock_review.review_runs.canonical_hash import canonical_hash
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.storage.review_run_repository import (
    ReviewRunRepository,
    SCHEMA_VERSION,
    review_run_schema_contract_issues,
    review_run_schema_table_names,
)


LEGACY_TABLES = (
    "evidence_snapshots",
    "pool_items",
    "pool_item_sectors",
    "pool_item_status_history",
    "manual_step_records",
    "manual_previews",
    "manual_final_responses",
    "manual_final_pool_links",
    "manual_final_preview_links",
    "manual_final_observation_links",
    "observations",
    "legacy_observations_v0",
    "observation_reviews",
)
TARGET_TABLES = review_run_schema_table_names()
SAMPLE_DATES = {"2026-07-06", "2026-07-07", "2026-07-10"}
DATABASE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


@dataclass(frozen=True)
class MigrationPlan:
    plan_id: str
    environment: str
    database_path: str
    database_sha256: str
    current_schema_version: int
    target_schema_version: int
    backup_path: str
    rename_legacy_observations: bool
    table_counts: Mapping[str, int]
    table_hashes: Mapping[str, str]
    artifact_entries: tuple[Mapping[str, Any], ...]
    evidence_dir: str
    daily_report_dir: str
    weekly_report_dir: str
    target_tables: tuple[str, ...]
    estimated_legacy_import_count: int
    readback_sql: tuple[str, ...]
    failure_recovery_steps: tuple[str, ...]
    created_at: str
    content_hash: str

    @classmethod
    def create(cls, **values: Any) -> MigrationPlan:
        plan = cls(**values, content_hash="")
        return replace(plan, content_hash=plan.calculate_hash())

    def calculate_hash(self) -> str:
        return canonical_hash(self._hash_payload())

    def validate_hash(self) -> None:
        if self.content_hash != self.calculate_hash():
            raise ReviewRunDomainError("数据库迁移计划内容与 plan hash 不一致。")

    def to_dict(self) -> dict[str, Any]:
        return {**self._hash_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MigrationPlan:
        plan = cls(
            plan_id=str(value.get("plan_id") or ""),
            environment=str(value.get("environment") or ""),
            database_path=str(value.get("database_path") or ""),
            database_sha256=str(value.get("database_sha256") or ""),
            current_schema_version=int(value.get("current_schema_version", 0)),
            target_schema_version=int(value.get("target_schema_version", 0)),
            backup_path=str(value.get("backup_path") or ""),
            rename_legacy_observations=bool(value.get("rename_legacy_observations", False)),
            table_counts={str(key): int(count) for key, count in _mapping(value.get("table_counts"), "table_counts").items()},
            table_hashes={str(key): str(item_hash) for key, item_hash in _mapping(value.get("table_hashes"), "table_hashes").items()},
            artifact_entries=tuple(_mapping(item, "artifact entry") for item in value.get("artifact_entries", ())),
            evidence_dir=str(value.get("evidence_dir") or ""),
            daily_report_dir=str(value.get("daily_report_dir") or ""),
            weekly_report_dir=str(value.get("weekly_report_dir") or ""),
            target_tables=tuple(str(item) for item in value.get("target_tables", ())),
            estimated_legacy_import_count=int(value.get("estimated_legacy_import_count", 0)),
            readback_sql=tuple(str(item) for item in value.get("readback_sql", ())),
            failure_recovery_steps=tuple(str(item) for item in value.get("failure_recovery_steps", ())),
            created_at=str(value.get("created_at") or ""),
            content_hash=str(value.get("content_hash") or ""),
        )
        plan.validate_hash()
        return plan

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "environment": self.environment,
            "database_path": self.database_path,
            "database_sha256": self.database_sha256,
            "current_schema_version": self.current_schema_version,
            "target_schema_version": self.target_schema_version,
            "backup_path": self.backup_path,
            "rename_legacy_observations": self.rename_legacy_observations,
            "table_counts": dict(self.table_counts),
            "table_hashes": dict(self.table_hashes),
            "artifact_entries": [dict(item) for item in self.artifact_entries],
            "evidence_dir": self.evidence_dir,
            "daily_report_dir": self.daily_report_dir,
            "weekly_report_dir": self.weekly_report_dir,
            "target_tables": list(self.target_tables),
            "estimated_legacy_import_count": self.estimated_legacy_import_count,
            "readback_sql": list(self.readback_sql),
            "failure_recovery_steps": list(self.failure_recovery_steps),
            "created_at": self.created_at,
        }


# plan-migration 全程以只读连接和文件哈希盘点现状，不创建备份或数据库对象。
def plan_review_run_migration(
    database_path: Path,
    target_version: int,
    backup_path: Path,
    *,
    evidence_dir: Path = Path("data") / "evidence",
    daily_report_dir: Path = Path("reports") / "daily",
    weekly_report_dir: Path = Path("reports") / "weekly",
    now: datetime | None = None,
) -> MigrationPlan:
    database = Path(database_path).resolve()
    backup = Path(backup_path).resolve()
    if not database.is_file():
        raise ReviewRunDomainError(f"迁移目标数据库不存在：{database}")
    _require_distinct_database_backup(database, backup)
    if target_version != SCHEMA_VERSION:
        raise ReviewRunDomainError(f"当前程序只支持迁移到 schema version {SCHEMA_VERSION}。")
    connection = _read_only_connection(database)
    try:
        # 一个只读事务冻结计划所见的表、记录与 WAL 页，避免逐项盘点跨越多个数据库状态。
        connection.execute("BEGIN")
        table_names = _table_names(connection)
        current_version = _current_schema_version(connection, table_names)
        if current_version > target_version:
            raise ReviewRunDomainError("目标 schema version 低于数据库当前版本。")
        legacy_tables = (
            ()
            if current_version == target_version
            else _present_legacy_tables(connection, table_names)
        )
        counts = {name: _table_count(connection, name) for name in legacy_tables}
        hashes = {name: canonical_hash(_table_rows(connection, name)) for name in legacy_tables}
        rename_observations = "observations" in legacy_tables and _is_legacy_observations(connection)
        database_hash = _database_snapshot_sha256(connection)
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    evidence_root = Path(evidence_dir).resolve()
    daily_report_root = Path(daily_report_dir).resolve()
    weekly_report_root = Path(weekly_report_dir).resolve()
    artifacts = (
        ()
        if current_version == target_version
        else _inventory_artifacts(
            evidence_root,
            daily_report_root,
            weekly_report_root,
        )
    )
    timestamp = _timestamp(now)
    identity = canonical_hash(
        {
            "database_path": str(database),
            "database_sha256": database_hash,
            "target_version": target_version,
            "backup_path": str(backup),
            "created_at": timestamp,
        }
    )
    return MigrationPlan.create(
        plan_id=f"MIGRATION-{identity[:24]}",
        environment="local",
        database_path=str(database),
        database_sha256=database_hash,
        current_schema_version=current_version,
        target_schema_version=target_version,
        backup_path=str(backup),
        rename_legacy_observations=rename_observations,
        table_counts=counts,
        table_hashes=hashes,
        artifact_entries=artifacts,
        evidence_dir=str(evidence_root),
        daily_report_dir=str(daily_report_root),
        weekly_report_dir=str(weekly_report_root),
        target_tables=TARGET_TABLES,
        estimated_legacy_import_count=sum(counts.values()) + len(artifacts),
        readback_sql=(
            "SELECT version, description, applied_at FROM schema_versions ORDER BY version",
            "SELECT source_kind, classification, COUNT(*) FROM legacy_imports GROUP BY source_kind, classification ORDER BY source_kind, classification",
            "PRAGMA foreign_key_check",
            "SELECT COUNT(*) FROM review_runs",
            "SELECT COUNT(*) FROM current_review_publications",
        ),
        failure_recovery_steps=(
            "停止其它数据库写连接，保留 SQLite 未提交事务的恢复文件。",
            "重新执行同一已确认计划，让 SQLite 先完成原子回滚再安全重放。",
            f"若完整性回查失败，停止自动重试并人工核对备份 {backup}。",
        ),
        created_at=timestamp,
    )


# migrate 只接受完整计划对象和匹配哈希；最终校验和全部写入共享一个单写者事务。
def migrate_review_run_database(
    plan: MigrationPlan,
    plan_hash: str,
    *,
    confirmed: bool,
) -> dict[str, Any]:
    plan.validate_hash()
    if not confirmed:
        raise ReviewRunDomainError("database migrate 必须显式提供 --confirmed。")
    if plan_hash != plan.content_hash:
        raise ReviewRunDomainError("database migrate 提供的 plan hash 与计划内容不一致。")
    if plan.environment != "local" or plan.target_schema_version != SCHEMA_VERSION:
        raise ReviewRunDomainError("迁移计划的环境或目标 schema version 不受支持。")
    database = Path(plan.database_path).resolve()
    backup = Path(plan.backup_path).resolve()
    if not database.is_file():
        raise ReviewRunDomainError("迁移计划绑定的数据库或备份路径无效。")
    _require_distinct_database_backup(database, backup)

    connection = _begin_migration_transaction(database)
    try:
        if _migration_was_applied(connection, plan.content_hash):
            _require_current_migrated_schema(connection, plan.target_schema_version)
            _require_replay_backup(plan, backup)
            connection.rollback()
            return _migration_result(plan, replayed=True)

        # 写锁取得后重新校验计划，迁移期间其它连接无法提交新记录。
        _verify_database_connection_matches_plan(
            plan,
            connection,
            mismatch_message="目标数据库自 plan-migration 后已经变化，必须重新生成计划。",
        )
        _verify_artifact_entries(plan)
        _prepare_migration_backup(plan, database, backup)
        if plan.rename_legacy_observations:
            _rename_legacy_observations(connection)
        repository = ReviewRunRepository(database, transaction_connection=connection)
        repository.initialize_schema()
        imported = _register_legacy_rows(repository, connection, plan)
        imported += _register_legacy_files(repository, plan)
        repository.register_legacy_import(
            "migration_plan",
            plan.content_hash,
            "applied",
            {
                "plan_id": plan.plan_id,
                "plan_hash": plan.content_hash,
                "source_database_sha256": plan.database_sha256,
                "target_schema_version": plan.target_schema_version,
                "legacy_import_count": imported,
                "backup_path": str(backup),
                "backup_snapshot_sha256": plan.database_sha256,
            },
        )
        audit_suffix = plan.content_hash[:24]
        repository.append_audit_event(
            audit_event_id=f"AUDIT-MIGRATION-{audit_suffix}",
            event_type="database migrate",
            payload={
                "command": "database migrate",
                "plan_id": plan.plan_id,
                "plan_hash": plan.content_hash,
                "database_path": str(database),
                "backup_path": str(backup),
                "backup_snapshot_sha256": plan.database_sha256,
                "target_schema_version": plan.target_schema_version,
                "legacy_import_count": imported,
                "status": "completed",
            },
            outbox_id=f"OUTBOX-MIGRATION-{audit_suffix}",
            outbox_payload={
                "command": "database migrate",
                "plan_id": plan.plan_id,
                "plan_hash": plan.content_hash,
                "status": "completed",
            },
        )
        _verify_migration_result(repository, connection, plan, imported)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return _migration_result(plan, replayed=False)


def _migration_result(plan: MigrationPlan, *, replayed: bool) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "plan_hash": plan.content_hash,
        "schema_version": plan.target_schema_version,
        "backup_path": str(Path(plan.backup_path).resolve()),
        "backup_snapshot_sha256": plan.database_sha256,
        "legacy_import_count": plan.estimated_legacy_import_count,
        "replayed": replayed,
    }


def _begin_migration_transaction(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, timeout=0.1)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        # IMMEDIATE 在 WAL 与 rollback journal 下都阻止其它连接提交写事务，同时允许只读回查。
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as error:
        connection.close()
        raise ReviewRunDomainError(
            "迁移无法取得目标数据库的离线写入门禁；请停止其它写连接后重试。"
        ) from error
    return connection


def _require_distinct_database_backup(database: Path, backup: Path) -> None:
    database = Path(database).resolve()
    backup = Path(backup).resolve()
    sidecar_paths = {
        Path(f"{database}{suffix}").resolve()
        for suffix in DATABASE_SIDECAR_SUFFIXES
    }
    if backup in sidecar_paths:
        raise ReviewRunDomainError("备份路径不能占用目标数据库的 journal、wal 或 shm sidecar。")
    if database == backup:
        raise ReviewRunDomainError("备份路径不能与迁移目标数据库相同。")
    if not backup.exists():
        return
    try:
        same_file = os.path.samefile(database, backup)
    except OSError as error:
        raise ReviewRunDomainError("无法确认迁移目标数据库与备份是否为不同文件。") from error
    if same_file:
        raise ReviewRunDomainError("备份路径与迁移目标数据库指向同一文件。")


def _require_current_migrated_schema(
    connection: sqlite3.Connection,
    target_version: int,
) -> None:
    table_names = _table_names(connection)
    actual_version = _current_schema_version(connection, table_names)
    if actual_version != target_version:
        raise ReviewRunDomainError(
            f"已迁移数据库的实际 schema version 为 {actual_version}，"
            f"不等于计划目标版本 {target_version}。"
        )
    issues = review_run_schema_contract_issues(connection)
    if issues:
        raise ReviewRunDomainError(
            "已迁移数据库的 ReviewRun 结构合同不完整：" + "；".join(issues)
        )


def _require_replay_backup(plan: MigrationPlan, backup: Path) -> None:
    if not backup.is_file():
        raise ReviewRunDomainError("迁移已经应用，但计划绑定的备份文件不存在。")
    _verify_database_matches_plan(
        plan,
        backup,
        mismatch_message="迁移已经应用，但备份逻辑快照与原计划不一致。",
    )


def _verify_artifact_entries(plan: MigrationPlan) -> None:
    if plan.current_schema_version == plan.target_schema_version:
        if plan.artifact_entries:
            raise ReviewRunDomainError("已是目标 schema 的迁移计划不得登记 legacy 产物。")
        return
    current_entries = _inventory_artifacts(
        Path(plan.evidence_dir),
        Path(plan.daily_report_dir),
        Path(plan.weekly_report_dir),
    )
    if canonical_hash(current_entries) != canonical_hash(plan.artifact_entries):
        raise ReviewRunDomainError(
            "历史产物清单自 plan-migration 后已经变化，必须重新生成计划。"
        )


def _prepare_migration_backup(
    plan: MigrationPlan,
    database: Path,
    backup: Path,
) -> None:
    _require_distinct_database_backup(database, backup)
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        _verify_database_matches_plan(
            plan,
            backup,
            mismatch_message="备份路径已存在不同数据库快照，不能覆盖。",
        )
        _require_distinct_database_backup(database, backup)
        return
    _write_consistent_database_snapshot(database, backup)
    try:
        _require_distinct_database_backup(database, backup)
        _verify_database_matches_plan(
            plan,
            backup,
            mismatch_message="迁移前备份与已确认的数据库快照不一致。",
        )
        _require_distinct_database_backup(database, backup)
    finally:
        # 新快照已 checkpoint 到主文件，校验连接可能生成的空 sidecar 不属于备份内容。
        _remove_database_sidecars(backup)


# 新版命令只接受已迁移且结构完整的数据库；start 仅可额外接收不存在或无表的新库。
def require_review_run_schema(
    database_path: Path,
    *,
    allow_empty: bool = False,
) -> None:
    database = Path(database_path)
    if not database.is_file():
        if allow_empty and not database.exists():
            return
        raise ReviewRunDomainError(f"ReviewRun 数据库不存在：{database}")
    connection: sqlite3.Connection | None = None
    try:
        connection = _read_only_connection(database)
        table_names = _table_names(connection)
        if allow_empty and not table_names:
            return
        if "schema_versions" not in table_names:
            raise ReviewRunDomainError(
                f"数据库尚未迁移到 ReviewRun schema version {SCHEMA_VERSION}；"
                "请先执行 database plan-migration 并确认迁移。"
            )
        current_version = _current_schema_version(connection, table_names)
        if current_version < SCHEMA_VERSION:
            raise ReviewRunDomainError(
                f"数据库尚未迁移到 ReviewRun schema version {SCHEMA_VERSION}；"
                "请先执行 database plan-migration 并确认迁移。"
            )
        if current_version > SCHEMA_VERSION:
            raise ReviewRunDomainError(
                f"数据库 schema version {current_version} 高于当前支持版本 {SCHEMA_VERSION}。"
            )
        contract_issues = review_run_schema_contract_issues(connection)
        if contract_issues:
            raise ReviewRunDomainError(
                "ReviewRun 数据库结构不完整：" + "；".join(contract_issues)
            )
    except ReviewRunDomainError:
        raise
    except sqlite3.Error as error:
        raise ReviewRunDomainError(f"无法读取 ReviewRun 数据库结构：{error}") from error
    finally:
        if connection is not None:
            connection.close()


# 表内容校验和快照哈希在同一个只读事务内完成，WAL checkpoint 不会造成误报。
def _verify_database_matches_plan(
    plan: MigrationPlan,
    database: Path,
    *,
    mismatch_message: str,
) -> None:
    connection: sqlite3.Connection | None = None
    try:
        connection = _read_only_connection(database)
        connection.execute("BEGIN")
        _verify_database_connection_matches_plan(
            plan,
            connection,
            mismatch_message=mismatch_message,
        )
    except ReviewRunDomainError:
        raise
    except sqlite3.Error as error:
        raise ReviewRunDomainError(f"{mismatch_message} SQLite 快照无法读取：{error}") from error
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()


def _verify_database_connection_matches_plan(
    plan: MigrationPlan,
    connection: sqlite3.Connection,
    *,
    mismatch_message: str,
) -> None:
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        # 已达目标版本后的新计划只校验完整数据库快照，不把保留的旧表再次登记为 legacy。
        tables = (
            ()
            if plan.current_schema_version == plan.target_schema_version
            else _present_legacy_tables(connection, _table_names(connection))
        )
        counts = {name: _table_count(connection, name) for name in tables}
        hashes = {name: canonical_hash(_table_rows(connection, name)) for name in tables}
        database_hash = _database_snapshot_sha256(connection)
    except ReviewRunDomainError:
        raise
    except sqlite3.Error as error:
        raise ReviewRunDomainError(f"{mismatch_message} SQLite 快照无法读取：{error}") from error
    if len(integrity) != 1 or str(integrity[0][0]).lower() != "ok":
        raise ReviewRunDomainError(f"{mismatch_message} SQLite 完整性检查未通过。")
    if (
        database_hash != plan.database_sha256
        or counts != dict(plan.table_counts)
        or hashes != dict(plan.table_hashes)
    ):
        raise ReviewRunDomainError(mismatch_message)


# SQLite 在线备份读取 pager 视图，会把已提交但尚未 checkpoint 的 WAL 页纳入同一快照。
def _write_consistent_database_snapshot(source_path: Path, target_path: Path) -> None:
    target = Path(target_path)
    _require_distinct_database_backup(source_path, target)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    source = _read_only_connection(source_path)
    destination: sqlite3.Connection | None = None
    try:
        journal_mode = str(source.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        destination = sqlite3.connect(temporary)
        if journal_mode == "wal":
            destination.execute("PRAGMA journal_mode = WAL")
        source.backup(destination)
        if journal_mode == "wal":
            checkpoint = destination.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise ReviewRunDomainError("迁移前数据库快照无法完成 WAL checkpoint。")
        integrity = destination.execute("PRAGMA integrity_check").fetchall()
        if len(integrity) != 1 or str(integrity[0][0]).lower() != "ok":
            raise ReviewRunDomainError("迁移前数据库快照完整性检查未通过。")
        destination.close()
        destination = None
        source.close()
        source = None
        _remove_database_sidecars(temporary)
        if target.exists():
            raise ReviewRunDomainError("备份路径在创建快照期间已被占用，不能覆盖。")
        try:
            # 同目录硬链接发布具备 no-clobber 语义，竞争者先占路径时绝不覆盖其文件。
            os.link(temporary, target)
        except FileExistsError as error:
            raise ReviewRunDomainError(
                "备份路径在创建快照期间已被占用，不能覆盖。"
            ) from error
        except OSError as error:
            raise ReviewRunDomainError("一致性快照无法原子发布到备份路径。") from error
        _require_distinct_database_backup(source_path, target)
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        _remove_database_sidecars(temporary)
        temporary.unlink(missing_ok=True)


def _remove_database_sidecars(database_path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{database_path}{suffix}").unlink(missing_ok=True)


def _rename_legacy_observations(connection: sqlite3.Connection) -> None:
    names = _table_names(connection)
    if "legacy_observations_v0" in names:
        return
    if "observations" not in names or not _is_legacy_observations(connection):
        raise ReviewRunDomainError("迁移计划要求重命名旧 observations，但实际结构不匹配。")
    connection.execute("ALTER TABLE observations RENAME TO legacy_observations_v0")


def _register_legacy_rows(
    repository: ReviewRunRepository,
    connection: sqlite3.Connection,
    plan: MigrationPlan,
) -> int:
    imported = 0
    names = _table_names(connection)
    for planned_name, expected_count in plan.table_counts.items():
        actual_name = "legacy_observations_v0" if planned_name == "observations" and plan.rename_legacy_observations else planned_name
        if actual_name not in names:
            raise ReviewRunDomainError(f"迁移时缺少旧表：{actual_name}")
        rows = _table_rows(connection, actual_name)
        if len(rows) != expected_count or canonical_hash(rows) != plan.table_hashes[planned_name]:
            raise ReviewRunDomainError(f"迁移时旧表校验失败：{planned_name}")
        for position, row in enumerate(rows, start=1):
            repository.register_legacy_import(
                f"table:{planned_name}",
                _legacy_row_key(connection, actual_name, row, position),
                _classify_legacy_row(planned_name, row),
                {"table": planned_name, "row": row},
            )
            imported += 1
    return imported


def _register_legacy_files(repository: ReviewRunRepository, plan: MigrationPlan) -> int:
    for entry in plan.artifact_entries:
        repository.register_legacy_import(
            str(entry["source_kind"]),
            str(entry["path"]),
            str(entry["classification"]),
            dict(entry),
        )
    return len(plan.artifact_entries)


def _verify_migration_result(
    repository: ReviewRunRepository,
    connection: sqlite3.Connection,
    plan: MigrationPlan,
    imported: int,
) -> None:
    versions = repository.list_schema_versions()
    if not versions or versions[-1]["version"] != plan.target_schema_version:
        raise ReviewRunDomainError("迁移后 schema version 回查失败。")
    contract_issues = review_run_schema_contract_issues(connection)
    if contract_issues:
        raise ReviewRunDomainError(
            "迁移后 ReviewRun 数据库结构合同校验失败："
            + "；".join(contract_issues)
        )
    if imported != plan.estimated_legacy_import_count:
        raise ReviewRunDomainError("迁移后的 legacy_import 数量与计划不一致。")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise ReviewRunDomainError("迁移后外键回查失败。")


def _migration_was_applied(connection: sqlite3.Connection, plan_hash: str) -> bool:
    if "legacy_imports" not in _table_names(connection):
        return False
    row = connection.execute(
        "SELECT 1 FROM legacy_imports WHERE source_kind = 'migration_plan' AND source_key = ?",
        (plan_hash,),
    ).fetchone()
    return row is not None


def _inventory_artifacts(
    evidence_dir: Path,
    daily_report_dir: Path,
    weekly_report_dir: Path,
) -> tuple[Mapping[str, Any], ...]:
    entries: list[Mapping[str, Any]] = []
    for source_kind, root in (
        ("evidence_file", Path(evidence_dir)),
        ("daily_report", Path(daily_report_dir)),
        ("weekly_report", Path(weekly_report_dir)),
    ):
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            entries.append(
                {
                    "source_kind": source_kind,
                    "path": str(path.resolve()),
                    "classification": _classify_file(source_kind, path, root),
                    "sha256": _file_sha256(path),
                    "size": path.stat().st_size,
                }
            )
    return tuple(entries)


def _classify_file(source_kind: str, path: Path, root: Path) -> str:
    lowered = path.name.lower()
    relative_parts = tuple(
        part.lower() for part in path.relative_to(root).parts
    )
    if any("experimental" in part for part in relative_parts):
        return "experimental"
    if any(
        "superseded" in part or "revised" in part
        for part in relative_parts
    ):
        return "superseded"
    if any(lowered.startswith(date) for date in SAMPLE_DATES):
        return "sample"
    if source_kind == "daily_report" and "detailed" in lowered:
        content = path.read_text(encoding="utf-8", errors="replace")
        if "条件买入" in content or "单股仓位" in content:
            return "legacy_policy"
    return "legacy"


def _classify_legacy_row(table_name: str, row: Mapping[str, Any]) -> str:
    if str(row.get("record_kind") or "") in {"sample", "system_validation"}:
        return str(row["record_kind"])
    trade_date = str(row.get("trade_date") or row.get("review_date") or "")
    if trade_date in SAMPLE_DATES:
        return "sample"
    return "legacy"


def _legacy_row_key(
    connection: sqlite3.Connection,
    table_name: str,
    row: Mapping[str, Any],
    position: int,
) -> str:
    primary_keys = [
        item["name"]
        for item in connection.execute(f"PRAGMA table_info({_quote_identifier(table_name)})")
        if item["pk"]
    ]
    if primary_keys:
        return "|".join(f"{key}={row.get(key)}" for key in primary_keys)
    return f"row-{position}-{canonical_hash(row)[:16]}"


def _present_legacy_tables(
    connection: sqlite3.Connection,
    table_names: set[str],
) -> tuple[str, ...]:
    result: list[str] = []
    for name in LEGACY_TABLES:
        if name not in table_names:
            continue
        if name == "observations" and not _is_legacy_observations(connection):
            continue
        if name == "legacy_observations_v0" and "observations" in result:
            continue
        result.append(name)
    return tuple(result)


def _is_legacy_observations(connection: sqlite3.Connection) -> bool:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(observations)").fetchall()
    }
    return "review_date" in columns and "payload_json" not in columns


def _table_rows(connection: sqlite3.Connection, table_name: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT * FROM {_quote_identifier(table_name)} ORDER BY rowid"
    ).fetchall()
    return [dict(row) for row in rows]


def _table_count(connection: sqlite3.Connection, table_name: str) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}"
        ).fetchone()[0]
    )


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _current_schema_version(connection: sqlite3.Connection, names: set[str]) -> int:
    if "schema_versions" not in names:
        return 0
    value = connection.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
    return int(value or 0)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


# 哈希覆盖同一只读事务内的完整 schema、全部表记录和业务 PRAGMA，避开 SQLite 易变文件头。
def _database_snapshot_sha256(connection: sqlite3.Connection) -> str:
    try:
        schema = [
            dict(row)
            for row in connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                ORDER BY type, name
                """
            ).fetchall()
        ]
        tables: list[dict[str, Any]] = []
        table_names = [
            str(row["name"])
            for row in schema
            if str(row["type"]) == "table"
        ]
        for table_name in table_names:
            cursor = connection.execute(f"SELECT * FROM {_quote_identifier(table_name)}")
            columns = [str(item[0]) for item in cursor.description or ()]
            rows = [
                [_normalize_sqlite_hash_value(value) for value in row]
                for row in cursor.fetchall()
            ]
            rows.sort(key=canonical_hash)
            tables.append(
                {
                    "name": table_name,
                    "columns": columns,
                    "rows": rows,
                }
            )
        snapshot = {
            "schema": schema,
            "tables": tables,
            "application_id": int(
                connection.execute("PRAGMA application_id").fetchone()[0]
            ),
            "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        }
    except sqlite3.Error as error:
        raise ReviewRunDomainError(f"无法生成数据库一致快照指纹：{error}") from error
    return canonical_hash(snapshot)


def _normalize_sqlite_hash_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"sqlite_type": "blob", "hex": value.hex()}
    return value


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(value: datetime | None) -> str:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ReviewRunDomainError("迁移计划时间必须包含时区。")
    return timestamp.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewRunDomainError(f"{label} 必须是 JSON 对象。")
    return value
