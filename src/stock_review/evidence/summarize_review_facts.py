# 本文件负责把本地证据快照和真实池历史事实按 STEP 1-7 汇总为可追溯事实，不生成交易结论。

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import EvidenceSnapshotError, read_json_mapping


DEFAULT_EVIDENCE_DIR = Path("data") / "evidence"
DEFAULT_REVIEW_FACT_OUTPUT_DIR = Path("reports") / "daily"
MINIMUM_HISTORY_DAYS = 5


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
    pool_history_records: tuple[dict[str, Any], ...]
    pool_history_missing: bool


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

    pool_history_path = pool_history_dir / f"{trade_date}_real_pool_stock_history.json"
    pool_history_records = read_pool_history_records(pool_history_path, trade_date)
    return ReviewFactsSummary(
        trade_date=trade_date,
        sample_date=snapshot.sample_date,
        field_sources=snapshot.field_sources,
        missing_fields=snapshot.missing_fields,
        market=snapshot.market,
        sentiment=snapshot.sentiment,
        sectors=tuple(snapshot.sectors),
        stocks=tuple(snapshot.stocks),
        pool_history_records=pool_history_records,
        pool_history_missing=not pool_history_path.exists(),
    )


# 真实池历史文件是独立采集产物；缺文件或字段不足只形成证据缺口，不调用数据库补查。
def read_pool_history_records(pool_history_path: Path, trade_date: str) -> tuple[dict[str, Any], ...]:
    if not pool_history_path.exists():
        return ()
    raw_data = read_json_mapping(pool_history_path)
    if str(raw_data.get("trade_date") or "") != trade_date or str(raw_data.get("sample_date") or "") != trade_date:
        raise EvidenceSnapshotError(f"真实池历史事实日期不一致：{pool_history_path}")
    records = raw_data.get("records")
    if not isinstance(records, list):
        raise EvidenceSnapshotError(f"真实池历史事实 records 格式无效：{pool_history_path}")
    return tuple(record for record in records if isinstance(record, dict))


# 报告只转述输入事实、缺口和反向证据；五日门槛前不输出阶段、核心票或买点结论。
def render_review_facts(summary: ReviewFactsSummary) -> str:
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
        "- 归纳结果：不可判定。",
        f"- 反向证据：缺少至少 {MINIMUM_HISTORY_DAYS} 个有效交易日的连续市场事实、涨跌家数和外围市场字段。",
        "",
        "## STEP 2 市场风格",
        "",
        f"- 可用事实：最高连板 {render_value(summary.sentiment.get('highest_board'))}；涨停数 {render_value(summary.sentiment.get('limit_up_count'))}。",
        f"- 情绪字段来源：{source_name(summary, 'sentiment')}。",
        "- 归纳结果：不可判定。",
        "- 反向证据：缺少大市值趋势、容量中军、连板梯队完整性和活跃资金流向的可验证字段。",
        "",
        "## STEP 3 情绪周期",
        "",
        f"- 可用事实：涨停 {render_value(summary.sentiment.get('limit_up_count'))}；跌停 {render_value(summary.sentiment.get('limit_down_count'))}；炸板率比例 {render_value(summary.sentiment.get('broken_board_rate'))}；最高连板 {render_value(summary.sentiment.get('highest_board'))}；情绪温度 {render_value(summary.sentiment.get('emotion_temperature'))}。",
        "- 归纳结果：不可判定。",
        "- 反向证据：缺少晋级率、昨日涨停/连板反馈、竞价和连续情绪样本，不能判断情绪周期。",
        "",
        "## STEP 4 核心板块",
        "",
        f"- 板块字段来源：{source_name(summary, 'sectors')}。",
        f"- 当日排行事实：{render_sectors(summary.sectors)}",
        "- 归纳结果：待确认。",
        f"- 反向证据：缺少至少 {MINIMUM_HISTORY_DAYS} 个有效板块交易日的反复活跃、量能和内部表现证据，不能认定核心板块。",
        "",
        "## STEP 5 主升板块阶段",
        "",
        "- 可用事实：仅沿用 STEP 4 的当日板块排行与字段来源。",
        "- 归纳结果：不可判定。",
        "- 反向证据：缺少连续 K 线、量能、启动/回调节点和板块内部扩散证据，不能判断启动、主升、轮动或破位。",
        "",
        "## STEP 6 核心票",
        "",
        f"- 快照个股事实：{render_stock_candidates(summary.stocks)}",
        f"- 真实池历史事实：{render_pool_history(summary.pool_history_records, summary.pool_history_missing)}",
        "- 归纳结果：待确认。",
        "- 反向证据：角色来源、5/20 日表现和当日事实只能作为候选；缺少身份验证、逆势表现和连续比较证据，不能认定核心票。",
        "",
        "## STEP 7 重点关注",
        "",
        "- 可用对象：仅展示 STEP 6 的已采集候选和真实池历史事实，不自动加入、暂停、移出或修改任何池子。",
        "- 归纳结果：待用户人工确认。",
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


def render_sectors(sectors: tuple[dict[str, Any], ...]) -> str:
    if not sectors:
        return "待确认"
    values = []
    for sector in sectors[:5]:
        values.append(
            f"{sector.get('name') or '待确认'}（排行 {render_value(sector.get('rank'))}，涨停 {render_value(sector.get('limit_up_count'))}，来源类型 {sector.get('source_type') or '待确认'}）"
        )
    return "；".join(values)


def render_stock_candidates(stocks: tuple[dict[str, Any], ...]) -> str:
    confirmed = [stock for stock in stocks if str(stock.get("code") or "") not in {"", "待确认"}]
    if not confirmed:
        return "没有可确认代码的个股事实。"
    return "；".join(
        f"{stock.get('code')} {stock.get('name') or '待确认'}（{stock.get('role') or '角色待确认'}，来源 {stock.get('role_source') or stock.get('source') or '待确认'}）"
        for stock in confirmed
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


def render_value(value: Any) -> str:
    return str(value) if value not in (None, "") else "待确认"


def render_percent(value: Any) -> str:
    if value in (None, ""):
        return "待确认"
    return f"{value}%"
