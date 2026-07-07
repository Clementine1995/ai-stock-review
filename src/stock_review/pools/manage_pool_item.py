# 本文件负责人工维护关注池和热点池记录，不做自动推荐或股票强弱判断。

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3


VALID_POOL_TYPES = {"watch", "hot"}
DEFAULT_DATABASE_PATH = Path("data") / "stock_review.sqlite"


class PoolItemError(ValueError):
    pass


@dataclass(frozen=True)
class PoolItem:
    pool_type: str
    code: str
    name: str
    exchange: str
    sector: str
    reason: str
    start_date: str
    status: str
    note: str
    created_at: str
    updated_at: str


# 池子记录必须由用户显式给出股票代码和名称，避免系统编造股票身份。
def add_pool_item(
    pool_type: str,
    code: str,
    name: str,
    start_date: str,
    reason: str,
    exchange: str = "",
    sector: str = "",
    note: str = "",
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> PoolItem:
    validate_pool_type(pool_type)
    normalized_code = code.strip()
    normalized_name = name.strip()
    if not normalized_code or not normalized_name:
        raise PoolItemError("股票代码和股票名称不能为空。")
    if pool_type == "hot" and not reason.strip():
        raise PoolItemError("加入热点池必须填写进入原因。")

    now = datetime.now().isoformat(timespec="seconds")
    item = PoolItem(
        pool_type=pool_type,
        code=normalized_code,
        name=normalized_name,
        exchange=exchange.strip() or "待确认",
        sector=sector.strip() or "待确认",
        reason=reason.strip(),
        start_date=start_date,
        status="active",
        note=note.strip(),
        created_at=now,
        updated_at=now,
    )

    from stock_review.storage.sqlite_repository import PoolItemRepository

    repository = PoolItemRepository(database_path)
    try:
        repository.add_item(item)
    except sqlite3.IntegrityError as error:
        raise PoolItemError(f"{pool_label(pool_type)}已存在该股票：{normalized_code} {normalized_name}") from error
    return item


def list_pool_items(pool_type: str | None = None, database_path: Path = DEFAULT_DATABASE_PATH) -> list[PoolItem]:
    if pool_type is not None:
        validate_pool_type(pool_type)

    from stock_review.storage.sqlite_repository import PoolItemRepository

    return PoolItemRepository(database_path).list_items(pool_type)


def validate_pool_type(pool_type: str) -> None:
    if pool_type not in VALID_POOL_TYPES:
        raise PoolItemError("池子类型必须是 watch 或 hot。")


def pool_label(pool_type: str) -> str:
    return "关注池" if pool_type == "watch" else "热点池"
