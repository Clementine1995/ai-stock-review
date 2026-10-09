# 本文件负责把用户明确选择的当日证据候选写入热点池，不做自动筛选或批量推荐。

from __future__ import annotations

from pathlib import Path

from stock_review.evidence.manage_evidence_snapshot import DEFAULT_EVIDENCE_DIR
from stock_review.pools.build_pool_candidates import build_pool_candidate_summary
from stock_review.pools.manage_pool_item import DEFAULT_DATABASE_PATH, PoolItem, add_pool_item


class PoolCandidateConfirmationError(ValueError):
    pass


# 入池对象的代码、名称、板块和角色必须来自同日快照；用户只需确认代码和可选补充原因。
def confirm_hot_pool_candidate(
    trade_date: str,
    code: str,
    user_confirmed: bool,
    snapshot_dir: Path = DEFAULT_EVIDENCE_DIR,
    reason: str = "",
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> PoolItem:
    if not user_confirmed:
        raise PoolCandidateConfirmationError("系统候选加入热点池必须由用户显式确认。")

    normalized_code = code.strip()
    summary = build_pool_candidate_summary(trade_date, snapshot_dir)
    candidate = next(
        (item for item in summary.verified_stock_candidates if item.code == normalized_code),
        None,
    )
    if candidate is None:
        raise PoolCandidateConfirmationError(
            f"{trade_date} Evidence Snapshot 中没有可确认代码的候选：{normalized_code}"
        )

    confirmed_reason = reason.strip() or f"用户确认的当日系统候选：{candidate.role_source}"
    exchange = candidate.exchange if candidate.exchange != "待确认" else infer_exchange(candidate.code)
    return add_pool_item(
        "hot",
        candidate.code,
        candidate.name,
        trade_date,
        confirmed_reason,
        exchange=exchange,
        sectors=(candidate.sector,),
        note=f"证据来源：{candidate.evidence_source}",
        database_path=database_path,
    )


# 交易所只按已确认六码代码的市场前缀确定，不交给模型补写。
def infer_exchange(code: str) -> str:
    if code.startswith("6"):
        return "SSE"
    if code.startswith(("0", "2", "3")):
        return "SZSE"
    if code.startswith(("4", "8", "9")):
        return "BSE"
    raise PoolCandidateConfirmationError(f"无法根据股票代码确认交易所：{code}")
