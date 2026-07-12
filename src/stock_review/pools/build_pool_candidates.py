# 本文件负责从本地 Evidence Snapshot 整理池子候选事实，不联网、不写入池子或认定核心票。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stock_review.evidence.evidence_snapshot import EvidenceSnapshot
from stock_review.evidence.manage_evidence_snapshot import check_evidence_snapshot


@dataclass(frozen=True)
class VerifiedStockCandidate:
    code: str
    name: str
    exchange: str
    sector: str
    role: str
    role_source: str
    evidence_source: str
    change_percent: str
    reason: str


@dataclass(frozen=True)
class UnverifiedStockFact:
    name: str
    sector: str
    role: str
    evidence_source: str
    reason: str


@dataclass(frozen=True)
class ThemeRankingFact:
    name: str
    source_type: str
    rank: str
    limit_up_count: str
    net_inflow_yi: str
    leading_stock: str


@dataclass(frozen=True)
class PoolCandidateSummary:
    trade_date: str
    sample_date: str
    verified_stock_candidates: tuple[VerifiedStockCandidate, ...]
    unverified_stock_facts: tuple[UnverifiedStockFact, ...]
    theme_ranking_facts: tuple[ThemeRankingFact, ...]


# 候选只读取当前快照已有事实；代码、名称或身份缺失时不得作为可直接入池对象。
def build_pool_candidate_summary(trade_date: str, snapshot_dir: Path) -> PoolCandidateSummary:
    snapshot = check_evidence_snapshot(trade_date, snapshot_dir=snapshot_dir)
    verified_candidates, unverified_facts = extract_stock_candidates(snapshot)
    return PoolCandidateSummary(
        trade_date=snapshot.trade_date,
        sample_date=snapshot.sample_date,
        verified_stock_candidates=verified_candidates,
        unverified_stock_facts=unverified_facts,
        theme_ranking_facts=extract_hhxg_theme_facts(snapshot),
    )


# 仅六位纯数字代码可进入待确认入池候选；重复代码保留第一条快照事实，避免重复展示。
def extract_stock_candidates(
    snapshot: EvidenceSnapshot,
) -> tuple[tuple[VerifiedStockCandidate, ...], tuple[UnverifiedStockFact, ...]]:
    verified_candidates: list[VerifiedStockCandidate] = []
    unverified_facts: list[UnverifiedStockFact] = []
    seen_codes: set[str] = set()
    seen_unverified: set[tuple[str, str, str]] = set()
    for stock in snapshot.stocks:
        code = normalize_text(stock.get("code"))
        name = normalize_text(stock.get("name"))
        sector = normalize_text(stock.get("sector"))
        role = normalize_text(stock.get("role"))
        evidence_source = normalize_text(stock.get("source")) or snapshot.field_sources.get("stocks", "待确认")
        reason = normalize_text(stock.get("reason"))
        if is_confirmed_stock_code(code) and name and code not in seen_codes:
            seen_codes.add(code)
            verified_candidates.append(
                VerifiedStockCandidate(
                    code=code,
                    name=name,
                    exchange=normalize_text(stock.get("exchange")) or "待确认",
                    sector=sector or "待确认",
                    role=role or "待确认",
                    role_source=normalize_text(stock.get("role_source")) or "待确认",
                    evidence_source=evidence_source,
                    change_percent=normalize_text(stock.get("change_percent")) or "待确认",
                    reason=reason or "待确认",
                )
            )
            continue
        if name:
            unverified_key = (name, sector, role)
            if unverified_key not in seen_unverified:
                seen_unverified.add(unverified_key)
                unverified_facts.append(
                    UnverifiedStockFact(
                        name=name,
                        sector=sector or "待确认",
                        role=role or "待确认",
                        evidence_source=evidence_source,
                        reason=reason or "待确认",
                    )
                )
    return tuple(verified_candidates), tuple(unverified_facts)


# hhxg 排行只作为主题或行业事实展示，领涨名称缺少确认代码时不得转成入池候选。
def extract_hhxg_theme_facts(snapshot: EvidenceSnapshot) -> tuple[ThemeRankingFact, ...]:
    facts: list[ThemeRankingFact] = []
    for sector in snapshot.sectors:
        source_type = normalize_text(sector.get("source_type"))
        name = normalize_text(sector.get("name"))
        if not name or not source_type.startswith("hhxg_"):
            continue
        facts.append(
            ThemeRankingFact(
                name=name,
                source_type=source_type,
                rank=normalize_text(sector.get("rank")) or "待确认",
                limit_up_count=normalize_text(sector.get("limit_up_count")) or "待确认",
                net_inflow_yi=normalize_text(sector.get("net_inflow_yi")) or "待确认",
                leading_stock=format_leading_stock(sector.get("leading_stock")),
            )
        )
    return tuple(facts)


def render_pool_candidate_summary(summary: PoolCandidateSummary) -> str:
    lines = [
        f"# {summary.trade_date} 池子候选事实",
        "",
        f"- 样本日期：{summary.sample_date}",
        "- 使用边界：本命令只整理数据源事实候选，不自动写入关注池/热点池，不认定核心票、龙头或买点。",
        "",
        "## 可确认代码的个股事实候选",
        "",
    ]
    if summary.verified_stock_candidates:
        for candidate in summary.verified_stock_candidates:
            lines.append(
                f"- {candidate.code} {candidate.name}｜交易所：{candidate.exchange}｜板块：{candidate.sector}｜"
                f"角色：{candidate.role}｜角色来源：{candidate.role_source}｜涨跌幅：{candidate.change_percent}%｜"
                f"事实来源：{candidate.evidence_source}｜原因：{candidate.reason}"
            )
    else:
        lines.append("- 当前快照没有可确认六码代码的个股事实候选。")

    lines.extend(["", "## 缺少确认代码的个股事实", ""])
    if summary.unverified_stock_facts:
        for fact in summary.unverified_stock_facts:
            lines.append(
                f"- {fact.name}｜板块：{fact.sector}｜角色：{fact.role}｜"
                f"事实来源：{fact.evidence_source}｜原因：{fact.reason}"
            )
    else:
        lines.append("- 无。")

    lines.extend(["", "## hhxg 题材与行业排行事实", ""])
    if summary.theme_ranking_facts:
        for fact in summary.theme_ranking_facts:
            lines.append(
                f"- {fact.name}｜类型：{fact.source_type}｜排名：{fact.rank}｜涨停家数：{fact.limit_up_count}｜"
                f"净流入：{fact.net_inflow_yi} 亿｜领涨名称：{fact.leading_stock}"
            )
    else:
        lines.append("- 当前快照没有 hhxg 题材或行业排行事实。")
    return "\n".join(lines) + "\n"


def is_confirmed_stock_code(code: str) -> bool:
    return len(code) == 6 and code.isdigit()


def normalize_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def format_leading_stock(value: Any) -> str:
    if isinstance(value, list):
        names = [normalize_text(item.get("name")) for item in value if isinstance(item, dict)]
        return "、".join(name for name in names if name) or "待确认"
    return normalize_text(value) or "待确认"
