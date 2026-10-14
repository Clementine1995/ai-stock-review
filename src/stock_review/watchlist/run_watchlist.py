# 本文件是核心异动票跟踪的应用入口：采集 → 计算 → 渲染 → 落盘，并支持候选收集。
# 只编排本包内的采集/计算/渲染，不写数据库、不触碰正式 ReviewRun 对象链。

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from stock_review.watchlist.collect_watchlist_candidates import (
    DEFAULT_TOP_N,
    collect_candidates,
)
from stock_review.watchlist.collect_watchlist_inputs import collect_watchlist_inputs
from stock_review.watchlist.compute_watchlist_facts import build_facts, build_reminders
from stock_review.watchlist.manage_watchlist_pool import DEFAULT_POOL_PATH, active_items
from stock_review.watchlist.render_watchlist import render_watchlist


DEFAULT_REPORT_DIR = Path("reports") / "watchlist"
DEFAULT_DATA_DIR = Path("data") / "watchlist"


def run_watchlist(
    trade_date: str,
    *,
    pool_path: Path = DEFAULT_POOL_PATH,
    report_dir: Path = DEFAULT_REPORT_DIR,
    data_dir: Path = DEFAULT_DATA_DIR,
    ak_client: Any | None = None,
    write: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """对跟踪池内的启用对象采集事实并出跟踪表；Markdown 与 JSON 同源。"""
    generated_at = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    items = active_items(pool_path)
    inputs = collect_watchlist_inputs(trade_date, items, ak_client=ak_client)
    facts = build_facts(inputs)
    markdown = render_watchlist(inputs, facts, generated_at=generated_at)

    result: dict[str, Any] = {
        "trade_date": trade_date,
        "generated_at": generated_at,
        "item_count": len(items),
        "fact_count": len(facts),
        "reminders": list(build_reminders(facts)),
        "report_path": None,
        "data_path": None,
        "gaps": list(inputs.gaps),
        "markdown": markdown,
    }

    if write:
        report_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{trade_date}.md"
        data_path = data_dir / f"{trade_date}.json"
        report_path.write_text(markdown, encoding="utf-8")
        data_path.write_text(
            json.dumps(
                {
                    "generated_at": generated_at,
                    "inputs": _serialize(inputs),
                    "facts": [_serialize(fact) for fact in facts],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        result["report_path"] = str(report_path)
        result["data_path"] = str(data_path)
    return result


def collect_candidates_file(
    trade_date: str,
    *,
    pool_path: Path = DEFAULT_POOL_PATH,
    data_dir: Path = DEFAULT_DATA_DIR,
    ak_client: Any | None = None,
    top_n: int = DEFAULT_TOP_N,
    write: bool = True,
) -> dict[str, Any]:
    """收集异动候选并按异动档位排序，落 data/watchlist/candidates/<日期>.json，供用户审核。"""
    pool = active_items(pool_path)
    payload = collect_candidates(
        trade_date,
        pool_codes=tuple(item.code for item in pool),
        ak_client=ak_client,
        top_n=top_n,
    )
    path = data_dir / "candidates" / f"{trade_date}.json"
    payload["path"] = None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["path"] = str(path)
    return payload


def _serialize(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value
