# 本文件负责按本地已采集交易日判断真实池是否需要人工复核，不修改池子状态。

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from stock_review.pools.manage_pool_item import PoolItem


MAX_UNCONFIRMED_TRADING_DAYS = 5


@dataclass(frozen=True)
class StalePoolItem:
    item: PoolItem
    last_confirmed_date: str
    unconfirmed_trading_days: int


@dataclass(frozen=True)
class PoolFreshnessSummary:
    fresh_items: tuple[PoolItem, ...]
    stale_items: tuple[StalePoolItem, ...]


# 已激活真实池超过五个本地已采集交易日未续期时进入待复核区，但不自动暂停或删除。
def classify_active_real_pool_items(
    items: list[PoolItem],
    review_date: str,
    snapshot_dir: Path,
    maximum_unconfirmed_days: int = MAX_UNCONFIRMED_TRADING_DAYS,
) -> PoolFreshnessSummary:
    normalized_review_date = date.fromisoformat(review_date)
    if maximum_unconfirmed_days < 1:
        raise ValueError("池子复核交易日阈值必须大于等于 1。")

    snapshot_dates = collect_snapshot_dates(snapshot_dir, normalized_review_date)
    fresh_items: list[PoolItem] = []
    stale_items: list[StalePoolItem] = []
    for item in items:
        if item.status != "active" or item.record_kind != "real":
            continue
        last_confirmed_date = resolve_last_confirmed_date(item, normalized_review_date)
        unconfirmed_days = sum(
            1 for snapshot_date in snapshot_dates if last_confirmed_date < snapshot_date <= normalized_review_date
        )
        if unconfirmed_days >= maximum_unconfirmed_days:
            stale_items.append(
                StalePoolItem(
                    item=item,
                    last_confirmed_date=last_confirmed_date.isoformat(),
                    unconfirmed_trading_days=unconfirmed_days,
                )
            )
        else:
            fresh_items.append(item)

    return PoolFreshnessSummary(tuple(fresh_items), tuple(stale_items))


def collect_snapshot_dates(snapshot_dir: Path, review_date: date) -> tuple[date, ...]:
    values: set[date] = set()
    for snapshot_path in snapshot_dir.glob("*_snapshot.json"):
        date_text = snapshot_path.name.removesuffix("_snapshot.json")
        try:
            snapshot_date = date.fromisoformat(date_text)
        except ValueError:
            continue
        if snapshot_date <= review_date:
            values.add(snapshot_date)
    return tuple(sorted(values))


# 历史回看不得使用复盘日之后的续期时间；这种情况下回退到最初入池日期。
def resolve_last_confirmed_date(item: PoolItem, review_date: date) -> date:
    start_date = date.fromisoformat(item.start_date)
    try:
        updated_date = date.fromisoformat(item.updated_at[:10])
    except ValueError:
        return start_date
    return updated_date if updated_date <= review_date else start_date
