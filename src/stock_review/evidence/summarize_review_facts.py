# 本文件负责把本地证据快照和真实池历史事实按 STEP 1-7 汇总为可追溯事实，不生成交易结论。

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
from pathlib import Path
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

from stock_review.evidence.akshare_sector_kline_source import (
    AKSHARE_SECTOR_KLINE_SOURCE,
    MAX_SECTOR_KLINE_OBJECTS,
    POSITIVE_SECTOR_SOURCE_TYPES,
    SECTOR_HISTORY_SOURCES,
    select_ranked_sector_objects,
)
from stock_review.evidence.check_history_readiness import has_hhxg_event
from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.longhubang_source import LONGHUBANG_SCOPE
from stock_review.evidence.manage_evidence_snapshot import EvidenceSnapshotError, read_json_mapping
from stock_review.evidence.summarize_hhxg_history import HhxgHistorySummary, summarize_hhxg_history
from stock_review.evidence.summarize_market_history import (
    MarketHistorySummary,
    summarize_market_history,
    summarize_tongdaxin_index_history,
)


DEFAULT_EVIDENCE_DIR = Path("data") / "evidence"
DEFAULT_REVIEW_FACT_OUTPUT_DIR = Path("reports") / "daily"
MINIMUM_HISTORY_DAYS = 5
CAPACITY_THRESHOLD_YUAN = 10_000_000_000
BOARD_ROLE_SOURCE_PATTERN = re.compile(r"^涨停池连板股[（(]\s*(\d+)\s*板[）)]$")


@dataclass(frozen=True)
class ReviewFactsSummary:
    trade_date: str
    sample_date: str
    field_sources: dict[str, str]
    missing_fields: tuple[str, ...]
    market: dict[str, Any]
    sentiment: dict[str, Any]
    sectors: tuple[dict[str, Any], ...]
    stocks: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    board_ladder: dict[str, Any]
    pool_history_records: tuple[dict[str, Any], ...]
    pool_history_missing: bool
    candidate_history_records: tuple[dict[str, Any], ...]
    candidate_history_missing: bool
    candidate_capacity_ranking: dict[str, Any]
    longhubang_evidence: dict[str, Any]
    us_market_evidence: dict[str, Any]
    sector_kline_evidence: dict[str, Any]
    market_history: MarketHistorySummary
    hhxg_history: HhxgHistorySummary
    evidence_references: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class StepEvidence:
    step_number: int
    allowed_field_keys: tuple[str, ...]
    hard_missing_fields: tuple[str, ...]

    @property
    def status(self) -> str:
        if not self.hard_missing_fields:
            return "pending_confirmation"
        primary_gap_by_step = {
            1: "missing_indices",
            2: "missing_sentiment",
            3: "missing_sentiment",
            4: "missing_sectors",
            5: "missing_sectors",
            6: "missing_stocks",
        }
        return (
            "unavailable"
            if primary_gap_by_step.get(self.step_number) in self.hard_missing_fields
            else "partial"
        )


STEP_EVIDENCE_FIELDS = {
    1: (
        "market.indices",
        "market.total_amount",
        "market.advance_decline",
        "market.us_session",
        "market.history_range",
    ),
    2: (
        "sentiment.limit_up_count",
        "sentiment.limit_down_count",
        "sentiment.broken_board_rate",
        "sentiment.highest_board",
        "sentiment.history_style",
        "sentiment.history_cycle",
        "sentiment.board_ladder",
        "sectors.rankings",
        "stocks.longhubang",
        "candidate_history.capacity_ranking",
        "candidate_history.trend_capacity",
    ),
    3: (
        "sentiment.limit_up_count",
        "sentiment.limit_down_count",
        "sentiment.broken_board_rate",
        "sentiment.highest_board",
        "sentiment.profit_effect_percent",
        "sentiment.history_cycle",
    ),
    4: (
        "sectors.rankings",
        "sectors.daily_kline",
        "sectors.history_repeated_rankings",
    ),
    5: ("sectors.rankings", "sectors.daily_kline"),
    6: (
        "stocks.current",
        "stocks.relative_comparison",
        "candidate_history.latest",
        "candidate_history.return_5d_percent",
        "candidate_history.return_20d_percent",
        "candidate_history.capacity_ranking",
        "candidate_history.trend_capacity",
        "stocks.longhubang",
        "pool_history.latest",
        "pool_history.return_5d_percent",
        "pool_history.return_20d_percent",
    ),
    7: (
        "pool.active_real",
        "stocks.current",
        "stocks.relative_comparison",
        "candidate_history.latest",
        "candidate_history.return_5d_percent",
        "candidate_history.return_20d_percent",
        "candidate_history.capacity_ranking",
        "candidate_history.trend_capacity",
        "stocks.longhubang",
        "pool_history.latest",
        "pool_history.return_5d_percent",
        "pool_history.return_20d_percent",
    ),
}

FIELD_LABELS = {
    "market.indices": "指数事实",
    "market.total_amount": "成交额事实",
    "market.advance_decline": "全市场涨跌家数",
    "market.us_session": "A 股收盘后有效窗内的美股常规会话",
    "market.history_range": "指数历史样本事实",
    "sentiment.limit_up_count": "涨停数",
    "sentiment.limit_down_count": "跌停数",
    "sentiment.broken_board_rate": "炸板率",
    "sentiment.highest_board": "最高连板",
    "sentiment.profit_effect_percent": "赚钱效应",
    "sentiment.history_style": "多日涨停与连板高度事实",
    "sentiment.history_cycle": "多日短线情绪事实",
    "sentiment.board_ladder": "结构化连板梯队",
    "stocks.longhubang": "有限候选龙虎榜事实",
    "candidate_history.capacity_ranking": "有限候选容量与 5/20 日横截面排序",
    "candidate_history.trend_capacity": "有限候选趋势与成交容量事实",
    "sectors.rankings": "板块排行",
    "sectors.daily_kline": "有限排行板块连续日 K",
    "sectors.history_repeated_rankings": "多日板块重复排行事实",
    "stocks.current": "当日个股事实",
    "stocks.relative_comparison": "个股相对强弱与连续比较事实",
    "pool.active_real": "有效真实池对象",
    "candidate_history.latest": "有限候选当日行情",
    "candidate_history.return_5d_percent": "有限候选 5 日涨跌幅",
    "candidate_history.return_20d_percent": "有限候选 20 日涨跌幅",
    "pool_history.latest": "真实池当日行情",
    "pool_history.return_5d_percent": "真实池 5 日涨跌幅",
    "pool_history.return_20d_percent": "真实池 20 日涨跌幅",
}

HARD_GAP_LABELS = {
    "missing_indices": "缺少指数事实",
    "missing_total_amount": "缺少成交额事实",
    "missing_sentiment": "缺少短线情绪事实",
    "missing_sectors": "缺少板块排行事实",
    "missing_stocks": "缺少可确认代码的个股事实",
    "missing_market_history": "缺少连续市场历史",
    "missing_continuous_market_history": "缺少经来源序列验证的连续市场历史",
    "missing_sentiment_history": "缺少连续短线情绪历史",
    "missing_advance_decline": "缺少涨跌家数",
    "missing_overseas_market": "缺少外围市场事实",
    "missing_large_cap_trend": "缺少大市值趋势",
    "missing_board_ladder": "缺少连板梯队",
    "missing_capital_flow": "缺少活跃资金流向",
    "missing_promotion_rate": "缺少晋级率",
    "missing_previous_day_feedback": "缺少昨日涨停或连板反馈",
    "missing_auction": "缺少竞价事实",
    "missing_sector_history": "缺少连续板块历史",
    "missing_sector_kline": "缺少板块连续 K 线和量能",
    "missing_sector_diffusion": "缺少板块内部扩散事实",
    "missing_real_pool_history": "缺少真实池历史文件",
    "missing_candidate_history": "缺少有限候选历史文件",
    "missing_candidate_capacity": "缺少有限候选成交容量或流通市值",
    "missing_pool_latest": "缺少真实池当日行情",
    "missing_pool_5d_return": "缺少真实池 5 日涨跌幅",
    "missing_pool_20d_return": "缺少真实池 20 日涨跌幅",
    "missing_stock_identity": "缺少个股身份验证",
    "missing_relative_strength": "缺少逆势表现",
    "missing_continuous_comparison": "缺少连续比较证据",
}

SNAPSHOT_GAP_CODES = frozenset(
    {
        "missing_indices",
        "missing_total_amount",
        "missing_sentiment",
        "missing_sectors",
        "missing_stocks",
    }
)


# 只读取指定交易日的快照和同日真实池事实文件；日期或范围不一致时不把记录作为当日事实使用。
def summarize_review_facts(trade_date: str, snapshot_dir: Path, pool_history_dir: Path) -> ReviewFactsSummary:
    try:
        date.fromisoformat(trade_date)
    except ValueError as error:
        raise EvidenceSnapshotError("复盘事实日期格式必须为 YYYY-MM-DD。") from error

    snapshot_path = snapshot_dir / f"{trade_date}_snapshot.json"
    if not snapshot_path.exists():
        raise EvidenceSnapshotError(f"证据快照不存在：{snapshot_path}")
    raw_data = read_json_mapping(snapshot_path)
    snapshot = build_evidence_snapshot(trade_date, raw_data)
    if snapshot.sample_date != trade_date:
        raise EvidenceSnapshotError(
            f"证据快照样本日期与交易日期不一致：交易日期 {trade_date}，样本日期 {snapshot.sample_date}。"
        )

    # 通达信固定三指数历史存在时优先使用；否则本地快照只能形成连续性未验证的样本区间。
    tongdaxin_history_path = snapshot_dir / f"{trade_date}_tdx_index_history.json"
    if tongdaxin_history_path.exists():
        market_history = summarize_tongdaxin_index_history(trade_date, tongdaxin_history_path)
    else:
        market_history = summarize_market_history(
            find_recent_history_start_date(trade_date, snapshot_dir, has_market_fact),
            trade_date,
            snapshot_dir,
        )
    hhxg_history = summarize_hhxg_history(
        find_recent_history_start_date(trade_date, snapshot_dir, has_valid_hhxg_fact),
        trade_date,
        snapshot_dir,
    )

    pool_history_path = pool_history_dir / f"{trade_date}_real_pool_stock_history.json"
    pool_history_records = read_pool_history_records(pool_history_path, trade_date)
    candidate_history_path = pool_history_dir / f"{trade_date}_review_candidate_history.json"
    candidate_history_records = read_candidate_history_records(
        candidate_history_path, trade_date, "复盘候选历史事实"
    )
    longhubang_path = snapshot_dir / f"{trade_date}_longhubang.json"
    longhubang_evidence = read_longhubang_evidence(
        longhubang_path,
        trade_date,
        candidate_history_path,
        candidate_history_records,
    )
    us_market_path = snapshot_dir / f"{trade_date}_us_market.json"
    us_market_evidence = read_us_market_evidence(us_market_path, trade_date)
    sector_kline_path = snapshot_dir / f"{trade_date}_sector_kline.json"
    sector_kline_evidence = read_sector_kline_evidence(
        sector_kline_path,
        trade_date,
        snapshot_path,
        snapshot.sectors,
    )
    board_ladder = derive_board_ladder(snapshot.stocks, snapshot.sentiment)
    candidate_capacity_ranking = derive_candidate_capacity_ranking(
        candidate_history_records,
        not candidate_history_path.exists(),
        read_history_source(candidate_history_path),
    )
    evidence_references = build_evidence_references(
        trade_date,
        snapshot_dir,
        snapshot_path,
        market_history,
        hhxg_history,
        pool_history_path,
        pool_history_records,
        candidate_history_path,
        candidate_history_records,
        longhubang_path,
        longhubang_evidence,
        us_market_path,
        us_market_evidence,
        sector_kline_path,
        sector_kline_evidence,
    )
    return ReviewFactsSummary(
        trade_date=trade_date,
        sample_date=snapshot.sample_date,
        field_sources=snapshot.field_sources,
        missing_fields=snapshot.missing_fields,
        market=snapshot.market,
        sentiment=snapshot.sentiment,
        sectors=tuple(snapshot.sectors),
        stocks=tuple(snapshot.stocks),
        events=tuple(snapshot.events),
        board_ladder=board_ladder,
        pool_history_records=pool_history_records,
        pool_history_missing=not pool_history_path.exists(),
        candidate_history_records=candidate_history_records,
        candidate_history_missing=not candidate_history_path.exists(),
        candidate_capacity_ranking=candidate_capacity_ranking,
        longhubang_evidence=longhubang_evidence,
        us_market_evidence=us_market_evidence,
        sector_kline_evidence=sector_kline_evidence,
        market_history=market_history,
        hhxg_history=hhxg_history,
        evidence_references=evidence_references,
    )


# 每个固定证据字段保留实际参与计算或渲染的本地来源，避免统一误指向复盘日快照。
def build_evidence_references(
    trade_date: str,
    snapshot_dir: Path,
    snapshot_path: Path,
    market_history: MarketHistorySummary,
    hhxg_history: HhxgHistorySummary,
    pool_history_path: Path,
    pool_history_records: tuple[dict[str, Any], ...],
    candidate_history_path: Path,
    candidate_history_records: tuple[dict[str, Any], ...],
    longhubang_path: Path,
    longhubang_evidence: dict[str, Any],
    us_market_path: Path,
    us_market_evidence: dict[str, Any],
    sector_kline_path: Path,
    sector_kline_evidence: dict[str, Any],
) -> dict[str, tuple[str, ...]]:
    current_reference = (str(snapshot_path),)
    references: dict[str, tuple[str, ...]] = {}
    for field_name in (
        "market.indices",
        "market.total_amount",
        "market.advance_decline",
        "sentiment.limit_up_count",
        "sentiment.limit_down_count",
        "sentiment.broken_board_rate",
        "sentiment.highest_board",
        "sentiment.profit_effect_percent",
        "sentiment.board_ladder",
        "sectors.rankings",
        "stocks.current",
        "stocks.relative_comparison",
    ):
        references[field_name] = current_reference

    references["market.history_range"] = market_history.evidence_references
    references["market.us_session"] = (
        (str(us_market_path),) if us_market_evidence else ()
    )
    references["stocks.longhubang"] = (
        (str(longhubang_path),) if longhubang_evidence else ()
    )
    references["sectors.daily_kline"] = (
        (str(sector_kline_path),) if sector_kline_evidence else ()
    )
    hhxg_references = tuple(
        str(snapshot_dir / f"{fact.trade_date}_snapshot.json")
        for fact in hhxg_history.daily_facts
    )
    references["sentiment.history_style"] = hhxg_references
    references["sentiment.history_cycle"] = hhxg_references
    references["sectors.history_repeated_rankings"] = hhxg_references

    candidate_reference = (str(candidate_history_path),) if candidate_history_path.exists() else ()
    for field_name in (
        "candidate_history.latest",
        "candidate_history.return_5d_percent",
        "candidate_history.return_20d_percent",
        "candidate_history.capacity_ranking",
        "candidate_history.trend_capacity",
    ):
        references[field_name] = candidate_reference

    if pool_history_path.exists():
        pool_reference = (str(pool_history_path),)
    elif pool_history_records:
        pool_reference = candidate_reference
    elif any(record.get("origin") == "real_pool" for record in candidate_history_records):
        pool_reference = candidate_reference
    else:
        pool_reference = ()
    for field_name in (
        "pool_history.latest",
        "pool_history.return_5d_percent",
        "pool_history.return_20d_percent",
    ):
        references[field_name] = pool_reference
    return references


# 最近窗口只服务固定 STEP 字段映射；少于五日时仍返回已有事实，并由各 STEP 的硬缺口阻止判断。
def find_recent_history_start_date(
    trade_date: str,
    snapshot_dir: Path,
    is_valid: Callable[[str, Any], bool],
) -> str:
    matching_dates: list[str] = []
    for snapshot_path in sorted(snapshot_dir.glob("*_snapshot.json"), reverse=True):
        raw_data = read_json_mapping(snapshot_path)
        snapshot_date = str(raw_data.get("trade_date") or snapshot_path.name.removesuffix("_snapshot.json"))
        if snapshot_date > trade_date:
            continue
        snapshot = build_evidence_snapshot(snapshot_date, raw_data)
        if snapshot.sample_date == snapshot_date and is_valid(snapshot_date, snapshot):
            matching_dates.append(snapshot_date)
        if len(matching_dates) == MINIMUM_HISTORY_DAYS:
            break
    return matching_dates[-1] if matching_dates else trade_date


# 市场历史只认定已有指数记录，成交额缺口仍在 STEP 1 硬缺口中单独呈现。
def has_market_fact(_trade_date: str, snapshot: Any) -> bool:
    indices = snapshot.market.get("indices")
    return isinstance(indices, list) and bool(indices)


# hhxg 历史只认定样本日期一致且事件来源明确的快照，顶层 source 不作为历史可用性的依据。
def has_valid_hhxg_fact(_trade_date: str, snapshot: Any) -> bool:
    return has_hhxg_event(snapshot.events)


# 真实池历史文件是独立采集产物；缺文件或字段不足只形成证据缺口，不调用数据库补查。
def read_pool_history_records(pool_history_path: Path, trade_date: str) -> tuple[dict[str, Any], ...]:
    return read_history_records(pool_history_path, trade_date, "真实池历史事实")


def read_history_records(
    history_path: Path, trade_date: str, label: str
) -> tuple[dict[str, Any], ...]:
    if not history_path.exists():
        return ()
    raw_data = read_json_mapping(history_path)
    if str(raw_data.get("trade_date") or "") != trade_date or str(raw_data.get("sample_date") or "") != trade_date:
        raise EvidenceSnapshotError(f"{label}日期不一致：{history_path}")
    records = raw_data.get("records")
    if not isinstance(records, list):
        raise EvidenceSnapshotError(f"{label} records 格式无效：{history_path}")
    return tuple(record for record in records if isinstance(record, dict))


# 有限候选分母只接受代码、名称均明确且代码唯一的记录，避免排序分母与详细报告候选集分裂。
def read_candidate_history_records(
    history_path: Path,
    trade_date: str,
    label: str,
) -> tuple[dict[str, Any], ...]:
    records = read_history_records(history_path, trade_date, label)
    if not history_path.exists():
        return records
    raw_data = read_json_mapping(history_path)
    raw_records = raw_data.get("records")
    if not isinstance(raw_records, list) or len(records) != len(raw_records):
        raise EvidenceSnapshotError(f"{label} records 必须全部是对象：{history_path}")
    identities: dict[str, str] = {}
    for record in records:
        code = str(record.get("code") or "").strip()
        name = str(record.get("name") or "").strip()
        if not (code.isdigit() and len(code) == 6 and is_confirmed_text(name)):
            raise EvidenceSnapshotError(f"{label}候选身份无效：{history_path}")
        if code in identities:
            raise EvidenceSnapshotError(f"{label}候选代码重复：{history_path}")
        identities[code] = name
    return records


# 龙虎榜文件必须与同日候选历史的代码和名称全集一致；未上榜记录也是中性事实，不能在读取时丢弃。
def read_longhubang_evidence(
    evidence_path: Path,
    trade_date: str,
    candidate_history_path: Path,
    candidate_history_records: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    if not evidence_path.exists():
        return {}
    evidence = read_json_mapping(evidence_path)
    validate_supplemental_review_date(evidence, trade_date, "龙虎榜证据", evidence_path)
    if str(evidence.get("sample_date") or "") != trade_date:
        raise EvidenceSnapshotError(f"龙虎榜证据样本日期不一致：{evidence_path}")
    records = evidence.get("records")
    candidate_count = finite_number(evidence.get("candidate_count"))
    expected_candidates: dict[str, str] = {}
    for candidate in candidate_history_records:
        code = str(candidate.get("code") or "").strip()
        name = str(candidate.get("name") or "").strip()
        if not (code.isdigit() and len(code) == 6 and is_confirmed_text(name)):
            raise EvidenceSnapshotError(f"有限候选历史身份无效，不能绑定龙虎榜证据：{candidate_history_path}")
        if code in expected_candidates and expected_candidates[code] != name:
            raise EvidenceSnapshotError(f"有限候选历史同代码名称不一致：{candidate_history_path}")
        expected_candidates[code] = name
    consumed_listed_count = finite_number(evidence.get("consumed_listed_count"))
    if (
        not is_confirmed_text(evidence.get("source"))
        or evidence.get("scope") != LONGHUBANG_SCOPE
        or evidence.get("candidate_history_file") != candidate_history_path.name
        or not isinstance(records, list)
        or candidate_count is None
        or not candidate_count.is_integer()
        or int(candidate_count) != len(records)
        or int(candidate_count) != len(expected_candidates)
        or consumed_listed_count is None
        or not consumed_listed_count.is_integer()
    ):
        raise EvidenceSnapshotError(f"龙虎榜证据格式无效：{evidence_path}")
    seen_codes: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise EvidenceSnapshotError(f"龙虎榜证据 records 格式无效：{evidence_path}")
        code = str(record.get("code") or "").strip()
        if (
            not (code.isdigit() and len(code) == 6)
            or not is_confirmed_text(record.get("name"))
            or code in seen_codes
            or record.get("status") not in {"listed", "not_listed"}
            or record.get("listed") is not (record.get("status") == "listed")
        ):
            raise EvidenceSnapshotError(f"龙虎榜证据候选记录无效：{evidence_path}")
        if expected_candidates.get(code) != str(record.get("name") or "").strip():
            raise EvidenceSnapshotError(f"龙虎榜证据与有限候选集合不一致：{evidence_path}")
        seen_codes.add(code)
    if seen_codes != set(expected_candidates) or int(consumed_listed_count) != sum(
        record.get("status") == "listed" for record in records
    ):
        raise EvidenceSnapshotError(f"龙虎榜证据与有限候选集合不一致：{evidence_path}")
    return evidence


# 美股文件允许 pending，但 available 必须同时包含纳指与标普的完整收盘和涨跌幅。
def read_us_market_evidence(evidence_path: Path, trade_date: str) -> dict[str, Any]:
    if not evidence_path.exists():
        return {}
    evidence = read_json_mapping(evidence_path)
    validate_supplemental_review_date(evidence, trade_date, "美股窗口证据", evidence_path)
    status = evidence.get("status")
    if (
        not is_confirmed_text(evidence.get("source"))
        or not isinstance(evidence.get("window"), dict)
        or not isinstance(evidence.get("indices"), list)
        or status not in {"available", "pending"}
    ):
        raise EvidenceSnapshotError(f"美股窗口证据格式无效：{evidence_path}")
    if not has_valid_us_market_window(evidence):
        raise EvidenceSnapshotError(f"美股窗口证据时点边界无效：{evidence_path}")
    if status == "available" and not has_complete_us_market_evidence(evidence):
        raise EvidenceSnapshotError(f"美股窗口证据缺少纳指或标普完整事实：{evidence_path}")
    if status == "pending" and evidence.get("indices"):
        raise EvidenceSnapshotError(f"pending 美股窗口证据不得携带旧会话指数：{evidence_path}")
    return evidence


# 板块日 K 必须绑定当前 Snapshot 的最多三个正向排行对象；不可用对象保留状态但不解除缺口。
def read_sector_kline_evidence(
    evidence_path: Path,
    trade_date: str,
    snapshot_path: Path,
    ranked_sectors: list[dict[str, Any]],
) -> dict[str, Any]:
    if not evidence_path.exists():
        return {}
    evidence = read_json_mapping(evidence_path)
    validate_supplemental_review_date(evidence, trade_date, "板块日 K 证据", evidence_path)
    if str(evidence.get("sample_date") or "") != trade_date:
        raise EvidenceSnapshotError(f"板块日 K 证据样本日期不一致：{evidence_path}")
    records = evidence.get("records")
    selected_count = finite_number(evidence.get("selected_object_count"))
    lookback_bars = finite_number(evidence.get("lookback_bars"))
    max_objects = finite_number(evidence.get("max_objects"))
    expected_objects = select_ranked_sector_objects(list(ranked_sectors))
    if (
        evidence.get("source") != AKSHARE_SECTOR_KLINE_SOURCE
        or evidence.get("scope") != "ranked_sector_kline_limited"
        or evidence.get("snapshot_file") != snapshot_path.name
        or evidence.get("selection_basis")
        != "positive_source_type_then_rank_then_snapshot_order_then_exact_unique_name"
        or evidence.get("eligible_source_types") != sorted(POSITIVE_SECTOR_SOURCE_TYPES)
        or not isinstance(records, list)
        or selected_count is None
        or not selected_count.is_integer()
        or not 1 <= int(selected_count) <= 3
        or int(selected_count) != len(records)
        or lookback_bars is None
        or not lookback_bars.is_integer()
        or not 5 <= int(lookback_bars) <= 10
        or max_objects != float(MAX_SECTOR_KLINE_OBJECTS)
        or len(records) != len(expected_objects)
    ):
        raise EvidenceSnapshotError(f"板块日 K 证据格式无效：{evidence_path}")
    available_count = 0
    for record, expected_object in zip(records, expected_objects, strict=True):
        ranking_identity = (
            f"{record.get('source_type') or ''} {record.get('ranking_group') or ''}".lower()
            if isinstance(record, dict)
            else ""
        )
        if (
            not isinstance(record, dict)
            or not is_confirmed_text(record.get("name"))
            or not is_confirmed_text(record.get("source_type"))
            or str(record.get("source_type") or "").lower() not in POSITIVE_SECTOR_SOURCE_TYPES
            or "weak" in ranking_identity
            or record.get("mapping_basis") != "exact_unique_name"
            or record.get("status") not in {"available", "unavailable", "ambiguous"}
            or any(
                record.get(field_name) != expected_object.get(field_name)
                for field_name in (
                    "name",
                    "source_type",
                    "ranking_group",
                    "rank",
                    "snapshot_position",
                )
            )
        ):
            raise EvidenceSnapshotError(f"板块日 K 证据记录无效：{evidence_path}")
        if record.get("status") == "available":
            if not has_complete_sector_kline_record(
                record,
                trade_date,
                int(lookback_bars),
            ):
                raise EvidenceSnapshotError(f"板块日 K 证据 K 线不完整：{evidence_path}")
            if record.get("history_source") != SECTOR_HISTORY_SOURCES.get(
                record.get("resolved_board_type")
            ):
                raise EvidenceSnapshotError(f"板块日 K 证据历史来源无效：{evidence_path}")
            available_count += 1
        elif record.get("bars") not in ([], None) or record.get("bar_count") not in (0, None):
            raise EvidenceSnapshotError(f"不可用板块日 K 记录不得携带行情：{evidence_path}")
    if finite_number(evidence.get("available_count")) != float(available_count):
        raise EvidenceSnapshotError(f"板块日 K 证据可用计数不一致：{evidence_path}")
    for status_name in ("unavailable", "ambiguous"):
        actual_count = sum(record.get("status") == status_name for record in records)
        if finite_number(evidence.get(f"{status_name}_count")) != float(actual_count):
            raise EvidenceSnapshotError(f"板块日 K 证据状态计数不一致：{evidence_path}")
    return evidence


def validate_supplemental_review_date(
    evidence: dict[str, Any],
    trade_date: str,
    label: str,
    evidence_path: Path,
) -> None:
    if str(evidence.get("review_date") or "") != trade_date:
        raise EvidenceSnapshotError(f"{label}日期不一致：{evidence_path}")


# 派生事实保留历史文件声明的来源；缺文件或缺来源时由完整性字段显式阻断确定性使用。
def read_history_source(history_path: Path) -> str:
    if not history_path.exists():
        return ""
    return str(read_json_mapping(history_path).get("source") or "").strip()


# 连板梯队只认涨停池连板候选的明确板数，并与当日最高板交叉校验。
def derive_board_ladder(
    stocks: list[dict[str, Any]], sentiment: dict[str, Any]
) -> dict[str, Any]:
    relevant_records = [
        stock
        for stock in stocks
        if stock.get("role") == "连板事实候选"
        or BOARD_ROLE_SOURCE_PATTERN.fullmatch(str(stock.get("role_source") or "").strip())
    ]
    candidates_by_board: dict[int, list[dict[str, str]]] = {}
    seen_codes: set[str] = set()
    sources: set[str] = set()
    has_invalid_record = False
    for stock in relevant_records:
        role_source = str(stock.get("role_source") or "").strip()
        match = BOARD_ROLE_SOURCE_PATTERN.fullmatch(role_source)
        code = str(stock.get("code") or "").strip()
        name = str(stock.get("name") or "").strip()
        source = str(stock.get("source") or "").strip()
        if source:
            sources.add(source)
        if match is None or not is_confirmed_text(code) or not is_confirmed_text(name):
            has_invalid_record = True
            continue
        board_count = int(match.group(1))
        if board_count < 2 or code in seen_codes:
            has_invalid_record = True
            continue
        if source != "akshare:stock_zt_pool_em":
            has_invalid_record = True
        seen_codes.add(code)
        candidates_by_board.setdefault(board_count, []).append({"code": code, "name": name})

    observed_highest = max(candidates_by_board, default=0)
    expected_highest = finite_number(sentiment.get("highest_board"))
    if (
        expected_highest is None
        or not expected_highest.is_integer()
        or int(expected_highest) != observed_highest
    ):
        has_invalid_record = True

    tiers = [
        {
            "board_count": board_count,
            "candidate_count": len(candidates_by_board.get(board_count, [])),
            "candidates": sorted(
                candidates_by_board.get(board_count, []),
                key=lambda item: (item["code"], item["name"]),
            ),
        }
        for board_count in range(observed_highest, 1, -1)
    ]
    if not relevant_records:
        completeness = "unavailable"
    elif not seen_codes or has_invalid_record:
        completeness = "partial"
    else:
        completeness = "complete"
    return {
        "tiers": tiers,
        "candidate_count": len(seen_codes),
        "completeness": completeness,
        "source": ",".join(sorted(sources)) or "待确认",
    }


# 容量和涨幅排序只描述同日历史文件内的去重候选，分母和覆盖数不得外推为全市场。
def derive_candidate_capacity_ranking(
    records: tuple[dict[str, Any], ...], missing: bool, source: str
) -> dict[str, Any]:
    unique_records: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    has_duplicate = False
    for record in records:
        code = str(record.get("code") or "").strip()
        if is_confirmed_text(code):
            if code in seen_codes:
                has_duplicate = True
                continue
            seen_codes.add(code)
        unique_records.append(record)

    capacity_rows: list[dict[str, Any]] = []
    return_5d_rows: list[dict[str, Any]] = []
    return_20d_rows: list[dict[str, Any]] = []
    has_incomplete_record = has_duplicate or not is_confirmed_text(source)
    for record in unique_records:
        code = str(record.get("code") or "").strip()
        name = str(record.get("name") or "").strip()
        market_value = finite_number(record.get("circulating_market_value"))
        average_amount = finite_number(record.get("average_5d_amount"))
        return_5d = finite_number(record.get("return_5d_percent"))
        return_20d = finite_number(record.get("return_20d_percent"))
        identity_confirmed = is_confirmed_text(code) and is_confirmed_text(name)
        if not identity_confirmed or None in (
            market_value,
            average_amount,
            return_5d,
            return_20d,
        ):
            has_incomplete_record = True
        if not identity_confirmed:
            continue
        if market_value is not None:
            capacity_rows.append(
                {
                    "code": code,
                    "name": name,
                    "circulating_market_value": market_value,
                    "average_5d_amount": average_amount,
                }
            )
        if return_5d is not None:
            return_5d_rows.append(
                {"code": code, "name": name, "return_percent": return_5d}
            )
        if return_20d is not None:
            return_20d_rows.append(
                {"code": code, "name": name, "return_percent": return_20d}
            )

    capacity_rows.sort(
        key=lambda item: (-item["circulating_market_value"], item["code"], item["name"])
    )
    capacity_candidates = [
        {"rank": rank, **item}
        for rank, item in enumerate(capacity_rows, start=1)
        if item["circulating_market_value"] >= CAPACITY_THRESHOLD_YUAN
    ]
    return_5d_ranking = rank_return_rows(return_5d_rows)
    return_20d_ranking = rank_return_rows(return_20d_rows)
    if missing or not unique_records:
        completeness = "unavailable"
    elif has_incomplete_record:
        completeness = "partial"
    else:
        completeness = "complete"
    return {
        "scope": "finite_candidate_history",
        "candidate_count": len(unique_records),
        "capacity_threshold_yuan": CAPACITY_THRESHOLD_YUAN,
        "capacity_covered_count": len(capacity_rows),
        "capacity_candidate_count": len(capacity_candidates),
        "capacity_candidates": capacity_candidates,
        "return_5d_covered_count": len(return_5d_ranking),
        "return_5d_ranking": return_5d_ranking,
        "return_20d_covered_count": len(return_20d_ranking),
        "return_20d_ranking": return_20d_ranking,
        "completeness": completeness,
        "source": source or "待确认",
    }


def rank_return_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        rows,
        key=lambda item: (-item["return_percent"], item["code"], item["name"]),
    )
    return [{"rank": rank, **item} for rank, item in enumerate(ordered, start=1)]


def build_step_evidence(summary: ReviewFactsSummary) -> dict[int, StepEvidence]:
    missing_fields = set(summary.missing_fields)
    resolved_non_snapshot_gaps = derive_resolved_non_snapshot_gaps(summary)
    step_gaps = {
        1: required_gaps(
            missing_fields,
            ("missing_indices", "missing_total_amount", "missing_advance_decline", "missing_overseas_market"),
            resolved_non_snapshot_gaps,
        ),
        2: required_gaps(
            missing_fields,
            (
                "missing_sentiment",
                "missing_large_cap_trend",
                "missing_board_ladder",
                "missing_capital_flow",
            ),
            resolved_non_snapshot_gaps,
        ),
        3: required_gaps(
            missing_fields,
            ("missing_sentiment", "missing_promotion_rate", "missing_previous_day_feedback", "missing_auction"),
            resolved_non_snapshot_gaps,
        ),
        4: required_gaps(
            missing_fields,
            ("missing_sectors", "missing_sector_kline", "missing_sector_diffusion"),
            resolved_non_snapshot_gaps,
        ),
        5: required_gaps(
            missing_fields,
            ("missing_sectors", "missing_sector_kline", "missing_sector_diffusion"),
            resolved_non_snapshot_gaps,
        ),
        6: required_gaps(
            missing_fields,
            ("missing_stocks", "missing_stock_identity", "missing_relative_strength", "missing_continuous_comparison"),
            resolved_non_snapshot_gaps,
        ),
        7: required_gaps(
            missing_fields,
            (
                "missing_stocks",
                "missing_stock_identity",
                "missing_relative_strength",
                "missing_continuous_comparison",
                "missing_auction",
            ),
            resolved_non_snapshot_gaps,
        ),
    }
    if not summary.market_history.index_range_facts:
        step_gaps[1].append("missing_market_history")
    elif not summary.market_history.has_verified_daily_sequence:
        step_gaps[1].append("missing_continuous_market_history")
    if not has_comparable_sentiment_history(summary):
        step_gaps[3].append("missing_sentiment_history")
    if not summary.hhxg_history.has_enough_history or not summary.hhxg_history.repeated_rank_facts:
        step_gaps[4].append("missing_sector_history")
    pool_history_gaps = pool_history_gap_codes(summary)
    step_gaps[6].extend(pool_history_gaps)
    step_gaps[7].extend(pool_history_gaps)
    candidate_history_gaps = candidate_history_gap_codes(summary)
    step_gaps[2].extend(candidate_history_gaps)
    step_gaps[6].extend(candidate_history_gaps)
    step_gaps[7].extend(candidate_history_gaps)
    return {
        step_number: StepEvidence(step_number, STEP_EVIDENCE_FIELDS[step_number], tuple(step_gaps[step_number]))
        for step_number in range(1, 8)
    }


def has_comparable_sentiment_history(summary: ReviewFactsSummary) -> bool:
    # 情绪周期必须覆盖与三指数相同的相邻日 K 日期；五个离散快照只用于展示，不证明周期方向。
    confirmed_dates = tuple(
        fact.trade_date
        for fact in summary.hhxg_history.daily_facts
        if fact.sentiment_source_confirmed
    )
    market_dates = tuple(fact.sample_date for fact in summary.market_history.market_facts)
    return (
        summary.hhxg_history.has_enough_history
        and summary.market_history.has_verified_daily_sequence
        and confirmed_dates[-MINIMUM_HISTORY_DAYS:] == market_dates[-MINIMUM_HISTORY_DAYS:]
        and len(confirmed_dates) >= MINIMUM_HISTORY_DAYS
    )


# 非快照标准缺口只有在本地存在对应正式字段时解除，有限候选排序不能替代全市场或盘中事实。
def derive_resolved_non_snapshot_gaps(summary: ReviewFactsSummary) -> set[str]:
    resolved: set[str] = set()
    if has_finite_pair(summary.market, "up_count", "down_count"):
        resolved.add("missing_advance_decline")
    if has_complete_us_market_evidence(summary.us_market_evidence):
        resolved.add("missing_overseas_market")
    if is_complete_fact(summary.market.get("large_cap_trend")):
        resolved.add("missing_large_cap_trend")
    if summary.board_ladder.get("completeness") == "complete":
        resolved.add("missing_board_ladder")
    if any(has_sector_capital_flow(sector) for sector in summary.sectors) or has_longhubang_capital_flow(
        summary.longhubang_evidence
    ):
        resolved.add("missing_capital_flow")
    if (
        finite_number(summary.sentiment.get("promotion_rate")) is not None
        and is_confirmed_text(summary.field_sources.get("sentiment"))
    ):
        resolved.add("missing_promotion_rate")
    if is_complete_fact(summary.sentiment.get("previous_day_feedback")):
        resolved.add("missing_previous_day_feedback")
    if any(
        is_complete_fact(value)
        for value in (
            summary.market.get("call_auction"),
            summary.sentiment.get("call_auction"),
            *(stock.get("call_auction") for stock in summary.stocks),
        )
    ):
        resolved.add("missing_auction")
    if any(has_verified_sector_kline(sector) for sector in summary.sectors) or any(
        has_complete_sector_kline_record(
            record,
            summary.trade_date,
            int(summary.sector_kline_evidence.get("lookback_bars") or 0),
        )
        for record in summary.sector_kline_evidence.get("records") or []
        if isinstance(record, dict)
    ):
        resolved.add("missing_sector_kline")
    if any(has_finite_pair(sector, "up_count", "down_count") for sector in summary.sectors):
        resolved.add("missing_sector_diffusion")
    if summary.stocks and all(
        stock.get("identity_verified") is True
        and is_confirmed_text(stock.get("code"))
        and is_confirmed_text(stock.get("name"))
        for stock in summary.stocks
    ):
        resolved.add("missing_stock_identity")
    # STEP 6/7 面向整组候选做比较；部分对象有值不能代表比较证据完整。
    if summary.stocks and all(
        is_complete_fact(stock.get("relative_strength")) for stock in summary.stocks
    ):
        resolved.add("missing_relative_strength")
    if summary.stocks and all(
        is_complete_fact(stock.get("continuous_comparison")) for stock in summary.stocks
    ):
        resolved.add("missing_continuous_comparison")
    return resolved


# 消费独立美股文件时再次复核生产端时点约束，避免手工替换文件绕过严格窗口。
def has_valid_us_market_window(evidence: dict[str, Any]) -> bool:
    window = evidence.get("window")
    if not isinstance(window, dict) or window.get("boundary") != "open_interval":
        return False
    generated_at = parse_aware_evidence_datetime(evidence.get("generated_at"))
    as_of = parse_aware_evidence_datetime(evidence.get("as_of"))
    window_start = parse_aware_evidence_datetime(window.get("a_share_close_at"))
    window_end = parse_aware_evidence_datetime(window.get("next_a_share_open_at"))
    if (
        None in (generated_at, as_of, window_start, window_end)
        or as_of > generated_at
        or window_start >= window_end
        or window_start.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        != str(evidence.get("review_date") or "")
    ):
        return False
    if evidence.get("status") == "pending":
        return (
            evidence.get("us_session_date") in (None, "")
            and evidence.get("session_close_at") in (None, "")
            and evidence.get("indices") == []
        )
    session_close_at = parse_aware_evidence_datetime(evidence.get("session_close_at"))
    return (
        evidence.get("status") == "available"
        and is_confirmed_text(evidence.get("us_session_date"))
        and session_close_at is not None
        and window_start < session_close_at < window_end
        and session_close_at <= as_of
    )


def parse_aware_evidence_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def has_complete_us_market_evidence(evidence: dict[str, Any]) -> bool:
    if (
        not has_valid_us_market_window(evidence)
        or evidence.get("status") != "available"
        or not is_confirmed_text(evidence.get("source"))
        or not is_confirmed_text(evidence.get("us_session_date"))
        or not is_confirmed_text(evidence.get("session_close_at"))
        or not isinstance(evidence.get("window"), dict)
    ):
        return False
    indices = evidence.get("indices")
    if not isinstance(indices, list):
        return False
    by_symbol = {
        str(index.get("symbol") or "").strip(): index
        for index in indices
        if isinstance(index, dict)
    }
    if set(by_symbol) != {".IXIC", ".INX"}:
        return False
    return all(
        finite_number(index.get("close")) is not None
        and finite_number(index.get("close")) > 0
        and finite_number(index.get("change_percent")) is not None
        for index in by_symbol.values()
    )


def has_longhubang_capital_flow(evidence: dict[str, Any]) -> bool:
    records = evidence.get("records")
    if not isinstance(records, list):
        return False
    return any(
        isinstance(record, dict)
        and record.get("status") == "listed"
        and any(
            finite_number(record.get(field_name)) is not None
            for field_name in (
                "buy_amount",
                "sell_amount",
                "net_amount",
                "institutional_net_amount",
            )
        )
        for record in records
    )


def has_sector_capital_flow(sector: dict[str, Any]) -> bool:
    return (
        is_confirmed_text(sector.get("name"))
        and is_confirmed_text(sector.get("source_type"))
        and finite_number(
            sector.get("net_inflow_yi")
            if sector.get("net_inflow_yi") not in (None, "")
            else sector.get("net_inflow")
        )
        is not None
    )


def has_verified_sector_kline(sector: dict[str, Any]) -> bool:
    records = sector.get("daily_kline")
    if not isinstance(records, list) or len(records) < MINIMUM_HISTORY_DAYS:
        return False
    valid_dates: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            return False
        sample_date = str(record.get("sample_date") or record.get("trade_date") or "")
        try:
            date.fromisoformat(sample_date)
        except ValueError:
            return False
        if finite_number(record.get("close")) is None or (
            finite_number(record.get("amount")) is None
            and finite_number(record.get("volume")) is None
        ):
            return False
        valid_dates.append(sample_date)
    return len(set(valid_dates)) == len(valid_dates) and valid_dates == sorted(valid_dates)


def has_complete_sector_kline_record(
    record: dict[str, Any],
    trade_date: str,
    lookback_bars: int,
) -> bool:
    bars = record.get("bars")
    if (
        record.get("status") != "available"
        or record.get("resolved_board_type") not in {"concept", "industry"}
        or not is_confirmed_text(record.get("resolved_board_code"))
        or not is_confirmed_text(record.get("history_source"))
        or not 5 <= lookback_bars <= 10
        or not isinstance(bars, list)
        or len(bars) != lookback_bars
        or finite_number(record.get("bar_count")) != float(lookback_bars)
    ):
        return False
    dates: list[str] = []
    for bar in bars:
        if not isinstance(bar, dict):
            return False
        sample_date = str(bar.get("date") or "")
        try:
            date.fromisoformat(sample_date)
        except ValueError:
            return False
        open_price = finite_number(bar.get("open"))
        high_price = finite_number(bar.get("high"))
        low_price = finite_number(bar.get("low"))
        close_price = finite_number(bar.get("close"))
        volume = finite_number(bar.get("volume"))
        amount = finite_number(bar.get("amount"))
        if (
            None in (open_price, high_price, low_price, close_price)
            or min(open_price, high_price, low_price, close_price) <= 0
            or high_price < max(open_price, close_price)
            or low_price > min(open_price, close_price)
            or (volume is None and amount is None)
            or (volume is not None and volume < 0)
            or (amount is not None and amount < 0)
        ):
            return False
        dates.append(sample_date)
    return len(set(dates)) == len(dates) and dates == sorted(dates) and dates[-1] == trade_date


def has_finite_pair(mapping: dict[str, Any], first_key: str, second_key: str) -> bool:
    first = finite_number(mapping.get(first_key))
    second = finite_number(mapping.get(second_key))
    return (
        first is not None
        and second is not None
        and first >= 0
        and second >= 0
        and first.is_integer()
        and second.is_integer()
    )


def is_complete_fact(value: Any) -> bool:
    return isinstance(value, dict) and (
        value.get("completeness") == "complete" or value.get("verified") is True
    )


def required_gaps(
    existing_gaps: set[str],
    required_codes: tuple[str, ...],
    resolved_non_snapshot_gaps: set[str],
) -> list[str]:
    return [
        code
        for code in required_codes
        if (
            code in existing_gaps
            if code in SNAPSHOT_GAP_CODES
            else code not in resolved_non_snapshot_gaps
        )
    ]


def pool_history_gap_codes(summary: ReviewFactsSummary) -> list[str]:
    # collect-review-history 已包含真实池对象时，它就是当前详细复盘的有效真实池历史，不再要求重复采集旧文件。
    pool_records = summary.pool_history_records or tuple(
        record
        for record in summary.candidate_history_records
        if record.get("origin") == "real_pool"
    )
    if summary.pool_history_missing and not pool_records:
        return ["missing_real_pool_history"]
    if not pool_records:
        return ["missing_pool_latest", "missing_pool_5d_return", "missing_pool_20d_return"]
    gaps: list[str] = []
    if not any(record.get("close") not in (None, "") for record in pool_records):
        gaps.append("missing_pool_latest")
    if not any(record.get("return_5d_percent") not in (None, "") for record in pool_records):
        gaps.append("missing_pool_5d_return")
    if not any(record.get("return_20d_percent") not in (None, "") for record in pool_records):
        gaps.append("missing_pool_20d_return")
    return gaps


def candidate_history_gap_codes(summary: ReviewFactsSummary) -> list[str]:
    if summary.candidate_history_missing:
        return ["missing_candidate_history"]
    return (
        []
        if summary.candidate_capacity_ranking.get("completeness") == "complete"
        else ["missing_candidate_capacity"]
    )


# 报告只转述输入事实、缺口和反向证据；五日门槛前不输出阶段、核心票或买点结论。
def render_review_facts(summary: ReviewFactsSummary) -> str:
    step_evidence = build_step_evidence(summary)
    lines = [
        f"# {summary.trade_date} STEP 1-7 证据事实归纳",
        "",
        f"- 交易日期：{summary.trade_date}",
        f"- 样本日期：{summary.sample_date}",
        "- 结论边界：本报告只归纳已有证据，不判断市场阶段、市场风格、情绪周期、板块阶段、核心票或买点。",
        "- 反向证据口径：列出当前不能支持确定性判断的关键缺口。",
        "",
        "## STEP 1 大盘阶段",
        "",
        f"- 市场字段来源：{source_name(summary, 'market')}。",
        f"- 指数事实：{render_indices(summary.market)}",
        f"- 成交额事实：{render_value(summary.market.get('total_amount'))}；来源：{summary.market.get('total_amount_source') or '待确认'}。",
        f"- 全市场涨跌家数：上涨 {render_value(summary.market.get('up_count'))}，下跌 {render_value(summary.market.get('down_count'))}；来源：{summary.market.get('advance_decline_source') or '待确认'}；质量：{summary.market.get('advance_decline_quality') or '待确认'}。",
        f"- 美股有效窗事实：{render_us_market_evidence(summary.us_market_evidence)}",
        f"- 市场历史样本事实：{render_market_history(summary.market_history)}",
        "- 归纳结果：不可判定。",
        *render_step_evidence_contract(step_evidence[1]),
        "- 反向证据：指数路径与涨跌家数只能支撑本地事实；外围市场及上述未解除硬缺口仍不得推断。",
        "",
        "## STEP 2 市场风格",
        "",
        f"- 可用事实：最高连板 {render_value(summary.sentiment.get('highest_board'))}；涨停数 {render_value(summary.sentiment.get('limit_up_count'))}。",
        f"- 情绪字段来源：{source_name(summary, 'sentiment')}。",
        f"- 板块净流入事实：{render_sector_capital_flows(summary.sectors)}",
        f"- 有限候选龙虎榜事实：{render_longhubang_evidence(summary.longhubang_evidence)}",
        f"- 多日涨停与连板高度事实：{render_hhxg_style_history(summary.hhxg_history)}",
        f"- 结构化连板梯队：{render_board_ladder(summary.board_ladder)}",
        f"- 有限候选容量与横截面排序：{render_candidate_capacity_ranking(summary.candidate_capacity_ranking)}",
        f"- 有限候选趋势与容量事实：{render_candidate_trend_capacity(summary.candidate_history_records, summary.candidate_history_missing)}",
        "- 归纳结果：不可判定。",
        *render_step_evidence_contract(step_evidence[2]),
        "- 反向证据：有限候选容量与涨幅排序不是全市场大市值趋势；未解除的硬缺口仍不得推断。",
        "",
        "## STEP 3 情绪周期",
        "",
        f"- 可用事实：涨停 {render_value(summary.sentiment.get('limit_up_count'))}；跌停 {render_value(summary.sentiment.get('limit_down_count'))}；炸板率比例 {render_value(summary.sentiment.get('broken_board_rate'))}；最高连板 {render_value(summary.sentiment.get('highest_board'))}；赚钱效应 {render_value(summary.sentiment.get('profit_effect_percent'))}（{summary.sentiment.get('profit_effect_calculation') or '公式待确认'}）。",
        f"- 多日短线情绪事实：{render_hhxg_sentiment_history(summary.hhxg_history)}",
        "- 归纳结果：不可判定。",
        *render_step_evidence_contract(step_evidence[3]),
        "- 反向证据：缺少晋级率、昨日涨停/连板反馈和竞价；情绪来源不连续时也不能判断情绪周期。",
        "",
        "## STEP 4 核心板块",
        "",
        f"- 板块字段来源：{source_name(summary, 'sectors')}。",
        f"- 当日排行事实：{render_sectors(summary.sectors)}",
        f"- 有限排行板块日 K：{render_sector_kline_evidence(summary.sector_kline_evidence)}",
        f"- 多日重复排行事实：{render_repeated_rank_facts(summary.hhxg_history)}",
        "- 归纳结果：待确认。",
        *render_step_evidence_contract(step_evidence[4]),
        "- 反向证据：同源排行与有限板块日 K 可以支持方向倾向，但不能外推未采集板块或替代内部扩散事实。",
        "",
        "## STEP 5 主升板块阶段",
        "",
        f"- 可用事实：沿用 STEP 4 的当日板块排行，并使用有限排行板块日 K：{render_sector_kline_evidence(summary.sector_kline_evidence)}",
        "- 归纳结果：不可判定。",
        *render_step_evidence_contract(step_evidence[5]),
        "- 反向证据：只对唯一精确映射的有限排行对象提供日 K；未解除的量价或内部扩散缺口仍限制阶段判断。",
        "",
        "## STEP 6 核心票",
        "",
        f"- 快照个股事实：{render_stock_candidates(summary.stocks)}",
        f"- 真实池历史事实：{render_pool_history(summary.pool_history_records, summary.pool_history_missing)}",
        f"- 系统候选与真实池逐只历史事实：{render_pool_history(summary.candidate_history_records, summary.candidate_history_missing)}",
        f"- 有限候选容量与横截面排序：{render_candidate_capacity_ranking(summary.candidate_capacity_ranking)}",
        "- 归纳结果：待确认。",
        *render_step_evidence_contract(step_evidence[6]),
        "- 反向证据：角色来源、5/20 日表现和当日事实只能作为候选；缺少身份验证、逆势表现和连续比较证据，不能认定核心票。",
        "",
        "## STEP 7 重点关注",
        "",
        "- 可用对象：仅展示 STEP 6 的已采集候选和真实池历史事实，不自动加入、暂停、移出或修改任何池子。",
        f"- 有限候选容量与横截面排序：{render_candidate_capacity_ranking(summary.candidate_capacity_ranking)}",
        "- 归纳结果：待用户人工确认。",
        *render_step_evidence_contract(step_evidence[7]),
        "- 反向证据：缺少用户当日复盘判断、符合预期/超预期/不及预期/放弃条件，不能生成次日重点关注或交易计划。",
        "",
        "## 统一证据缺口",
        "",
    ]
    if summary.missing_fields:
        lines.extend(f"- {field_name}" for field_name in summary.missing_fields)
    else:
        lines.append("- 当前 Evidence Snapshot 的标准字段缺口为 0；这不表示 STEP 1-7 所需时序和判断字段齐全。")
    return "\n".join(lines).rstrip() + "\n"


def render_step_evidence_contract(step_evidence: StepEvidence) -> list[str]:
    field_labels = "、".join(FIELD_LABELS[field_name] for field_name in step_evidence.allowed_field_keys)
    hard_gap_labels = "、".join(HARD_GAP_LABELS[gap] for gap in step_evidence.hard_missing_fields) or "无"
    return [
        f"- 本地允许事实字段：{field_labels}。",
        f"- 本地状态：{step_evidence.status}；硬缺口：{hard_gap_labels}。",
    ]


# 输出文件只由显式 CLI 调用创建，归纳过程本身不写 SQLite、不调用外部数据源。
def create_review_facts_report(
    trade_date: str,
    snapshot_dir: Path = DEFAULT_EVIDENCE_DIR,
    pool_history_dir: Path = DEFAULT_EVIDENCE_DIR,
    output_dir: Path = DEFAULT_REVIEW_FACT_OUTPUT_DIR,
) -> tuple[Path, ReviewFactsSummary]:
    summary = summarize_review_facts(trade_date, snapshot_dir, pool_history_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{trade_date}_step_1_to_7_facts.md"
    output_path.write_text(render_review_facts(summary), encoding="utf-8")
    return output_path, summary


def source_name(summary: ReviewFactsSummary, field_name: str) -> str:
    return summary.field_sources.get(field_name, "待确认")


def render_indices(market: dict[str, Any]) -> str:
    indices = market.get("indices")
    if not isinstance(indices, list) or not indices:
        return "待确认"
    values = []
    for index in indices:
        if isinstance(index, dict):
            values.append(f"{index.get('name') or '待确认'} 收盘 {render_value(index.get('close'))}，涨跌幅 {render_percent(index.get('change_percent'))}")
    return "；".join(values) if values else "待确认"


def render_market_history(summary: MarketHistorySummary) -> str:
    if not summary.has_enough_history:
        return f"当前仅 {len(summary.market_facts)} 个有效市场样本，至少需要 {MINIMUM_HISTORY_DAYS} 个样本。"
    if not summary.index_range_facts:
        return "已有 5 个市场样本，但没有可计算的指数区间事实。"
    dates = "、".join(fact.sample_date for fact in summary.market_facts)
    if summary.has_verified_daily_sequence:
        prefix = f"通达信返回的连续 5 根日 K（{dates}；未独立核验交易日历）"
        suffix = "根日 K 首尾变化"
        path_label = "完整收盘路径"
    else:
        prefix = f"最近 {len(summary.market_facts)} 个本地有效快照样本（{dates}；连续性未验证）"
        suffix = "个样本首尾变化"
        path_label = "样本收盘路径"
    rendered_facts: list[str] = []
    for fact in summary.index_range_facts:
        close_path: list[str] = []
        for market_fact in summary.market_facts:
            matching_index = next(
                (
                    index
                    for index in market_fact.indices
                    if index.get("name") == fact.name
                ),
                None,
            )
            if matching_index is None:
                continue
            close = finite_number(matching_index.get("close"))
            if close is not None:
                close_path.append(
                    f"{market_fact.sample_date} 收盘 {render_number(close)}"
                )
        rendered_facts.append(
            f"{fact.name}{path_label} "
            f"{' → '.join(close_path)}；{fact.observation_count} {suffix} "
            f"{render_percent(fact.cumulative_change_percent)}"
        )
    facts = "；".join(rendered_facts)
    return f"{prefix}：{facts}"


def render_hhxg_style_history(summary: HhxgHistorySummary) -> str:
    facts = [fact for fact in summary.daily_facts if fact.sentiment_source_confirmed]
    if not facts:
        return "没有来源可确认的多日涨停与连板高度事实。"
    return "；".join(
        f"{fact.trade_date} 涨停 {render_value(fact.limit_up_count)}、最高连板 {render_value(fact.highest_board)}"
        for fact in facts
    )


def render_hhxg_sentiment_history(summary: HhxgHistorySummary) -> str:
    facts = [fact for fact in summary.daily_facts if fact.sentiment_source_confirmed]
    if not facts:
        return "没有来源可确认的多日短线情绪事实。"
    values = [
        f"{fact.trade_date} 涨停 {render_value(fact.limit_up_count)}、跌停 {render_value(fact.limit_down_count)}、"
        f"炸板率 {render_ratio_percent(fact.broken_board_rate)}、最高连板 {render_value(fact.highest_board)}"
        for fact in facts
    ]
    suffix = "" if len(facts) >= MINIMUM_HISTORY_DAYS else f"；其中仅 {len(facts)} 日情绪来源可确认"
    return "；".join(values) + suffix


def render_sectors(sectors: tuple[dict[str, Any], ...]) -> str:
    if not sectors:
        return "待确认"
    grouped_texts: list[str] = []
    for source_type, label in (
        ("hhxg_hot_theme", "热门题材"),
        ("hhxg_strong_sector", "强势板块"),
    ):
        records = [item for item in sectors if item.get("source_type") == source_type][:5]
        if records:
            grouped_texts.append(
                f"{label}：" + "；".join(
                    f"{item.get('name') or '待确认'}（排行 {render_value(item.get('rank'))}）"
                    for item in records
                )
            )
    if grouped_texts:
        return "｜".join(grouped_texts)
    return "；".join(
        f"{item.get('name') or '待确认'}（排行 {render_value(item.get('rank'))}，来源类型 {item.get('source_type') or '待确认'}）"
        for item in sectors[:5]
    )


def render_sector_capital_flows(sectors: tuple[dict[str, Any], ...]) -> str:
    records = [sector for sector in sectors if has_sector_capital_flow(sector)]
    if not records:
        return "待确认"
    return "；".join(
        f"{sector.get('name')}（{sector.get('source_type')}，净流入 "
        f"{render_value(sector.get('net_inflow_yi') if sector.get('net_inflow_yi') not in (None, '') else sector.get('net_inflow'))}"
        f"{' 亿元' if sector.get('net_inflow_yi') not in (None, '') else ''}）"
        for sector in records
    )


def render_sector_kline_evidence(evidence: dict[str, Any]) -> str:
    if not evidence:
        return "未采集。"
    rendered_records: list[str] = []
    for record in evidence.get("records") or []:
        if not isinstance(record, dict):
            continue
        identity = (
            f"{record.get('name')}（原榜 {record.get('source_type') or '待确认'}，"
            f"排名 {render_value(record.get('rank'))}）"
        )
        if record.get("status") != "available":
            rendered_records.append(
                f"{identity}：{record.get('status')}，{record.get('note') or '映射不可用'}"
            )
            continue
        bars = "；".join(
            f"{bar.get('date')} 开 {render_value(bar.get('open'))} 高 {render_value(bar.get('high'))} "
            f"低 {render_value(bar.get('low'))} 收 {render_value(bar.get('close'))} "
            f"成交量 {render_value(bar.get('volume'))} 成交额 {render_value(bar.get('amount'))}"
            for bar in record.get("bars") or []
            if isinstance(bar, dict)
        )
        rendered_records.append(
            f"{identity}：唯一映射为 {record.get('resolved_board_type')} "
            f"{record.get('resolved_board_code')}，{render_value(record.get('bar_count'))} 根日 K：{bars}"
        )
    return (
        f"正向排行对象 {render_value(evidence.get('selected_object_count'))} 个，"
        f"可用 {render_value(evidence.get('available_count'))} 个；"
        f"{'｜'.join(rendered_records) or '没有有效记录'}；"
        "成交量与成交额保留 AKShare 上游原值，未声明单位不另行换算。"
    )


def render_us_market_evidence(evidence: dict[str, Any]) -> str:
    if not evidence:
        return "未采集。"
    window = evidence.get("window") if isinstance(evidence.get("window"), dict) else {}
    window_text = (
        f"有效窗 {window.get('a_share_close_at') or '待确认'} 至 "
        f"{window.get('next_a_share_open_at') or '待确认'}"
    )
    if evidence.get("status") != "available":
        return (
            f"pending；截至 {evidence.get('as_of') or '待确认'}，{window_text}内没有已完成会话；"
            "不回退使用 A 股收盘前的旧会话。"
        )
    index_text = "、".join(
        f"{index.get('name') or index.get('symbol')} 收盘 {render_value(index.get('close'))}、"
        f"涨跌幅 {render_percent(index.get('change_percent'))}"
        for index in evidence.get("indices") or []
        if isinstance(index, dict)
    )
    return (
        f"{evidence.get('us_session_date')} 常规会话，收盘时点 "
        f"{evidence.get('session_close_at')}；{index_text}；{window_text}；仅作外围事实，不表示因果。"
    )


def render_longhubang_evidence(evidence: dict[str, Any]) -> str:
    if not evidence:
        return "未采集。"
    records = evidence.get("records") if isinstance(evidence.get("records"), list) else []
    listed_records = [
        record
        for record in records
        if isinstance(record, dict) and record.get("status") == "listed"
    ]
    listed_text = "；".join(
        f"{record.get('code')} {record.get('name')}（原因 {record.get('reason') or '待确认'}，"
        f"净额 {render_value(record.get('net_amount'))} 元，"
        f"机构净额 {render_value(record.get('institutional_net_amount'))} 元）"
        for record in listed_records
    ) or "有限候选当日均未上公开日榜"
    return (
        f"有限候选分母 {render_value(evidence.get('candidate_count'))} 只，"
        f"其中当日公开榜内 {render_value(evidence.get('consumed_listed_count'))} 只；"
        f"{listed_text}；未上榜只表示没有当日日榜记录，不构成负面证据。"
    )


def render_repeated_rank_facts(summary: HhxgHistorySummary) -> str:
    if not summary.has_enough_history:
        return f"当前仅 {len(summary.daily_facts)} 个有效 hhxg 交易日，至少需要 {MINIMUM_HISTORY_DAYS} 日。"
    if not summary.repeated_rank_facts:
        return "已有 5 日 hhxg 快照，但没有重复出现至少 2 日的排行事实。"
    return "；".join(
        f"{fact.name}（{fact.source_type}，出现 {fact.occurrence_count} 日，最近 {fact.latest_trade_date} 排名 {render_value(fact.latest_rank)}）"
        for fact in summary.repeated_rank_facts[:10]
    )


def render_stock_candidates(stocks: tuple[dict[str, Any], ...]) -> str:
    confirmed = [stock for stock in stocks if str(stock.get("code") or "") not in {"", "待确认"}]
    if not confirmed:
        return "没有可确认代码的个股事实。"
    return "；".join(
        f"{stock.get('code')} {stock.get('name') or '待确认'}（{stock.get('role') or '角色待确认'}，来源 {stock.get('role_source') or stock.get('source') or '待确认'}）"
        for stock in confirmed
    )


def render_board_ladder(fact: dict[str, Any]) -> str:
    completeness = str(fact.get("completeness") or "unavailable")
    if completeness == "unavailable":
        return "没有可验证的连板梯队事实。"
    tier_texts = []
    for tier in fact.get("tiers") or []:
        candidates = tier.get("candidates") or []
        candidate_text = "、".join(
            f"{item.get('code')} {item.get('name')}" for item in candidates
        ) or "无"
        tier_texts.append(
            f"{render_value(tier.get('board_count'))} 板 {render_value(tier.get('candidate_count'))} 只（{candidate_text}）"
        )
    return (
        f"去重候选分母 {render_value(fact.get('candidate_count'))} 只；"
        f"{'；'.join(tier_texts) or '梯队待确认'}；完整性 {completeness}；"
        f"来源 {fact.get('source') or '待确认'}"
    )


def render_candidate_capacity_ranking(fact: dict[str, Any]) -> str:
    if fact.get("completeness") == "unavailable":
        return "同日有限候选历史事实不可用。"
    candidate_count = render_value(fact.get("candidate_count"))
    capacity_candidates = fact.get("capacity_candidates") or []
    capacity_text = "、".join(
        f"{item.get('rank')}. {item.get('code')} {item.get('name')}（流通市值 {render_yi(item.get('circulating_market_value'))}，5 日均成交额 {render_yi(item.get('average_5d_amount'))}）"
        for item in capacity_candidates
    ) or "无"
    return_5d_text = render_return_ranking(fact.get("return_5d_ranking") or [])
    return_20d_text = render_return_ranking(fact.get("return_20d_ranking") or [])
    return (
        f"范围为有限候选历史去重 {candidate_count} 只（非全市场）；"
        f"流通市值覆盖 {render_value(fact.get('capacity_covered_count'))}/{candidate_count}，"
        f"其中不低于 100 亿元 {render_value(fact.get('capacity_candidate_count'))} 只：{capacity_text}；"
        f"5 日横截面覆盖 {render_value(fact.get('return_5d_covered_count'))}/{candidate_count}：{return_5d_text}；"
        f"20 日横截面覆盖 {render_value(fact.get('return_20d_covered_count'))}/{candidate_count}：{return_20d_text}；"
        f"完整性 {fact.get('completeness') or 'unavailable'}；来源 {fact.get('source') or '待确认'}"
    )


def render_return_ranking(ranking: list[dict[str, Any]]) -> str:
    if not ranking:
        return "待确认"
    return "、".join(
        f"{item.get('rank')}. {item.get('code')} {item.get('name')} {render_percent(item.get('return_percent'))}"
        for item in ranking
    )


def render_pool_history(records: tuple[dict[str, Any], ...], missing: bool) -> str:
    if missing:
        return "同日真实池历史事实文件不存在。"
    if not records:
        return "真实池历史事实文件为空。"
    values = []
    for record in records:
        values.append(
            f"{record.get('code') or '待确认'} {record.get('name') or '待确认'}：5 日 {render_percent(record.get('return_5d_percent'))}，20 日 {render_percent(record.get('return_20d_percent'))}，缺口 {','.join(record.get('missing_fields') or []) or '无'}"
        )
    return "；".join(values)


def render_candidate_trend_capacity(records: tuple[dict[str, Any], ...], missing: bool) -> str:
    if missing:
        return "同日有限候选历史事实文件不存在。"
    if not records:
        return "有限候选历史事实为空。"
    return "；".join(
        f"{record.get('code') or '待确认'} {record.get('name') or '待确认'}：5 日 {render_percent(record.get('return_5d_percent'))}，"
        f"20 日 {render_percent(record.get('return_20d_percent'))}，5 日均成交额 {render_value(record.get('average_5d_amount'))} 元，"
        f"流通市值 {render_value(record.get('circulating_market_value'))} 元"
        for record in records
    )


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def is_confirmed_text(value: Any) -> bool:
    return str(value or "").strip() not in {"", "待确认", "不适用"}


def render_number(value: Any) -> str:
    number = finite_number(value)
    if number is None:
        return "待确认"
    return f"{number:.4f}".rstrip("0").rstrip(".")


def render_yi(value: Any) -> str:
    number = finite_number(value)
    if number is None:
        return "待确认"
    return f"{number / 100_000_000:.2f}".rstrip("0").rstrip(".") + " 亿元"


def render_value(value: Any) -> str:
    return str(value) if value not in (None, "") else "待确认"


def render_percent(value: Any) -> str:
    if value in (None, ""):
        return "待确认"
    return f"{value}%"


# 炸板率历史与当日口径一致，快照内 0 至 1 的比例统一显示为百分比。
def render_ratio_percent(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "待确认"
    return f"{value * 100:.4f}".rstrip("0").rstrip(".") + "%"
