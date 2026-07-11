# 本文件负责基于 Evidence Snapshot 已有个股事实生成角色标签和疑似观察模式，不输出买卖建议。

from __future__ import annotations

from dataclasses import dataclass
import re

from stock_review.evidence.evidence_snapshot import EvidenceSnapshot


@dataclass(frozen=True)
class StockPatternResult:
    code: str
    name: str
    sector: str
    role_tags: tuple[str, ...]
    pattern_matches: tuple[str, ...]
    evidence_lines: tuple[str, ...]
    missing_fields: tuple[str, ...]


# 个股标签只从数据源已给出的角色事实提取，禁止根据有限证据升级为核心票或龙头。
def match_stock_patterns(snapshot: EvidenceSnapshot) -> list[StockPatternResult]:
    sector_by_name = {str(sector.get("name")): sector for sector in snapshot.sectors if sector.get("name")}
    return [match_single_stock(stock, sector_by_name, snapshot.sentiment) for stock in snapshot.stocks]


# 单个个股的疑似模式必须同时输出证据和缺口，避免把弱证据写成确定买点。
def match_single_stock(
    stock: dict[str, object],
    sector_by_name: dict[str, dict[str, object]],
    sentiment: dict[str, object],
) -> StockPatternResult:
    code = str(stock.get("code") or "待确认")
    name = str(stock.get("name") or "待确认")
    sector_name = str(stock.get("sector") or "待确认")
    source = str(stock.get("source") or "")
    role_source = str(stock.get("role_source") or stock.get("role") or "")
    role_tags = build_role_tags(source, role_source, sentiment)
    pattern_matches: list[str] = []
    evidence_lines = [
        f"来源={source or '待确认'}",
        f"角色来源={role_source or '待确认'}",
        f"涨跌幅={stock.get('change_percent', '待确认')}",
    ]
    missing_fields = build_stock_missing_fields(stock)

    sector = sector_by_name.get(sector_name)
    if "板块领涨" in role_tags:
        if sector:
            # 这里延迟引用板块评分，避免评分报告入口和个股匹配模块形成导入环。
            from stock_review.scoring.score_market_state import score_sector_strength

            sector_result = score_sector_strength(sector)
            evidence_lines.append(f"板块强度标签={sector_result.label}，得分={sector_result.score}")
            if sector_result.score is not None and sector_result.score >= 50:
                pattern_matches.append("板块领涨疑似观察模式")
        else:
            missing_fields.append("sector.evidence")

    board_count = extract_board_count(role_source)
    if "连板股" in role_tags:
        evidence_lines.append(f"连板数={board_count if board_count is not None else '待确认'}")
        if board_count is not None and board_count >= 2:
            pattern_matches.append("连板接力疑似观察模式")

    if pattern_matches:
        missing_fields.extend(
            [
                "call_auction.evidence",
                "intraday.evidence",
                "previous_day_feedback.evidence",
            ]
        )

    return StockPatternResult(
        code=code,
        name=name,
        sector=sector_name,
        role_tags=tuple(role_tags),
        pattern_matches=tuple(pattern_matches),
        evidence_lines=tuple(evidence_lines),
        missing_fields=tuple(dict.fromkeys(missing_fields)),
    )


def build_role_tags(source: str, role_source: str, sentiment: dict[str, object]) -> list[str]:
    tags: list[str] = []
    if source == "akshare:sector_leading_stock" or "板块领涨" in role_source:
        tags.append("板块领涨")
    board_count = extract_board_count(role_source)
    if "连板" in role_source and board_count is not None and board_count >= 2:
        tags.append("连板股")
    highest_board = sentiment.get("highest_board")
    if board_count is not None and highest_board not in (None, "") and board_count == int(float(highest_board)):
        tags.append("连板高度事实候选")
    return tags


def extract_board_count(role_source: str) -> int | None:
    matched = re.search(r"（(\d+)板）|\((\d+)板\)", role_source)
    if not matched:
        return None
    value = matched.group(1) or matched.group(2)
    return int(value)


def build_stock_missing_fields(stock: dict[str, object]) -> list[str]:
    missing: list[str] = []
    if stock.get("code") in (None, "", "待确认"):
        missing.append("stock.code")
    if stock.get("exchange") in (None, "", "待确认"):
        missing.append("stock.exchange")
    if stock.get("sector") in (None, "", "待确认"):
        missing.append("stock.sector")
    return missing
