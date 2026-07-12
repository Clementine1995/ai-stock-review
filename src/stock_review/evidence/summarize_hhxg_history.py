# 本文件负责汇总本地 hhxg 历史事实，不调用外部数据源、不判断情绪周期或生成交易建议。

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import logging
from typing import Any

from stock_review.evidence.check_history_readiness import (
    MINIMUM_HHXG_HISTORY_DAYS,
    check_hhxg_history_readiness,
    has_hhxg_event,
    parse_date_range,
)
from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import EvidenceSnapshotError, read_json_mapping


DEFAULT_HHXG_HISTORY_OUTPUT_DIR = Path("reports") / "daily"
DEFAULT_LOG_PATH = Path("logs") / "stock_review.log"


@dataclass(frozen=True)
class HhxgDailyFact:
    trade_date: str
    limit_up_count: float | None
    limit_down_count: float | None
    broken_board_rate: float | None
    highest_board: float | None
    hot_theme_count: int
    strong_sector_count: int
    weak_sector_count: int
    sentiment_source_confirmed: bool


@dataclass(frozen=True)
class HhxgRepeatedRankFact:
    name: str
    source_type: str
    occurrence_count: int
    cumulative_net_inflow_yi: float
    latest_trade_date: str
    latest_rank: int | None


@dataclass(frozen=True)
class HhxgHistorySummary:
    start_date: str
    end_date: str
    daily_facts: tuple[HhxgDailyFact, ...]
    missing_hhxg_dates: tuple[str, ...]
    repeated_rank_facts: tuple[HhxgRepeatedRankFact, ...]

    @property
    def has_enough_history(self) -> bool:
        return len(self.daily_facts) >= MINIMUM_HHXG_HISTORY_DAYS


# 历史汇总只使用日期已核验且带 hhxg 事件的快照；其它来源的同日事实不混入本报告。
def summarize_hhxg_history(start_date: str, end_date: str, snapshot_dir: Path) -> HhxgHistorySummary:
    normalized_start, normalized_end = parse_date_range(start_date, end_date)
    readiness = check_hhxg_history_readiness(start_date, end_date, snapshot_dir)
    if not readiness.valid_days and not readiness.snapshot_dates_without_valid_hhxg:
        raise EvidenceSnapshotError(
            f"指定日期范围内没有证据快照：{start_date} 至 {end_date}，目录：{snapshot_dir}"
        )

    daily_facts: list[HhxgDailyFact] = []
    ranked_records: list[tuple[str, dict[str, Any]]] = []
    for snapshot_path in sorted(snapshot_dir.glob("*_snapshot.json")):
        raw_data = read_json_mapping(snapshot_path)
        trade_date = str(raw_data.get("trade_date") or snapshot_path.name.removesuffix("_snapshot.json"))
        try:
            parsed_trade_date = date.fromisoformat(trade_date)
        except ValueError as error:
            raise EvidenceSnapshotError(f"证据快照交易日期格式无效：{snapshot_path}：{trade_date}") from error
        if not normalized_start <= parsed_trade_date <= normalized_end:
            continue

        snapshot = build_evidence_snapshot(trade_date, raw_data)
        if snapshot.sample_date != trade_date or not has_hhxg_event(snapshot.events):
            continue
        sectors = [item for item in snapshot.sectors if str(item.get("source_type") or "").startswith("hhxg_")]
        daily_facts.append(
            build_daily_fact(trade_date, snapshot.field_sources.get("sentiment"), snapshot.sentiment, sectors)
        )
        ranked_records.extend((trade_date, item) for item in sectors)

    repeated_rank_facts = build_repeated_rank_facts(ranked_records) if len(daily_facts) >= MINIMUM_HHXG_HISTORY_DAYS else ()
    return HhxgHistorySummary(
        start_date=normalized_start.isoformat(),
        end_date=normalized_end.isoformat(),
        daily_facts=tuple(daily_facts),
        missing_hhxg_dates=readiness.snapshot_dates_without_valid_hhxg,
        repeated_rank_facts=repeated_rank_facts,
    )


# 情绪字段只有在顶层来源仍为 hhxg 时才纳入汇总，避免同日合并后的字段来源被错误归因。
def build_daily_fact(
    trade_date: str,
    sentiment_source: str | None,
    sentiment: dict[str, Any],
    sectors: list[dict[str, Any]],
) -> HhxgDailyFact:
    sentiment_source_confirmed = sentiment_source == "hhxg"
    source_type_counts = {source_type: 0 for source_type in ("hhxg_hot_theme", "hhxg_strong_sector", "hhxg_weak_sector")}
    for sector in sectors:
        source_type = str(sector.get("source_type") or "")
        if source_type in source_type_counts:
            source_type_counts[source_type] += 1
    return HhxgDailyFact(
        trade_date=trade_date,
        limit_up_count=parse_number(sentiment.get("limit_up_count")) if sentiment_source_confirmed else None,
        limit_down_count=parse_number(sentiment.get("limit_down_count")) if sentiment_source_confirmed else None,
        broken_board_rate=parse_number(sentiment.get("broken_board_rate")) if sentiment_source_confirmed else None,
        highest_board=parse_number(sentiment.get("highest_board")) if sentiment_source_confirmed else None,
        hot_theme_count=source_type_counts["hhxg_hot_theme"],
        strong_sector_count=source_type_counts["hhxg_strong_sector"],
        weak_sector_count=source_type_counts["hhxg_weak_sector"],
        sentiment_source_confirmed=sentiment_source_confirmed,
    )


# 重复出现只按 hhxg 的原始排行条目计数，净流入为公开字段累计，不解释为资金流向结论。
def build_repeated_rank_facts(
    ranked_records: list[tuple[str, dict[str, Any]]],
) -> tuple[HhxgRepeatedRankFact, ...]:
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    seen_records: set[tuple[str, str, str]] = set()
    for trade_date, record in ranked_records:
        name = str(record.get("name") or "").strip()
        source_type = str(record.get("source_type") or "")
        record_key = (trade_date, source_type, name)
        if not name or record_key in seen_records:
            continue
        seen_records.add(record_key)
        state = aggregated.setdefault(
            (source_type, name),
            {"occurrence_count": 0, "cumulative_net_inflow_yi": 0.0, "latest_trade_date": trade_date, "latest_rank": None},
        )
        state["occurrence_count"] += 1
        state["cumulative_net_inflow_yi"] += parse_number(record.get("net_inflow_yi")) or 0.0
        state["latest_trade_date"] = trade_date
        rank_value = parse_number(record.get("rank"))
        state["latest_rank"] = int(rank_value) if rank_value is not None else None

    facts = [
        HhxgRepeatedRankFact(name=name, source_type=source_type, **state)
        for (source_type, name), state in aggregated.items()
        if state["occurrence_count"] >= 2
    ]
    facts.sort(key=lambda item: (-item.occurrence_count, item.latest_rank or 9999, item.source_type, item.name))
    return tuple(facts)


def render_hhxg_history(summary: HhxgHistorySummary) -> str:
    lines = [
        f"# {summary.start_date} 至 {summary.end_date} hhxg 历史事实",
        "",
        f"- 已验证有效交易日：{len(summary.daily_facts)} / {MINIMUM_HHXG_HISTORY_DAYS}",
        "- 结论边界：只汇总 hhxg 已有事实，不判断情绪周期、主线、核心板块或交易方向。",
        "",
        "## 每日 hhxg 事实",
        "",
    ]
    if summary.daily_facts:
        for fact in summary.daily_facts:
            if fact.sentiment_source_confirmed:
                sentiment_text = (
                    f"涨停：{format_number(fact.limit_up_count)}｜跌停：{format_number(fact.limit_down_count)}｜"
                    f"炸板率：{format_number(fact.broken_board_rate)}｜连板高度：{format_number(fact.highest_board)}"
                )
            else:
                sentiment_text = "情绪字段来源待确认（同日快照顶层来源已被其它数据源覆盖）"
            lines.append(
                f"- {fact.trade_date}｜{sentiment_text}｜热门题材：{fact.hot_theme_count} 条｜"
                f"强势行业：{fact.strong_sector_count} 条｜弱势行业：{fact.weak_sector_count} 条"
            )
    else:
        lines.append("- 当前日期范围内没有已验证 hhxg 事实。")

    lines.extend(["", "## 未计入 hhxg 历史的本地快照", ""])
    if summary.missing_hhxg_dates:
        lines.extend(f"- {trade_date}：没有满足 hhxg 有效条件的快照。" for trade_date in summary.missing_hhxg_dates)
    else:
        lines.append("- 无。")

    lines.extend(["", "## 多日重复出现的排行事实", ""])
    if not summary.has_enough_history:
        lines.append(
            f"- 数据不足：当前只有 {len(summary.daily_facts)} 个有效交易日，至少需要 "
            f"{MINIMUM_HHXG_HISTORY_DAYS} 个交易日；不得输出多日历史分析结论。"
        )
    elif not summary.repeated_rank_facts:
        lines.append("- 当前有效历史中没有重复出现至少 2 次的排行事实。")
    else:
        for fact in summary.repeated_rank_facts:
            lines.append(
                f"- {fact.name}｜类型：{fact.source_type}｜出现：{fact.occurrence_count} 日｜"
                f"累计净流入：{format_number(fact.cumulative_net_inflow_yi)} 亿｜"
                f"最近日期：{fact.latest_trade_date}｜最近排名：{fact.latest_rank or '待确认'}"
            )
    return "\n".join(lines) + "\n"


def create_hhxg_history_report(
    start_date: str,
    end_date: str,
    snapshot_dir: Path,
    output_dir: Path = DEFAULT_HHXG_HISTORY_OUTPUT_DIR,
    log_path: Path = DEFAULT_LOG_PATH,
) -> tuple[Path, HhxgHistorySummary]:
    summary = summarize_hhxg_history(start_date, end_date, snapshot_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{summary.end_date}_hhxg_history.md"
    output_path.write_text(render_hhxg_history(summary), encoding="utf-8")
    write_hhxg_history_log(summary, snapshot_dir, output_path, log_path)
    return output_path, summary


def write_hhxg_history_log(
    summary: HhxgHistorySummary,
    snapshot_dir: Path,
    output_path: Path,
    log_path: Path,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.hhxg_history")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=evidence hhxg-history start_date=%s end_date=%s snapshot_dir=%s output=%s "
            "valid_days=%s status=created",
            summary.start_date,
            summary.end_date,
            snapshot_dir,
            output_path,
            len(summary.daily_facts),
        )
    finally:
        logger.removeHandler(handler)
        handler.close()


def parse_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_number(value: float | None) -> str:
    if value is None:
        return "待确认"
    return f"{value:.4f}".rstrip("0").rstrip(".")
