# 本文件负责市场状态和板块强度的可解释评分，不预测涨跌或输出买卖建议。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stock_review.evidence.evidence_snapshot import EvidenceSnapshot, build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import read_json_mapping
from stock_review.scoring.match_trade_patterns import match_stock_patterns


@dataclass(frozen=True)
class ScoreResult:
    name: str
    score: float | None
    label: str
    coverage: int
    evidence_lines: tuple[str, ...]
    missing_fields: tuple[str, ...]


# 市场评分四项等权；缺指数或核心情绪字段时直接不可判定，避免用残缺证据给出状态。
def score_market_state(snapshot: EvidenceSnapshot) -> ScoreResult:
    indices = snapshot.market.get("indices")
    sentiment = snapshot.sentiment
    required_sentiment = ("limit_up_count", "limit_down_count", "broken_board_rate", "highest_board")
    missing: list[str] = []
    if not isinstance(indices, list) or not indices:
        missing.append("market.indices")
    for field_name in required_sentiment:
        if sentiment.get(field_name) in (None, ""):
            missing.append(f"sentiment.{field_name}")
    if missing:
        return ScoreResult("市场状态", None, "不可判定", 0, (), tuple(missing))

    changes = [float(item["change_percent"]) for item in indices if item.get("change_percent") not in (None, "")]
    if not changes:
        return ScoreResult("市场状态", None, "不可判定", 0, (), ("market.indices.change_percent",))
    limit_up = float(sentiment["limit_up_count"])
    limit_down = float(sentiment["limit_down_count"])
    total_limits = limit_up + limit_down
    positive_ratio = sum(change > 0 for change in changes) / len(changes)
    limit_ratio = limit_up / total_limits if total_limits else 0
    broken_rate = min(max(float(sentiment["broken_board_rate"]), 0), 1)
    highest_board = max(float(sentiment["highest_board"]), 0)
    components = (
        ("指数正收益占比", positive_ratio, positive_ratio * 25),
        ("涨停占涨跌停比例", limit_ratio, limit_ratio * 25),
        ("封板稳定度", 1 - broken_rate, (1 - broken_rate) * 25),
        ("连板高度达标度", min(highest_board / 5, 1), min(highest_board / 5, 1) * 25),
    )
    score = round(sum(component[2] for component in components), 2)
    label = "偏强" if score >= 70 else "中性" if score >= 40 else "偏弱"
    evidence_lines = tuple(
        f"{name}={value:.4f}，得分={points:.2f}/25" for name, value, points in components
    )
    return ScoreResult("市场状态", score, label, 100, evidence_lines, ())


# 板块评分使用四项显式规则；覆盖率低于 50% 时不输出强弱标签。
def score_sector_strength(sector: dict[str, object]) -> ScoreResult:
    points = 0
    available = 0
    evidence: list[str] = []
    missing: list[str] = []

    change = sector.get("change_percent")
    if change in (None, ""):
        missing.append("change_percent")
    else:
        available += 40
        value = float(change)
        earned = 40 if value > 2 else 25 if value > 0 else 15 if value == 0 else 0
        points += earned
        evidence.append(f"change_percent={value}，得分={earned}/40")

    up_count = sector.get("up_count")
    down_count = sector.get("down_count")
    if up_count in (None, "") or down_count in (None, ""):
        missing.append("up_count/down_count")
    else:
        available += 30
        total = float(up_count) + float(down_count)
        ratio = float(up_count) / total if total else 0
        earned = 30 if ratio >= 0.7 else 20 if ratio >= 0.5 else 0
        points += earned
        evidence.append(f"up_ratio={ratio:.4f}，得分={earned}/30")

    net_inflow = sector.get("net_inflow")
    if net_inflow in (None, ""):
        missing.append("net_inflow")
    else:
        available += 20
        value = float(net_inflow)
        earned = 20 if value > 0 else 0
        points += earned
        evidence.append(f"net_inflow={value}，得分={earned}/20")

    leader_change = sector.get("leading_stock_change_percent")
    if leader_change in (None, ""):
        missing.append("leading_stock_change_percent")
    else:
        available += 10
        value = float(leader_change)
        earned = 10 if value > 0 else 0
        points += earned
        evidence.append(f"leading_stock_change_percent={value}，得分={earned}/10")

    coverage = available
    score = round(points / available * 100, 2) if available else None
    if coverage < 50 or score is None:
        label = "不可判定"
    else:
        label = "强" if score >= 75 else "中" if score >= 50 else "弱"
    return ScoreResult(
        str(sector.get("name") or "待确认板块"),
        score,
        label,
        coverage,
        tuple(evidence),
        tuple(missing),
    )


def create_scoring_report(trade_date: str, evidence_path: Path, output_dir: Path) -> Path:
    snapshot = build_evidence_snapshot(trade_date, read_json_mapping(evidence_path))
    market_result = score_market_state(snapshot)
    sector_results = [score_sector_strength(sector) for sector in snapshot.sectors]
    stock_results = match_stock_patterns(snapshot)
    lines = [
        f"# {trade_date} 可解释规则评分",
        "",
        f"- 证据来源：{snapshot.source}",
        f"- 样本日期：{snapshot.sample_date}",
        "- 边界：评分、角色标签和疑似模式只辅助复盘，不预测涨跌，不构成买卖建议。",
        "",
        "## 市场状态",
        "",
        *render_score_result(market_result),
        "## 板块强度",
        "",
    ]
    if not sector_results:
        lines.extend(["- 不可判定：缺少 sectors。", ""])
    for result in sector_results:
        lines.extend([f"### {result.name}", "", *render_score_result(result)])
    lines.extend(["## 个股角色与疑似观察模式", ""])
    if not stock_results:
        lines.extend(["- 不可判定：缺少 stocks。", ""])
    for result in stock_results:
        lines.extend(
            [
                f"### {result.code} {result.name}",
                "",
                f"- 板块：{result.sector}",
                f"- 角色标签：{', '.join(result.role_tags) if result.role_tags else '无'}",
                f"- 疑似模式：{', '.join(result.pattern_matches) if result.pattern_matches else '无'}",
                "- 边界：疑似模式不是确认买点，需要人工结合竞价、分时、前一日反馈和交易计划条件。",
            ]
        )
        lines.extend(f"- 规则证据：{line}" for line in result.evidence_lines)
        lines.extend(f"- 缺口：{field}" for field in result.missing_fields)
        lines.append("")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{trade_date}_scoring.md"
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return output_path


def render_score_result(result: ScoreResult) -> list[str]:
    score_text = "不可判定" if result.score is None else str(result.score)
    lines = [
        f"- 得分：{score_text}",
        f"- 标签：{result.label}",
        f"- 证据覆盖率：{result.coverage}%",
    ]
    lines.extend(f"- 规则证据：{line}" for line in result.evidence_lines)
    lines.extend(f"- 缺口：{field}" for field in result.missing_fields)
    return [*lines, ""]
