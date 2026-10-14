# 本文件维护核心异动票的独立跟踪池（data/watchlist/pool.json）。
# 池成员必须由用户审核后显式加入；本模块不自动入池、不判断股票强弱、不触发正式对象链。

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from stock_review.watchlist.watchlist_inputs import WatchlistItem, build_item


DEFAULT_POOL_PATH = Path("data") / "watchlist" / "pool.json"

VALID_STATUSES = ("active", "paused", "removed")


class WatchlistPoolError(ValueError):
    pass


def load_pool(path: Path = DEFAULT_POOL_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"items": [], "updated_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WatchlistPoolError(f"跟踪池文件读取失败：{path}：{error}") from error
    if not isinstance(payload, dict):
        raise WatchlistPoolError("跟踪池顶层必须是 JSON 对象。")
    payload.setdefault("items", [])
    return payload


def save_pool(payload: dict[str, Any], path: Path = DEFAULT_POOL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    # 池内按代码排序，避免同一次维护在不同机器上产生顺序差异。
    payload["items"] = sorted(payload.get("items") or [], key=lambda item: str(item.get("code") or ""))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pool_items(path: Path = DEFAULT_POOL_PATH) -> tuple[WatchlistItem, ...]:
    payload = load_pool(path)
    items: list[WatchlistItem] = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not code or not name:
            continue
        items.append(
            build_item(
                code=code,
                name=name,
                added_date=str(raw.get("added_date") or ""),
                reason=str(raw.get("reason") or ""),
                status=str(raw.get("status") or "active"),
            )
        )
    return tuple(items)


def active_items(path: Path = DEFAULT_POOL_PATH) -> tuple[WatchlistItem, ...]:
    return tuple(item for item in pool_items(path) if item.status == "active")


def add_item(
    code: str,
    name: str,
    reason: str,
    added_date: str,
    path: Path = DEFAULT_POOL_PATH,
    status: str = "active",
) -> WatchlistItem:
    item = build_item(code=code, name=name, added_date=added_date, reason=reason, status=status)
    if item.board == "待确认":
        raise WatchlistPoolError(f"无法由代码判定板块，拒绝入池：{code}")
    if not item.reason:
        raise WatchlistPoolError("加入跟踪池必须填写入池原因（异动档位或来源）。")
    payload = load_pool(path)
    for raw in payload.get("items") or []:
        if str(raw.get("code") or "") == item.code:
            raise WatchlistPoolError(f"跟踪池已存在该代码：{item.code} {item.name}")
    payload.setdefault("items", []).append(
        {
            "code": item.code,
            "name": item.name,
            "board": item.board,
            "benchmark": item.benchmark,
            "added_date": item.added_date,
            "reason": item.reason,
            "status": item.status,
        }
    )
    save_pool(payload, path)
    return item


def set_status(
    code: str,
    status: str,
    reason: str,
    path: Path = DEFAULT_POOL_PATH,
) -> WatchlistItem:
    if status not in VALID_STATUSES:
        raise WatchlistPoolError(f"池状态必须是 {'、'.join(VALID_STATUSES)}。")
    if not reason:
        raise WatchlistPoolError("变更池状态必须填写原因。")
    payload = load_pool(path)
    target = str(code).strip()
    updated: WatchlistItem | None = None
    for raw in payload.get("items") or []:
        if str(raw.get("code") or "") != target:
            continue
        raw["status"] = status
        raw["status_reason"] = reason
        raw["status_changed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        updated = build_item(
            code=target,
            name=str(raw.get("name") or ""),
            added_date=str(raw.get("added_date") or ""),
            reason=str(raw.get("reason") or ""),
            status=status,
        )
    if updated is None:
        raise WatchlistPoolError(f"跟踪池未找到该代码：{target}")
    save_pool(payload, path)
    return updated
