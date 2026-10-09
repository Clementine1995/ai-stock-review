# 本文件负责汇总本地 Evidence Snapshot 中的市场层历史事实，不判断市场阶段或生成交易建议。

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
from pathlib import Path
from typing import Any

from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import EvidenceSnapshotError, read_json_mapping
from stock_review.evidence.tongdaxin_source import TONGDAXIN_INDEX_HISTORY_QUALITY


MINIMUM_MARKET_HISTORY_DAYS = 5
DEFAULT_MARKET_HISTORY_OUTPUT_DIR = Path("reports") / "daily"
DEFAULT_LOG_PATH = Path("logs") / "stock_review.log"


@dataclass(frozen=True)
class MarketDailyFact:
    trade_date: str
    sample_date: str
    indices: tuple[dict[str, Any], ...]
    total_amount: float | None
    total_amount_source: str


@dataclass(frozen=True)
class MarketIndexRangeFact:
    name: str
    observation_count: int
    start_date: str
    start_close: float
    end_date: str
    end_close: float
    cumulative_change_percent: float


@dataclass(frozen=True)
class MarketHistorySummary:
    start_date: str
    end_date: str
    snapshot_dates: tuple[str, ...]
    market_facts: tuple[MarketDailyFact, ...]
    missing_market_dates: tuple[str, ...]
    index_range_facts: tuple[MarketIndexRangeFact, ...]
    sequence_basis: str
    evidence_references: tuple[str, ...]

    @property
    def has_enough_history(self) -> bool:
        return len(self.market_facts) >= MINIMUM_MARKET_HISTORY_DAYS

    @property
    def has_verified_daily_sequence(self) -> bool:
        return (
            self.sequence_basis == TONGDAXIN_INDEX_HISTORY_QUALITY
            and self.has_enough_history
            and bool(self.index_range_facts)
        )


# 市场历史只读取本地快照中已有的指数和成交额事实，交易日和样本日期分别保留。
def summarize_market_history(
    start_date: str,
    end_date: str,
    snapshot_dir: Path,
) -> MarketHistorySummary:
    try:
        normalized_start = date.fromisoformat(start_date)
        normalized_end = date.fromisoformat(end_date)
    except ValueError as error:
        raise EvidenceSnapshotError("市场历史日期格式必须为 YYYY-MM-DD。") from error
    if normalized_start > normalized_end:
        raise EvidenceSnapshotError("市场历史开始日期不能晚于结束日期。")
    if not snapshot_dir.exists():
        raise EvidenceSnapshotError(f"证据快照目录不存在：{snapshot_dir}")

    snapshot_dates: list[str] = []
    market_facts: list[MarketDailyFact] = []
    missing_market_dates: list[str] = []
    evidence_references: list[str] = []
    for snapshot_path in sorted(snapshot_dir.glob("*_snapshot.json")):
        raw_data = read_json_mapping(snapshot_path)
        trade_date = str(raw_data.get("trade_date") or snapshot_path.name.removesuffix("_snapshot.json"))
        try:
            parsed_trade_date = date.fromisoformat(trade_date)
        except ValueError as error:
            raise EvidenceSnapshotError(f"证据快照交易日期格式无效：{snapshot_path}：{trade_date}") from error
        if not normalized_start <= parsed_trade_date <= normalized_end:
            continue

        snapshot_dates.append(trade_date)
        snapshot = build_evidence_snapshot(trade_date, raw_data)
        indices = snapshot.market.get("indices")
        if not isinstance(indices, list) or not indices:
            missing_market_dates.append(trade_date)
            continue
        market_facts.append(
            MarketDailyFact(
                trade_date=trade_date,
                sample_date=snapshot.sample_date,
                indices=tuple(item for item in indices if isinstance(item, dict)),
                total_amount=parse_number(snapshot.market.get("total_amount")),
                total_amount_source=str(snapshot.market.get("total_amount_source") or "待确认"),
            )
        )
        evidence_references.append(str(snapshot_path))

    if not snapshot_dates:
        raise EvidenceSnapshotError(
            f"指定日期范围内没有证据快照：{start_date} 至 {end_date}，目录：{snapshot_dir}"
        )

    return MarketHistorySummary(
        start_date=normalized_start.isoformat(),
        end_date=normalized_end.isoformat(),
        snapshot_dates=tuple(snapshot_dates),
        market_facts=tuple(market_facts),
        missing_market_dates=tuple(missing_market_dates),
        index_range_facts=build_index_range_facts(market_facts) if len(market_facts) >= MINIMUM_MARKET_HISTORY_DAYS else (),
        sequence_basis="local_snapshot_samples",
        evidence_references=tuple(evidence_references),
    )


# 连接器历史文件只证明三指数返回了相同的五根相邻日 K，不扩展为独立交易日历结论。
def summarize_tongdaxin_index_history(
    trade_date: str,
    history_path: Path,
) -> MarketHistorySummary:
    raw_data = read_json_mapping(history_path)
    if str(raw_data.get("trade_date") or "") != trade_date:
        raise EvidenceSnapshotError(f"通达信指数历史交易日期不一致：{history_path}")
    if str(raw_data.get("sample_date") or "") != trade_date:
        raise EvidenceSnapshotError(f"通达信指数历史样本日期不一致：{history_path}")
    if raw_data.get("quality") != TONGDAXIN_INDEX_HISTORY_QUALITY:
        raise EvidenceSnapshotError(f"通达信指数历史质量标记无效：{history_path}")

    sample_dates = raw_data.get("sample_dates")
    records = raw_data.get("records")
    if not isinstance(sample_dates, list) or not all(
        isinstance(item, str) for item in sample_dates
    ):
        raise EvidenceSnapshotError(f"通达信指数历史 sample_dates 格式无效：{history_path}")
    try:
        parsed_sample_dates = tuple(date.fromisoformat(item) for item in sample_dates)
    except ValueError as error:
        raise EvidenceSnapshotError(f"通达信指数历史 sample_dates 日期无效：{history_path}") from error
    if (
        len(sample_dates) != MINIMUM_MARKET_HISTORY_DAYS
        or len(set(sample_dates)) != len(sample_dates)
        or parsed_sample_dates != tuple(sorted(parsed_sample_dates))
        or sample_dates[-1] != trade_date
    ):
        raise EvidenceSnapshotError(f"通达信指数历史必须包含以目标日结束的 5 个唯一有序样本日期：{history_path}")
    if not isinstance(records, list) or len(records) != len(sample_dates):
        raise EvidenceSnapshotError(f"通达信指数历史 records 与 sample_dates 不一致：{history_path}")

    market_facts: list[MarketDailyFact] = []
    expected_index_names = {
        "sh000001": "上证指数",
        "sz399001": "深证成指",
        "sz399006": "创业板指",
    }
    expected_symbols = set(expected_index_names)
    for expected_date, record in zip(sample_dates, records, strict=True):
        if not isinstance(record, dict) or record.get("sample_date") != expected_date:
            raise EvidenceSnapshotError(f"通达信指数历史记录日期不一致：{history_path}")
        indices = record.get("indices")
        if not isinstance(indices, list) or len(indices) != len(expected_symbols):
            raise EvidenceSnapshotError(f"通达信指数历史必须逐日包含固定三指数：{history_path}")
        symbols = {str(item.get("symbol") or "") for item in indices if isinstance(item, dict)}
        if symbols != expected_symbols:
            raise EvidenceSnapshotError(f"通达信指数历史代码不在固定三指数范围内：{history_path}")
        normalized_indices: list[dict[str, Any]] = []
        for item in indices:
            if not isinstance(item, dict):
                raise EvidenceSnapshotError(f"通达信指数历史指数记录格式无效：{history_path}")
            symbol = str(item.get("symbol") or "")
            if item.get("name") != expected_index_names[symbol]:
                raise EvidenceSnapshotError(f"通达信指数历史指数名称与代码不一致：{history_path}")
            close = parse_number(item.get("close"))
            if close is None or close <= 0:
                raise EvidenceSnapshotError(f"通达信指数历史收盘值无效：{history_path}")
            normalized_indices.append(dict(item))
        market_facts.append(
            MarketDailyFact(
                trade_date=expected_date,
                sample_date=expected_date,
                indices=tuple(normalized_indices),
                total_amount=None,
                total_amount_source="不适用",
            )
        )

    raw_reference = str(raw_data.get("raw_reference") or "").strip()
    references = (str(history_path),) + ((raw_reference,) if raw_reference else ())
    return MarketHistorySummary(
        start_date=str(sample_dates[0]),
        end_date=str(sample_dates[-1]),
        snapshot_dates=tuple(str(item) for item in sample_dates),
        market_facts=tuple(market_facts),
        missing_market_dates=(),
        index_range_facts=build_index_range_facts(market_facts),
        sequence_basis=TONGDAXIN_INDEX_HISTORY_QUALITY,
        evidence_references=references,
    )


# 区间涨跌只在同一指数至少有五个市场事实时计算，避免把两日波动包装为市场阶段。
def build_index_range_facts(market_facts: list[MarketDailyFact]) -> tuple[MarketIndexRangeFact, ...]:
    grouped_records: dict[str, list[tuple[str, float]]] = {}
    for market_fact in market_facts:
        for index in market_fact.indices:
            name = str(index.get("name") or "").strip()
            close = parse_number(index.get("close"))
            if name and close is not None:
                grouped_records.setdefault(name, []).append((market_fact.sample_date, close))

    facts: list[MarketIndexRangeFact] = []
    for name, records in grouped_records.items():
        if len(records) < MINIMUM_MARKET_HISTORY_DAYS:
            continue
        start_date, start_close = records[0]
        end_date, end_close = records[-1]
        if start_close == 0:
            continue
        facts.append(
            MarketIndexRangeFact(
                name=name,
                observation_count=len(records),
                start_date=start_date,
                start_close=start_close,
                end_date=end_date,
                end_close=end_close,
                cumulative_change_percent=round((end_close / start_close - 1) * 100, 4),
            )
        )
    return tuple(sorted(facts, key=lambda item: item.name))


def render_market_history(summary: MarketHistorySummary) -> str:
    lines = [
        f"# {summary.start_date} 至 {summary.end_date} 市场历史事实",
        "",
        f"- 快照日期数：{len(summary.snapshot_dates)}",
        f"- 有效市场证据日期数：{len(summary.market_facts)}",
        f"- 市场区间事实最小历史要求：{MINIMUM_MARKET_HISTORY_DAYS} 个有效样本",
        "- 结论边界：本报告只汇总指数和成交额事实，不判断市场阶段、仓位或买卖方向。",
        "",
        "## 每日市场事实",
        "",
    ]
    if summary.market_facts:
        for fact in summary.market_facts:
            index_text = "；".join(
                f"{index.get('name', '待确认')} 收盘 {format_number(parse_number(index.get('close')))} "
                f"涨跌幅 {format_number(parse_number(index.get('change_percent')))}%"
                for index in fact.indices
            )
            amount_text = (
                "待确认"
                if fact.total_amount is None
                else f"{format_number(fact.total_amount)}（{fact.total_amount_source}）"
            )
            lines.append(
                f"- 交易日期：{fact.trade_date}｜样本日期：{fact.sample_date}｜"
                f"{index_text}｜成交额：{amount_text}"
            )
    else:
        lines.append("- 当前日期范围内没有市场层事实。")

    lines.extend(["", "## 市场证据缺口", ""])
    if summary.missing_market_dates:
        for trade_date in summary.missing_market_dates:
            lines.append(f"- {trade_date}：missing_indices 或 missing_total_amount。")
    else:
        lines.append("- 已读取快照均包含市场层指数事实。")

    lines.extend(["", "## 指数区间事实", ""])
    if not summary.has_enough_history:
        lines.append(
            f"- 数据不足：当前只有 {len(summary.market_facts)} 个有效市场样本，"
            f"至少需要 {MINIMUM_MARKET_HISTORY_DAYS} 个样本；不得判断市场阶段。"
        )
    elif not summary.index_range_facts:
        lines.append("- 当前有效历史中没有可计算区间涨跌的指数事实。")
    else:
        if summary.has_verified_daily_sequence:
            lines.append(
                "- 序列口径：通达信返回的连续 5 根日 K；仅验证连接器行序列，未独立核验交易日历。"
            )
        else:
            lines.append(
                f"- 区间口径：最近 {len(summary.market_facts)} 个本地有效快照样本（连续性未验证）；"
                f"日期：{'、'.join(fact.sample_date for fact in summary.market_facts)}。"
            )
        for fact in summary.index_range_facts:
            observation_label = "根日 K" if summary.has_verified_daily_sequence else "个样本"
            lines.append(
                f"- {fact.name}｜样本：{fact.observation_count} {observation_label}｜"
                f"{fact.start_date} 收盘 {format_number(fact.start_close)} → "
                f"{fact.end_date} 收盘 {format_number(fact.end_close)}｜"
                f"区间涨跌幅：{format_number(fact.cumulative_change_percent)}%"
            )
    return "\n".join(lines).rstrip() + "\n"


def create_market_history_report(
    start_date: str,
    end_date: str,
    snapshot_dir: Path,
    output_dir: Path = DEFAULT_MARKET_HISTORY_OUTPUT_DIR,
    log_path: Path = DEFAULT_LOG_PATH,
) -> tuple[Path, MarketHistorySummary]:
    summary = summarize_market_history(start_date, end_date, snapshot_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{summary.end_date}_market_history.md"
    output_path.write_text(render_market_history(summary), encoding="utf-8")
    write_market_history_log(summary, snapshot_dir, output_path, log_path)
    return output_path, summary


def write_market_history_log(
    summary: MarketHistorySummary,
    snapshot_dir: Path,
    output_path: Path,
    log_path: Path,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.market_history")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=evidence market-history start_date=%s end_date=%s snapshot_dir=%s output=%s "
            "snapshot_days=%s market_days=%s status=created",
            summary.start_date,
            summary.end_date,
            snapshot_dir,
            output_path,
            len(summary.snapshot_dates),
            len(summary.market_facts),
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
