# 本文件负责汇总本地 Evidence Snapshot 中的板块历史事实，不调用外部数据源或认定核心板块。

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
from pathlib import Path
from typing import Any

from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import EvidenceSnapshotError, read_json_mapping


MINIMUM_CORE_SECTOR_DAYS = 5
DEFAULT_SECTOR_HISTORY_OUTPUT_DIR = Path("reports") / "daily"
DEFAULT_LOG_PATH = Path("logs") / "stock_review.log"


@dataclass(frozen=True)
class DailyStrongestSector:
    trade_date: str
    name: str
    change_percent: float
    source_type: str
    leading_stock: str


@dataclass(frozen=True)
class RepeatedSectorCandidate:
    name: str
    occurrence_count: int
    positive_count: int
    cumulative_change_percent: float
    latest_trade_date: str
    latest_change_percent: float | None


@dataclass(frozen=True)
class SectorHistorySummary:
    start_date: str
    end_date: str
    snapshot_dates: tuple[str, ...]
    sector_evidence_dates: tuple[str, ...]
    missing_sector_dates: tuple[str, ...]
    daily_strongest: tuple[DailyStrongestSector, ...]
    repeated_candidates: tuple[RepeatedSectorCandidate, ...]

    @property
    def has_enough_history(self) -> bool:
        return len(self.sector_evidence_dates) >= MINIMUM_CORE_SECTOR_DAYS


# 历史汇总只读取明确日期范围内的本地快照，避免把样例输入或范围外数据混入判断。
def summarize_sector_history(
    start_date: str,
    end_date: str,
    snapshot_dir: Path,
) -> SectorHistorySummary:
    try:
        normalized_start = date.fromisoformat(start_date)
        normalized_end = date.fromisoformat(end_date)
    except ValueError as error:
        raise EvidenceSnapshotError("板块历史日期格式必须为 YYYY-MM-DD。") from error
    if normalized_start > normalized_end:
        raise EvidenceSnapshotError("板块历史开始日期不能晚于结束日期。")
    if not snapshot_dir.exists():
        raise EvidenceSnapshotError(f"证据快照目录不存在：{snapshot_dir}")

    snapshots: list[tuple[str, list[dict[str, Any]]]] = []
    for snapshot_path in sorted(snapshot_dir.glob("*_snapshot.json")):
        raw_data = read_json_mapping(snapshot_path)
        trade_date_value = str(raw_data.get("trade_date") or snapshot_path.name.removesuffix("_snapshot.json"))
        try:
            snapshot_date = date.fromisoformat(trade_date_value)
        except ValueError as error:
            raise EvidenceSnapshotError(f"证据快照交易日期格式无效：{snapshot_path}：{trade_date_value}") from error
        if normalized_start <= snapshot_date <= normalized_end:
            snapshot = build_evidence_snapshot(trade_date_value, raw_data)
            snapshots.append((snapshot.trade_date, snapshot.sectors))

    if not snapshots:
        raise EvidenceSnapshotError(
            f"指定日期范围内没有证据快照：{start_date} 至 {end_date}，目录：{snapshot_dir}"
        )

    sector_evidence_dates = tuple(trade_date for trade_date, sectors in snapshots if sectors)
    missing_sector_dates = tuple(trade_date for trade_date, sectors in snapshots if not sectors)
    daily_strongest = tuple(build_daily_strongest(trade_date, sectors) for trade_date, sectors in snapshots if sectors)
    daily_strongest = tuple(item for item in daily_strongest if item is not None)
    repeated_candidates = (
        build_repeated_candidates(snapshots) if len(sector_evidence_dates) >= MINIMUM_CORE_SECTOR_DAYS else ()
    )

    return SectorHistorySummary(
        start_date=normalized_start.isoformat(),
        end_date=normalized_end.isoformat(),
        snapshot_dates=tuple(trade_date for trade_date, _ in snapshots),
        sector_evidence_dates=sector_evidence_dates,
        missing_sector_dates=missing_sector_dates,
        daily_strongest=daily_strongest,
        repeated_candidates=repeated_candidates,
    )


# 当日最强板块只按快照已有涨跌幅事实排序，不补写缺失字段或推断持续性。
def build_daily_strongest(
    trade_date: str,
    sectors: list[dict[str, Any]],
) -> DailyStrongestSector | None:
    comparable_items = [
        (sector, parse_number(sector.get("change_percent")))
        for sector in sectors
        if str(sector.get("name") or "").strip()
    ]
    comparable_items = [(sector, value) for sector, value in comparable_items if value is not None]
    if not comparable_items:
        return None
    sector, change_percent = max(comparable_items, key=lambda item: item[1])
    return DailyStrongestSector(
        trade_date=trade_date,
        name=str(sector.get("name") or "待确认"),
        change_percent=change_percent,
        source_type=str(sector.get("source_type") or "待确认"),
        leading_stock=str(sector.get("leading_stock") or "待确认"),
    )


# 近期候选只统计多日重复出现的板块事实，不使用“核心板块”确定性标签。
def build_repeated_candidates(
    snapshots: list[tuple[str, list[dict[str, Any]]]],
) -> tuple[RepeatedSectorCandidate, ...]:
    aggregated: dict[str, dict[str, Any]] = {}
    for trade_date, sectors in snapshots:
        seen_names: set[str] = set()
        for sector in sectors:
            name = str(sector.get("name") or "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            change_percent = parse_number(sector.get("change_percent"))
            state = aggregated.setdefault(
                name,
                {
                    "occurrence_count": 0,
                    "positive_count": 0,
                    "cumulative_change_percent": 0.0,
                    "latest_trade_date": trade_date,
                    "latest_change_percent": None,
                },
            )
            state["occurrence_count"] += 1
            if change_percent is not None:
                state["cumulative_change_percent"] += change_percent
                if change_percent > 0:
                    state["positive_count"] += 1
            state["latest_trade_date"] = trade_date
            state["latest_change_percent"] = change_percent

    candidates = [
        RepeatedSectorCandidate(name=name, **state)
        for name, state in aggregated.items()
        if state["occurrence_count"] >= 2
    ]
    candidates.sort(
        key=lambda item: (
            -item.occurrence_count,
            -item.positive_count,
            -item.cumulative_change_percent,
            item.name,
        )
    )
    return tuple(candidates)


def render_sector_history(summary: SectorHistorySummary) -> str:
    lines = [
        f"# {summary.start_date} 至 {summary.end_date} 板块历史事实",
        "",
        f"- 快照日期数：{len(summary.snapshot_dates)}",
        f"- 有效板块证据日期数：{len(summary.sector_evidence_dates)}",
        f"- 近期候选最小历史要求：{MINIMUM_CORE_SECTOR_DAYS} 个有效板块交易日",
        "- 结论边界：本报告只汇总板块事实和反复活跃候选，不认定核心板块、主升阶段或买点。",
        "",
        "## 当日最强板块事实",
        "",
    ]
    if summary.daily_strongest:
        for item in summary.daily_strongest:
            lines.append(
                f"- {item.trade_date}｜{item.name}｜涨跌幅：{format_number(item.change_percent)}%｜"
                f"来源类型：{item.source_type}｜领涨股：{item.leading_stock}"
            )
    else:
        lines.append("- 当前日期范围内没有可按涨跌幅比较的板块事实。")

    lines.extend(["", "## 板块证据缺口", ""])
    if summary.missing_sector_dates:
        for trade_date in summary.missing_sector_dates:
            lines.append(f"- {trade_date}：missing_sectors。")
    else:
        lines.append("- 已读取快照均包含板块证据。")

    lines.extend(["", "## 近期反复活跃候选", ""])
    if not summary.has_enough_history:
        lines.append(
            f"- 数据不足：当前只有 {len(summary.sector_evidence_dates)} 个有效板块交易日，"
            f"至少需要 {MINIMUM_CORE_SECTOR_DAYS} 个交易日；不得认定近期核心板块。"
        )
    elif not summary.repeated_candidates:
        lines.append("- 当前有效历史中没有至少重复出现 2 次的板块，不输出近期候选。")
    else:
        for candidate in summary.repeated_candidates:
            latest_change = (
                "待确认"
                if candidate.latest_change_percent is None
                else f"{format_number(candidate.latest_change_percent)}%"
            )
            lines.append(
                f"- {candidate.name}｜出现：{candidate.occurrence_count} 日｜"
                f"上涨：{candidate.positive_count} 日｜累计涨跌幅："
                f"{format_number(candidate.cumulative_change_percent)}%｜"
                f"最近日期：{candidate.latest_trade_date}｜最近涨跌幅：{latest_change}"
            )
    return "\n".join(lines).rstrip() + "\n"


def create_sector_history_report(
    start_date: str,
    end_date: str,
    snapshot_dir: Path,
    output_dir: Path = DEFAULT_SECTOR_HISTORY_OUTPUT_DIR,
    log_path: Path = DEFAULT_LOG_PATH,
) -> tuple[Path, SectorHistorySummary]:
    summary = summarize_sector_history(start_date, end_date, snapshot_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{summary.end_date}_sector_history.md"
    output_path.write_text(render_sector_history(summary), encoding="utf-8")
    write_sector_history_log(summary, snapshot_dir, output_path, log_path)
    return output_path, summary


def write_sector_history_log(
    summary: SectorHistorySummary,
    snapshot_dir: Path,
    output_path: Path,
    log_path: Path,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.sector_history")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=evidence sector-history start_date=%s end_date=%s snapshot_dir=%s output=%s "
            "snapshot_days=%s sector_days=%s status=created",
            summary.start_date,
            summary.end_date,
            snapshot_dir,
            output_path,
            len(summary.snapshot_dates),
            len(summary.sector_evidence_dates),
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


def format_number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")
