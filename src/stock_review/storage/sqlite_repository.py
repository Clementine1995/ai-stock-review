# 本文件只提供 SQLite 本地持久化能力，不承载复盘业务判断。

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3

from stock_review.evidence.evidence_snapshot import EvidenceSnapshot
from stock_review.observations.manage_observation import Observation
from stock_review.pools.manage_pool_item import PoolItem, PoolItemError, PoolItemStatusEvent
from stock_review.review_documents.manage_manual_review import ManualPreview, ManualStepRecord


class EvidenceSnapshotRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    # 初始表结构只覆盖 Evidence Snapshot，后续池子、计划和 Observation 单独扩展。
    def save_snapshot(self, snapshot: EvidenceSnapshot) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_snapshots (
                    trade_date TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    sample_date TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    missing_fields_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO evidence_snapshots (
                    trade_date,
                    source,
                    sample_date,
                    snapshot_json,
                    missing_fields_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.trade_date,
                    snapshot.source,
                    snapshot.sample_date,
                    json.dumps(snapshot.to_mapping(), ensure_ascii=False),
                    json.dumps(list(snapshot.missing_fields), ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    # 只读回查用于测试和后续 CLI 检查，不在 SQL 中拼业务判断。
    def load_snapshot(self, trade_date: str) -> dict[str, object] | None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT trade_date, source, sample_date, snapshot_json, missing_fields_json
                FROM evidence_snapshots
                WHERE trade_date = ?
                """,
                (trade_date,),
            ).fetchone()
        finally:
            connection.close()

        if row is None:
            return None

        return {
            "trade_date": row["trade_date"],
            "source": row["source"],
            "sample_date": row["sample_date"],
            "snapshot": json.loads(row["snapshot_json"]),
            "missing_fields": json.loads(row["missing_fields_json"]),
        }


class PoolItemRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    # 池子表只保存人工维护记录，不写入任何自动推荐或走势判断。
    def add_item(self, item: PoolItem) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            self.ensure_tables(connection)
            connection.execute(
                """
                INSERT INTO pool_items (
                    pool_type,
                    code,
                    name,
                    exchange,
                    reason,
                    start_date,
                    status,
                    note,
                    created_at,
                    updated_at,
                    record_kind
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.pool_type,
                    item.code,
                    item.name,
                    item.exchange,
                    item.reason,
                    item.start_date,
                    item.status,
                    item.note,
                    item.created_at,
                    item.updated_at,
                    item.record_kind,
                ),
            )
            self.save_item_sectors(connection, item)
            connection.execute(
                """
                INSERT INTO pool_item_status_history (
                    pool_type,
                    code,
                    status,
                    reason,
                    note,
                    changed_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item.pool_type,
                    item.code,
                    item.status,
                    item.reason,
                    item.note,
                    item.created_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    # 查询按池子类型收敛，不在 SQL 中做核心票、走坏等业务判断。
    def list_items(self, pool_type: str | None = None, record_kind: str | None = None) -> list[PoolItem]:
        if not self.database_path.exists():
            return []
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            if not self.table_exists(connection, "pool_items"):
                return []
            select_columns = self.pool_item_select_columns(connection)
            if pool_type is None:
                rows = connection.execute(
                    f"SELECT {select_columns} FROM pool_items ORDER BY pool_type, start_date, code"
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT {select_columns} FROM pool_items WHERE pool_type = ? ORDER BY start_date, code",
                    (pool_type,),
                ).fetchall()
        finally:
            connection.close()

        sector_map = self.load_sector_map(connection=None, rows=rows)
        items = [self.row_to_pool_item(row, sector_map.get((row["pool_type"], row["code"]), ())) for row in rows]
        return [item for item in items if record_kind is None or item.record_kind == record_kind]

    # 池子主表保存当前状态；板块采用独立关联表，允许同一对象保留最多三条人工确认归属。
    def ensure_tables(self, connection: sqlite3.Connection) -> None:
        if self.table_exists(connection, "pool_items") and self.column_exists(connection, "pool_items", "sector"):
            raise PoolItemError("本地池子仍是单板块结构，请先显式执行 pool migrate-sectors。")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pool_items (
                pool_type TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                reason TEXT NOT NULL,
                start_date TEXT NOT NULL,
                status TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                record_kind TEXT NOT NULL DEFAULT 'real',
                PRIMARY KEY (pool_type, code)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pool_item_sectors (
                pool_type TEXT NOT NULL,
                code TEXT NOT NULL,
                sector TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (pool_type, code, sector)
            )
            """
        )
        if not self.column_exists(connection, "pool_items", "record_kind"):
            connection.execute("ALTER TABLE pool_items ADD COLUMN record_kind TEXT NOT NULL DEFAULT 'real'")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pool_item_status_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pool_type TEXT NOT NULL,
                code TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                note TEXT NOT NULL,
                changed_at TEXT NOT NULL
            )
            """
        )

    def update_item_status(
        self,
        pool_type: str,
        code: str,
        status: str,
        reason: str,
        note: str,
        changed_at: str,
    ) -> PoolItem | None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            self.ensure_tables(connection)
            select_columns = self.pool_item_select_columns(connection)
            row = connection.execute(
                f"SELECT {select_columns} FROM pool_items WHERE pool_type = ? AND code = ?",
                (pool_type, code),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE pool_items
                SET status = ?, updated_at = ?
                WHERE pool_type = ? AND code = ?
                """,
                (status, changed_at, pool_type, code),
            )
            connection.execute(
                """
                INSERT INTO pool_item_status_history (
                    pool_type,
                    code,
                    status,
                    reason,
                    note,
                    changed_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (pool_type, code, status, reason, note, changed_at),
            )
            connection.commit()
            updated_row = connection.execute(
                f"SELECT {select_columns} FROM pool_items WHERE pool_type = ? AND code = ?",
                (pool_type, code),
            ).fetchone()
            sectors = self.load_sector_map(connection, [updated_row]).get((pool_type, code), ()) if updated_row else ()
        finally:
            connection.close()

        return self.row_to_pool_item(updated_row, sectors) if updated_row is not None else None

    # 类型迁移只改变样例或真实标记，不改变池子状态、原因或历史状态事件。
    def update_item_record_kind(self, pool_type: str, code: str, record_kind: str) -> PoolItem | None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            self.ensure_tables(connection)
            connection.execute(
                "UPDATE pool_items SET record_kind = ? WHERE pool_type = ? AND code = ?",
                (record_kind, pool_type, code),
            )
            connection.commit()
            row = connection.execute(
                f"SELECT {self.pool_item_select_columns(connection)} FROM pool_items WHERE pool_type = ? AND code = ?",
                (pool_type, code),
            ).fetchone()
            sectors = self.load_sector_map(connection, [row]).get((pool_type, code), ()) if row else ()
        finally:
            connection.close()
        return self.row_to_pool_item(row, sectors) if row is not None else None

    # 状态历史按发生时间返回，供用户回查暂停、移出和重新启用的原因。
    def list_status_history(self, pool_type: str, code: str) -> list[PoolItemStatusEvent]:
        if not self.database_path.exists():
            return []
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            if not self.table_exists(connection, "pool_item_status_history"):
                return []
            rows = connection.execute(
                """
                SELECT pool_type, code, status, reason, note, changed_at
                FROM pool_item_status_history
                WHERE pool_type = ? AND code = ?
                ORDER BY history_id
                """,
                (pool_type, code),
            ).fetchall()
        finally:
            connection.close()

        return [
            PoolItemStatusEvent(
                pool_type=row["pool_type"],
                code=row["code"],
                status=row["status"],
                reason=row["reason"],
                note=row["note"],
                changed_at=row["changed_at"],
            )
            for row in rows
        ]

    @staticmethod
    def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    @staticmethod
    def column_exists(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
        return any(row[1] == column_name for row in connection.execute(f"PRAGMA table_info({table_name})"))

    def pool_item_select_columns(self, connection: sqlite3.Connection) -> str:
        base_columns = "pool_type, code, name, exchange, reason, start_date, status, note, created_at, updated_at"
        if self.column_exists(connection, "pool_items", "record_kind"):
            return f"{base_columns}, record_kind"
        return f"{base_columns}, '待确认' AS record_kind"

    def row_to_pool_item(self, row: sqlite3.Row, sectors: tuple[str, ...]) -> PoolItem:
        return PoolItem(
            pool_type=row["pool_type"],
            code=row["code"],
            name=row["name"],
            exchange=row["exchange"],
            sectors=sectors,
            reason=row["reason"],
            start_date=row["start_date"],
            status=row["status"],
            note=row["note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            record_kind=row["record_kind"],
        )

    def save_item_sectors(self, connection: sqlite3.Connection, item: PoolItem) -> None:
        connection.executemany(
            """
            INSERT INTO pool_item_sectors (pool_type, code, sector, source, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(item.pool_type, item.code, sector, "人工确认", item.created_at) for sector in item.sectors],
        )

    def load_sector_map(
        self, connection: sqlite3.Connection | None, rows: list[sqlite3.Row]
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        if not rows:
            return {}
        local_connection = connection or sqlite3.connect(self.database_path)
        try:
            if not self.table_exists(local_connection, "pool_item_sectors"):
                return {}
            sector_rows = local_connection.execute(
                "SELECT pool_type, code, sector FROM pool_item_sectors ORDER BY rowid"
            ).fetchall()
        finally:
            if connection is None:
                local_connection.close()
        values: dict[tuple[str, str], list[str]] = {}
        for pool_type, code, sector in sector_rows:
            values.setdefault((pool_type, code), []).append(sector)
        return {key: tuple(sectors) for key, sectors in values.items()}

    # 显式迁移保留原有单板块字段为第一条人工确认关联，完成后删除废弃列，避免新旧双轨。
    def migrate_legacy_sector_schema(self) -> int:
        if not self.database_path.exists():
            return 0
        connection = sqlite3.connect(self.database_path)
        try:
            if not self.table_exists(connection, "pool_items") or not self.column_exists(connection, "pool_items", "sector"):
                return 0
            rows = connection.execute(
                """
                SELECT pool_type, code, name, exchange, sector, reason, start_date,
                       status, note, created_at, updated_at, record_kind
                FROM pool_items
                """
            ).fetchall()
            connection.execute(
                """
                CREATE TABLE pool_items_rebuilt (
                    pool_type TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL,
                    exchange TEXT NOT NULL, reason TEXT NOT NULL, start_date TEXT NOT NULL,
                    status TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, record_kind TEXT NOT NULL DEFAULT 'real',
                    PRIMARY KEY (pool_type, code)
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO pool_items_rebuilt (
                    pool_type, code, name, exchange, reason, start_date, status,
                    note, created_at, updated_at, record_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(row[0], row[1], row[2], row[3], row[5], row[6], row[7], row[8], row[9], row[10], row[11]) for row in rows],
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pool_item_sectors (
                    pool_type TEXT NOT NULL, code TEXT NOT NULL, sector TEXT NOT NULL,
                    source TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (pool_type, code, sector)
                )
                """
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO pool_item_sectors (pool_type, code, sector, source, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(row[0], row[1], row[4] or "待确认", "迁移自旧单板块字段", row[9]) for row in rows],
            )
            connection.execute("DROP TABLE pool_items")
            connection.execute("ALTER TABLE pool_items_rebuilt RENAME TO pool_items")
            connection.commit()
            return len(rows)
        finally:
            connection.close()


class ManualReviewRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    # 人工复盘表只保存用户显式输入；不与池子、计划或 Observation 做隐式联动。
    def add_step_record(self, record: ManualStepRecord) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            self.ensure_tables(connection)
            connection.execute(
                """
                INSERT INTO manual_step_records (
                    record_id, review_date, step_number, judgment, hypothesis,
                    evidence_reference, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id, record.review_date, record.step_number, record.judgment,
                    record.hypothesis, record.evidence_reference, record.note, record.created_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    # STEP 8 预演独立保存四类条件，必须由上层服务完成用户确认后才能写入。
    def add_preview(self, preview: ManualPreview) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            self.ensure_tables(connection)
            connection.execute(
                """
                INSERT INTO manual_previews (
                    preview_id, review_date, target, expectation, over_expectation,
                    under_expectation, abandon_condition, evidence_reference, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preview.preview_id, preview.review_date, preview.target, preview.expectation,
                    preview.over_expectation, preview.under_expectation, preview.abandon_condition,
                    preview.evidence_reference, preview.created_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    # ID 按复盘日期递增，便于未来由用户明确选择记录与计划或 Observation 建立关联。
    def next_step_record_id(self, review_date: str, step_number: int) -> str:
        prefix = f"STEP-{review_date.replace('-', '')}-{step_number:02d}-"
        return self.next_id("manual_step_records", "record_id", prefix)

    def next_preview_id(self, review_date: str) -> str:
        prefix = f"PREVIEW-{review_date.replace('-', '')}-"
        return self.next_id("manual_previews", "preview_id", prefix)

    def next_id(self, table_name: str, id_column: str, prefix: str) -> str:
        connection = sqlite3.connect(self.database_path)
        try:
            self.ensure_tables(connection)
            row = connection.execute(
                f"SELECT {id_column} FROM {table_name} WHERE {id_column} LIKE ? ORDER BY {id_column} DESC LIMIT 1",
                (f"{prefix}%",),
            ).fetchone()
        finally:
            connection.close()
        sequence = int(row[0].removeprefix(prefix)) + 1 if row else 1
        return f"{prefix}{sequence:03d}"

    # 读取只按复盘日期返回人工原始记录，不在 Repository 层推导交易判断。
    def list_step_records(self, review_date: str) -> list[ManualStepRecord]:
        return [
            ManualStepRecord(*row)
            for row in self.read_rows(
                """
                SELECT record_id, review_date, step_number, judgment, hypothesis,
                       evidence_reference, note, created_at
                FROM manual_step_records WHERE review_date = ? ORDER BY step_number
                """,
                review_date,
            )
        ]

    def list_previews(self, review_date: str) -> list[ManualPreview]:
        return [
            ManualPreview(*row)
            for row in self.read_rows(
                """
                SELECT preview_id, review_date, target, expectation, over_expectation,
                       under_expectation, abandon_condition, evidence_reference, created_at
                FROM manual_previews WHERE review_date = ? ORDER BY preview_id
                """,
                review_date,
            )
        ]

    def read_rows(self, statement: str, review_date: str) -> list[tuple[object, ...]]:
        if not self.database_path.exists():
            return []
        connection = sqlite3.connect(self.database_path)
        try:
            if not self.table_exists(connection, "manual_step_records"):
                return []
            return connection.execute(statement, (review_date,)).fetchall()
        finally:
            connection.close()

    def ensure_tables(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_step_records (
                record_id TEXT PRIMARY KEY,
                review_date TEXT NOT NULL,
                step_number INTEGER NOT NULL,
                judgment TEXT NOT NULL,
                hypothesis TEXT NOT NULL,
                evidence_reference TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (review_date, step_number)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_previews (
                preview_id TEXT PRIMARY KEY,
                review_date TEXT NOT NULL,
                target TEXT NOT NULL,
                expectation TEXT NOT NULL,
                over_expectation TEXT NOT NULL,
                under_expectation TEXT NOT NULL,
                abandon_condition TEXT NOT NULL,
                evidence_reference TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (review_date, target)
            )
            """
        )

    @staticmethod
    def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
        ).fetchone()
        return row is not None


class ObservationRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    # Observation 主表保存人工判断及当前状态，唯一约束用于阻止同日重复判断。
    def add_observation(self, observation: Observation) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            self.ensure_tables(connection)
            connection.execute(
                """
                INSERT INTO observations (
                    observation_id,
                    review_date,
                    topic,
                    related_target,
                    hypothesis,
                    confirmation_condition,
                    invalidation_condition,
                    evidence_source,
                    plan_item,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.review_date,
                    observation.topic,
                    observation.related_target,
                    observation.hypothesis,
                    observation.confirmation_condition,
                    observation.invalidation_condition,
                    observation.evidence_source,
                    observation.plan_item,
                    observation.status,
                    observation.created_at,
                    observation.updated_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    # ID 在单个复盘日期内递增，便于 CLI 人工引用和日志追踪。
    def next_observation_id(self, review_date: str) -> str:
        connection = sqlite3.connect(self.database_path)
        try:
            self.ensure_tables(connection)
            prefix = f"OBS-{review_date.replace('-', '')}-"
            row = connection.execute(
                """
                SELECT observation_id
                FROM observations
                WHERE observation_id LIKE ?
                ORDER BY observation_id DESC
                LIMIT 1
                """,
                (f"{prefix}%",),
            ).fetchone()
        finally:
            connection.close()

        sequence = int(row[0].removeprefix(prefix)) + 1 if row else 1
        return f"{prefix}{sequence:03d}"

    # 列表查询只返回持久化事实，状态筛选不承担后续学习判断。
    def list_observations(
        self,
        review_date: str | None = None,
        status: str | None = None,
    ) -> list[Observation]:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            self.ensure_tables(connection)
            clauses: list[str] = []
            parameters: list[str] = []
            if review_date is not None:
                clauses.append("observations.review_date = ?")
                parameters.append(review_date)
            if status is not None:
                clauses.append("observations.status = ?")
                parameters.append(status)
            where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"""
                SELECT
                    observations.observation_id,
                    observations.review_date,
                    observations.topic,
                    observations.related_target,
                    observations.hypothesis,
                    observations.confirmation_condition,
                    observations.invalidation_condition,
                    observations.evidence_source,
                    observations.plan_item,
                    observations.status,
                    observations.created_at,
                    observations.updated_at,
                    COALESCE(observation_reviews.actual_result, '') AS actual_result,
                    COALESCE(observation_reviews.review_note, '') AS review_note
                FROM observations
                LEFT JOIN observation_reviews
                    ON observation_reviews.observation_id = observations.observation_id
                {where_clause}
                ORDER BY observations.review_date, observations.observation_id
                """,
                parameters,
            ).fetchall()
        finally:
            connection.close()
        return [self.row_to_observation(row) for row in rows]

    # 周度查询只按复盘日期范围读取，不把 invalid 或 pending 在存储层静默过滤。
    def list_observations_between(self, start_date: str, end_date: str) -> list[Observation]:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            self.ensure_tables(connection)
            rows = connection.execute(
                """
                SELECT
                    observations.observation_id,
                    observations.review_date,
                    observations.topic,
                    observations.related_target,
                    observations.hypothesis,
                    observations.confirmation_condition,
                    observations.invalidation_condition,
                    observations.evidence_source,
                    observations.plan_item,
                    observations.status,
                    observations.created_at,
                    observations.updated_at,
                    COALESCE(observation_reviews.actual_result, '') AS actual_result,
                    COALESCE(observation_reviews.review_note, '') AS review_note
                FROM observations
                LEFT JOIN observation_reviews
                    ON observation_reviews.observation_id = observations.observation_id
                WHERE observations.review_date BETWEEN ? AND ?
                ORDER BY observations.review_date, observations.observation_id
                """,
                (start_date, end_date),
            ).fetchall()
        finally:
            connection.close()
        return [self.row_to_observation(row) for row in rows]

    # 回填与主表状态在同一事务更新，避免状态和实际结果不一致。
    def review_observation(
        self,
        observation_id: str,
        status: str,
        actual_result: str,
        review_note: str,
        reviewed_at: str,
    ) -> Observation | None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            self.ensure_tables(connection)
            exists = connection.execute(
                "SELECT 1 FROM observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
            if exists is None:
                return None
            connection.execute(
                """
                INSERT INTO observation_reviews (
                    observation_id,
                    actual_result,
                    status,
                    review_note,
                    reviewed_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    actual_result = excluded.actual_result,
                    status = excluded.status,
                    review_note = excluded.review_note,
                    reviewed_at = excluded.reviewed_at
                """,
                (observation_id, actual_result, status, review_note, reviewed_at),
            )
            connection.execute(
                """
                UPDATE observations
                SET status = ?, updated_at = ?
                WHERE observation_id = ?
                """,
                (status, reviewed_at, observation_id),
            )
            connection.commit()
        finally:
            connection.close()

        observations = self.list_observations()
        return next(
            (observation for observation in observations if observation.observation_id == observation_id),
            None,
        )

    def ensure_tables(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                observation_id TEXT PRIMARY KEY,
                review_date TEXT NOT NULL,
                topic TEXT NOT NULL,
                related_target TEXT NOT NULL,
                hypothesis TEXT NOT NULL,
                confirmation_condition TEXT NOT NULL,
                invalidation_condition TEXT NOT NULL,
                evidence_source TEXT NOT NULL,
                plan_item TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (review_date, topic, hypothesis)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS observation_reviews (
                observation_id TEXT PRIMARY KEY,
                actual_result TEXT NOT NULL,
                status TEXT NOT NULL,
                review_note TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                FOREIGN KEY (observation_id) REFERENCES observations (observation_id)
            )
            """
        )

    def row_to_observation(self, row: sqlite3.Row) -> Observation:
        return Observation(
            observation_id=row["observation_id"],
            review_date=row["review_date"],
            topic=row["topic"],
            related_target=row["related_target"],
            hypothesis=row["hypothesis"],
            confirmation_condition=row["confirmation_condition"],
            invalidation_condition=row["invalidation_condition"],
            evidence_source=row["evidence_source"],
            plan_item=row["plan_item"],
            status=row["status"],
            actual_result=row["actual_result"],
            review_note=row["review_note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
