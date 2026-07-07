# 本文件只提供 SQLite 本地持久化能力，不承载复盘业务判断。

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3

from stock_review.evidence.evidence_snapshot import EvidenceSnapshot
from stock_review.pools.manage_pool_item import PoolItem


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
            self.ensure_table(connection)
            connection.execute(
                """
                INSERT INTO pool_items (
                    pool_type,
                    code,
                    name,
                    exchange,
                    sector,
                    reason,
                    start_date,
                    status,
                    note,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.pool_type,
                    item.code,
                    item.name,
                    item.exchange,
                    item.sector,
                    item.reason,
                    item.start_date,
                    item.status,
                    item.note,
                    item.created_at,
                    item.updated_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    # 查询按池子类型收敛，不在 SQL 中做核心票、走坏等业务判断。
    def list_items(self, pool_type: str | None = None) -> list[PoolItem]:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            self.ensure_table(connection)
            if pool_type is None:
                rows = connection.execute(
                    """
                    SELECT pool_type, code, name, exchange, sector, reason, start_date, status, note, created_at, updated_at
                    FROM pool_items
                    ORDER BY pool_type, start_date, code
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT pool_type, code, name, exchange, sector, reason, start_date, status, note, created_at, updated_at
                    FROM pool_items
                    WHERE pool_type = ?
                    ORDER BY start_date, code
                    """,
                    (pool_type,),
                ).fetchall()
        finally:
            connection.close()

        return [self.row_to_pool_item(row) for row in rows]

    def ensure_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pool_items (
                pool_type TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                sector TEXT NOT NULL,
                reason TEXT NOT NULL,
                start_date TEXT NOT NULL,
                status TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (pool_type, code)
            )
            """
        )

    def row_to_pool_item(self, row: sqlite3.Row) -> PoolItem:
        return PoolItem(
            pool_type=row["pool_type"],
            code=row["code"],
            name=row["name"],
            exchange=row["exchange"],
            sector=row["sector"],
            reason=row["reason"],
            start_date=row["start_date"],
            status=row["status"],
            note=row["note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
