# 本文件负责人工维护关注池和热点池记录，不做自动推荐或股票强弱判断。

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3


VALID_POOL_TYPES = {"watch", "hot"}
VALID_POOL_STATUSES = {"active", "paused", "removed"}
VALID_POOL_RECORD_KINDS = {"real", "sample"}
DEFAULT_DATABASE_PATH = Path("data") / "stock_review.sqlite"


class PoolItemError(ValueError):
    pass


@dataclass(frozen=True)
class PoolItem:
    pool_type: str
    code: str
    name: str
    exchange: str
    sectors: tuple[str, ...]
    reason: str
    start_date: str
    status: str
    note: str
    created_at: str
    updated_at: str
    record_kind: str


@dataclass(frozen=True)
class PoolItemStatusEvent:
    pool_type: str
    code: str
    status: str
    reason: str
    note: str
    changed_at: str


# 池子记录必须由用户显式给出股票代码和名称，避免系统编造股票身份。
def add_pool_item(
    pool_type: str,
    code: str,
    name: str,
    start_date: str,
    reason: str,
    exchange: str = "",
    sectors: tuple[str, ...] = (),
    note: str = "",
    record_kind: str = "real",
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> PoolItem:
    validate_pool_type(pool_type)
    normalized_code = code.strip()
    normalized_name = name.strip()
    if not normalized_code or not normalized_name:
        raise PoolItemError("股票代码和股票名称不能为空。")
    if pool_type == "hot" and not reason.strip():
        raise PoolItemError("加入热点池必须填写进入原因。")
    validate_pool_record_kind(record_kind)

    now = datetime.now().isoformat(timespec="seconds")
    item = PoolItem(
        pool_type=pool_type,
        code=normalized_code,
        name=normalized_name,
        exchange=exchange.strip() or "待确认",
        sectors=normalize_sectors(sectors),
        reason=reason.strip(),
        start_date=start_date,
        status="active",
        note=note.strip(),
        created_at=now,
        updated_at=now,
        record_kind=record_kind,
    )

    from stock_review.storage.sqlite_repository import PoolItemRepository

    repository = PoolItemRepository(database_path)
    try:
        repository.add_item(item)
    except sqlite3.IntegrityError as error:
        raise PoolItemError(f"{pool_label(pool_type)}已存在该股票：{normalized_code} {normalized_name}") from error
    return item


def list_pool_items(
    pool_type: str | None = None,
    record_kind: str | None = None,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> list[PoolItem]:
    if pool_type is not None:
        validate_pool_type(pool_type)
    if record_kind is not None:
        validate_pool_record_kind(record_kind)

    from stock_review.storage.sqlite_repository import PoolItemRepository

    return PoolItemRepository(database_path).list_items(pool_type, record_kind)


# 板块结构迁移必须由用户显式触发，避免普通读取或新增操作在不知情时改写本地库。
def migrate_pool_item_sectors(database_path: Path = DEFAULT_DATABASE_PATH) -> int:
    from stock_review.storage.sqlite_repository import PoolItemRepository

    return PoolItemRepository(database_path).migrate_legacy_sector_schema()


# 记录类型变更会影响是否进入真实计划，必须由用户显式确认并填写原因。
def update_pool_item_record_kind(
    pool_type: str,
    code: str,
    record_kind: str,
    reason: str,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> PoolItem:
    validate_pool_type(pool_type)
    validate_pool_record_kind(record_kind)
    normalized_code = code.strip()
    if not normalized_code:
        raise PoolItemError("股票代码不能为空。")
    if not reason.strip():
        raise PoolItemError("更新记录类型必须填写原因。")

    from stock_review.storage.sqlite_repository import PoolItemRepository

    item = PoolItemRepository(database_path).update_item_record_kind(pool_type, normalized_code, record_kind)
    if item is None:
        raise PoolItemError(f"未找到{pool_label(pool_type)}记录：{normalized_code}")
    return item


# 状态变更只记录用户明确给出的维护原因，removed 仍保留历史记录以便复盘回查。
def update_pool_item_status(
    pool_type: str,
    code: str,
    status: str,
    reason: str,
    note: str = "",
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> PoolItem:
    validate_pool_type(pool_type)
    validate_pool_status(status)
    normalized_code = code.strip()
    if not normalized_code:
        raise PoolItemError("股票代码不能为空。")
    if not reason.strip():
        raise PoolItemError("更新池子状态必须填写原因。")

    from stock_review.storage.sqlite_repository import PoolItemRepository

    now = datetime.now().isoformat(timespec="seconds")
    item = PoolItemRepository(database_path).update_item_status(
        pool_type=pool_type,
        code=normalized_code,
        status=status,
        reason=reason.strip(),
        note=note.strip(),
        changed_at=now,
    )
    if item is None:
        raise PoolItemError(f"未找到{pool_label(pool_type)}记录：{normalized_code}")
    return item


def list_pool_item_status_history(
    pool_type: str,
    code: str,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> list[PoolItemStatusEvent]:
    validate_pool_type(pool_type)
    normalized_code = code.strip()
    if not normalized_code:
        raise PoolItemError("股票代码不能为空。")

    from stock_review.storage.sqlite_repository import PoolItemRepository

    return PoolItemRepository(database_path).list_status_history(pool_type, normalized_code)


def validate_pool_type(pool_type: str) -> None:
    if pool_type not in VALID_POOL_TYPES:
        raise PoolItemError("池子类型必须是 watch 或 hot。")


def validate_pool_status(status: str) -> None:
    if status not in VALID_POOL_STATUSES:
        raise PoolItemError("池子状态必须是 active、paused 或 removed。")


def validate_pool_record_kind(record_kind: str) -> None:
    if record_kind not in VALID_POOL_RECORD_KINDS:
        raise PoolItemError("记录类型必须是 real 或 sample。")


def normalize_sectors(sectors: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(sector.strip() for sector in sectors if sector.strip()))
    if len(normalized) > 3:
        raise PoolItemError("短线视角下每个池子对象最多关联 3 个板块。")
    return normalized or ("待确认",)


def pool_label(pool_type: str) -> str:
    return "关注池" if pool_type == "watch" else "热点池"
