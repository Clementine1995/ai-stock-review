# 本文件负责证据导入、真实采集编排和 Evidence Snapshot 检查，不直接实现第三方接口。

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable

from stock_review.evidence.akshare_source import (
    AkshareSourceError,
    collect_akshare_market_evidence,
    collect_akshare_sector_evidence,
    collect_akshare_sentiment_evidence,
    collect_akshare_stock_evidence,
)
from stock_review.evidence.evidence_snapshot import EvidenceSnapshot, build_evidence_snapshot
from stock_review.evidence.hhxg_source import HhxgSourceError, collect_hhxg_snapshot_evidence
from stock_review.evidence.tongdaxin_source import (
    TONGDAXIN_SOURCE,
    TongdaxinSourceError,
    build_tongdaxin_index_history_evidence,
    build_tongdaxin_market_breadth_evidence,
    build_tongdaxin_market_evidence,
)
from stock_review.evidence.enrich_stock_facts import (
    StockFactEnrichmentError,
    enrich_snapshot_comparisons,
    enrich_snapshot_identities,
    load_stock_identity_input,
)
from stock_review.storage.sqlite_repository import EvidenceSnapshotRepository


DEFAULT_EVIDENCE_DIR = Path("data") / "evidence"
DEFAULT_DATABASE_PATH = Path("data") / "stock_review.sqlite"


class EvidenceSnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceScopeCollectionResult:
    scope: str
    output_path: Path | None
    error_message: str | None

    @property
    def is_successful(self) -> bool:
        return self.error_message is None


# 样例导入只接受本地 JSON，生成标准化快照并保存到本地 SQLite，保证后续复盘只读取标准证据。
def import_evidence_snapshot(
    trade_date: str,
    evidence_file: Path,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> Path:
    raw_data = read_json_mapping(evidence_file)
    raw_data = attach_field_sources(raw_data)
    snapshot = build_evidence_snapshot(trade_date, raw_data)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{trade_date}_snapshot.json"
    output_path.write_text(
        json.dumps(snapshot.to_mapping(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    repository = EvidenceSnapshotRepository(database_path)
    repository.save_snapshot(snapshot)
    return output_path


# 三指数历史导入只生成独立标准化文件，不改写同日快照、SQLite 或其它业务对象。
def import_tongdaxin_index_history(
    trade_date: str,
    evidence_file: Path,
    confirmed: bool,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
) -> Path:
    if not confirmed:
        raise EvidenceSnapshotError("导入通达信三指数历史证据前必须取得用户明确确认。")

    snapshot_path = output_dir / f"{trade_date}_snapshot.json"
    if not snapshot_path.exists():
        raise EvidenceSnapshotError(f"通达信三指数历史导入要求已有同日证据快照：{snapshot_path}")

    existing_data = read_json_mapping(snapshot_path)
    existing_trade_date = str(existing_data.get("trade_date") or trade_date)
    if existing_trade_date != trade_date:
        raise EvidenceSnapshotError(
            f"当前快照交易日期不是目标交易日：{existing_trade_date}，拒绝导入指数历史。"
        )
    existing_snapshot = build_evidence_snapshot(trade_date, existing_data)
    if existing_snapshot.sample_date != trade_date:
        raise EvidenceSnapshotError(
            f"当前快照样本日期不是目标交易日：{existing_snapshot.sample_date}，拒绝导入指数历史。"
        )

    try:
        normalized = build_tongdaxin_index_history_evidence(
            trade_date,
            read_json_mapping(evidence_file),
        )
    except TongdaxinSourceError as error:
        raise EvidenceSnapshotError(str(error)) from error

    target_indices = normalized["records"][-1]["indices"]
    validate_existing_market_indices(
        existing_snapshot.market,
        {str(item["name"]): item["close"] for item in target_indices},
        evidence_label="通达信三指数历史证据",
    )

    # 原始信封路径进入标准化证据，供后续报告追溯；原始 Rows 不复制进标准文件。
    output_data = dict(normalized)
    output_data["raw_reference"] = str(evidence_file)
    output_path = output_dir / f"{trade_date}_tdx_index_history.json"
    output_path.write_text(
        json.dumps(output_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


# 通达信导入只补已有标准快照的成交额缺口，不替换指数或其它 scope，并要求用户显式确认。
def import_tongdaxin_market_snapshot(
    trade_date: str,
    evidence_file: Path,
    confirmed: bool,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> Path:
    if not confirmed:
        raise EvidenceSnapshotError("导入通达信成交额备用证据前必须取得用户明确确认。")

    output_path = output_dir / f"{trade_date}_snapshot.json"
    if not output_path.exists():
        raise EvidenceSnapshotError(f"通达信成交额备用导入要求已有同日证据快照：{output_path}")

    existing_data = read_json_mapping(output_path)
    existing_snapshot = build_evidence_snapshot(trade_date, existing_data)
    if "missing_total_amount" not in existing_snapshot.missing_fields:
        raise EvidenceSnapshotError("当前快照已有有效成交额，禁止用通达信行情口径覆盖。")
    if existing_snapshot.sample_date != trade_date:
        raise EvidenceSnapshotError(
            f"当前快照样本日期不是目标交易日：{existing_snapshot.sample_date}，拒绝补入成交额。"
        )

    try:
        fallback = build_tongdaxin_market_evidence(trade_date, read_json_mapping(evidence_file))
    except TongdaxinSourceError as error:
        raise EvidenceSnapshotError(str(error)) from error

    validate_existing_market_indices(
        existing_snapshot.market,
        fallback["index_close_checks"],
        evidence_label="通达信成交额备用证据",
    )

    # 只向原 market 增加成交额字段，现有三大指数、情绪、板块、个股和人工证据保持不变。
    merged_data = existing_snapshot.to_mapping()
    market = dict(existing_snapshot.market)
    for field_name in (
        "total_amount",
        "total_amount_source",
        "total_amount_unit",
        "total_amount_quality",
        "total_amount_calculation",
        "total_amount_breakdown",
    ):
        market[field_name] = fallback[field_name]
    merged_data["market"] = market

    field_sources = dict(existing_snapshot.field_sources)
    field_sources["market"] = merge_source_names(field_sources.get("market", ""), TONGDAXIN_SOURCE)
    merged_data["field_sources"] = field_sources
    merged_data["events"] = deduplicate_events(
        [
            *existing_snapshot.events,
            {
                "title": "通达信成交额行情备用证据",
                "source": TONGDAXIN_SOURCE,
                "note": (
                    "AKShare 交易所成交额缺失后经用户确认导入；使用 000002 与 399106 的 Rows.Amount，"
                    "属于通达信行情口径，非交易所官方汇总。"
                ),
            },
        ]
    )
    merged_data.pop("missing_fields", None)
    snapshot = build_evidence_snapshot(trade_date, merged_data)

    output_path.write_text(
        json.dumps(snapshot.to_mapping(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    EvidenceSnapshotRepository(database_path).save_snapshot(snapshot)
    return output_path


# 涨跌家数使用独立确认入口，不要求成交额缺失，也不覆盖已有同日涨跌家数证据。
def import_tongdaxin_market_breadth_snapshot(
    trade_date: str,
    evidence_file: Path,
    confirmed: bool,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> Path:
    if not confirmed:
        raise EvidenceSnapshotError("导入通达信涨跌家数证据前必须取得用户明确确认。")

    output_path = output_dir / f"{trade_date}_snapshot.json"
    if not output_path.exists():
        raise EvidenceSnapshotError(f"通达信涨跌家数导入要求已有同日证据快照：{output_path}")
    existing_data = read_json_mapping(output_path)
    existing_snapshot = build_evidence_snapshot(trade_date, existing_data)
    if existing_snapshot.sample_date != trade_date:
        raise EvidenceSnapshotError(
            f"当前快照样本日期不是目标交易日：{existing_snapshot.sample_date}，拒绝补入涨跌家数。"
        )
    market = dict(existing_snapshot.market)
    if market.get("up_count") not in (None, "") or market.get("down_count") not in (
        None,
        "",
    ):
        raise EvidenceSnapshotError("当前快照已有涨跌家数证据，禁止用通达信行情口径覆盖。")

    try:
        breadth = build_tongdaxin_market_breadth_evidence(
            trade_date,
            read_json_mapping(evidence_file),
        )
    except TongdaxinSourceError as error:
        raise EvidenceSnapshotError(str(error)) from error
    validate_existing_market_indices(
        existing_snapshot.market,
        breadth["index_close_checks"],
        evidence_label="通达信涨跌家数证据",
    )

    for field_name in (
        "up_count",
        "down_count",
        "advance_decline_source",
        "advance_decline_quality",
        "advance_decline_calculation",
        "advance_decline_breakdown",
        "advance_decline_note",
    ):
        market[field_name] = breadth[field_name]
    merged_data = existing_snapshot.to_mapping()
    merged_data["market"] = market
    field_sources = dict(existing_snapshot.field_sources)
    field_sources["market"] = merge_source_names(
        field_sources.get("market", ""),
        TONGDAXIN_SOURCE,
    )
    merged_data["field_sources"] = field_sources
    merged_data["events"] = deduplicate_events(
        [
            *existing_snapshot.events,
            {
                "title": "通达信涨跌家数行情证据",
                "source": TONGDAXIN_SOURCE,
                "note": breadth["advance_decline_note"],
            },
        ]
    )
    merged_data.pop("missing_fields", None)
    snapshot = build_evidence_snapshot(trade_date, merged_data)
    output_path.write_text(
        json.dumps(snapshot.to_mapping(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    EvidenceSnapshotRepository(database_path).save_snapshot(snapshot)
    return output_path


# 个股身份与相对强弱/连续比较事实经用户确认后落盘，要求已有同日快照，不覆盖其它证据。
def import_tongdaxin_stock_facts(
    trade_date: str,
    evidence_file: Path,
    confirmed: bool,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
    candidate_history_path: Path | None = None,
    index_history_path: Path | None = None,
) -> Path:
    if not confirmed:
        raise EvidenceSnapshotError("导入通达信个股身份与强弱证据前必须取得用户明确确认。")

    output_path = output_dir / f"{trade_date}_snapshot.json"
    if not output_path.exists():
        raise EvidenceSnapshotError(f"通达信个股事实导入要求已有同日证据快照：{output_path}")
    existing_data = read_json_mapping(output_path)
    existing_trade_date = str(existing_data.get("trade_date") or trade_date)
    if existing_trade_date != trade_date:
        raise EvidenceSnapshotError(
            f"当前快照交易日期不是目标交易日：{existing_trade_date}，拒绝导入个股事实。"
        )
    existing_snapshot = build_evidence_snapshot(trade_date, existing_data)
    if existing_snapshot.sample_date != trade_date:
        raise EvidenceSnapshotError(
            f"当前快照样本日期不是目标交易日：{existing_snapshot.sample_date}，拒绝导入个股事实。"
        )

    try:
        identity_mapping = load_stock_identity_input(evidence_file, trade_date)
    except StockFactEnrichmentError as error:
        raise EvidenceSnapshotError(str(error)) from error
    merged_data = existing_snapshot.to_mapping()
    enrich_snapshot_identities(merged_data, identity_mapping)

    candidate_history_path = candidate_history_path or (
        output_dir / f"{trade_date}_review_candidate_history.json"
    )
    index_history_path = index_history_path or (
        output_dir / f"{trade_date}_tdx_index_history.json"
    )
    if candidate_history_path.exists() and index_history_path.exists():
        candidate_history = read_json_mapping(candidate_history_path)
        index_history = read_json_mapping(index_history_path)
        # 比较事实只能消费同一交易日的两个标准文件，文件名不能代替内容日期校验。
        if candidate_history.get("trade_date") != trade_date:
            raise EvidenceSnapshotError(
                "候选历史交易日期不是目标交易日，拒绝导入个股比较事实。"
            )
        if candidate_history.get("sample_date") not in (None, trade_date):
            raise EvidenceSnapshotError(
                "候选历史样本日期不是目标交易日，拒绝导入个股比较事实。"
            )
        if index_history.get("trade_date") != trade_date:
            raise EvidenceSnapshotError(
                "指数历史交易日期不是目标交易日，拒绝导入个股比较事实。"
            )
        enrich_snapshot_comparisons(merged_data, candidate_history, index_history)

    merged_data.pop("missing_fields", None)
    snapshot = build_evidence_snapshot(trade_date, merged_data)
    output_path.write_text(
        json.dumps(snapshot.to_mapping(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    EvidenceSnapshotRepository(database_path).save_snapshot(snapshot)
    return output_path


# 备用成交额必须和已有三大指数属于同一交易日；沪深收盘按展示精度交叉核对。
def validate_existing_market_indices(
    market: dict[str, Any],
    index_close_checks: dict[str, Any],
    evidence_label: str,
) -> None:
    indices = market.get("indices")
    if not isinstance(indices, list):
        raise EvidenceSnapshotError(f"当前快照缺少完整三大指数，不能导入{evidence_label}。")
    index_by_name = {
        str(item.get("name")): item
        for item in indices
        if isinstance(item, dict) and item.get("name") not in (None, "")
    }
    required_names = {"上证指数", "深证成指", "创业板指"}
    if not required_names.issubset(index_by_name):
        raise EvidenceSnapshotError(f"当前快照缺少完整三大指数，不能导入{evidence_label}。")

    for index_name, fallback_close in index_close_checks.items():
        existing_close = parse_price(index_by_name[index_name].get("close"))
        normalized_fallback_close = parse_price(fallback_close)
        if existing_close is None or normalized_fallback_close is None:
            raise EvidenceSnapshotError(f"{index_name} 收盘值不可用于通达信交叉核对。")
        if existing_close.quantize(Decimal("0.01")) != normalized_fallback_close.quantize(
            Decimal("0.01")
        ):
            raise EvidenceSnapshotError(
                f"{index_name} 收盘与通达信目标日原始行不一致，拒绝导入{evidence_label}。"
            )


def parse_price(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def merge_source_names(existing_source: str, incoming_source: str) -> str:
    sources = [source for source in existing_source.split("+") if source]
    if incoming_source not in sources:
        sources.append(incoming_source)
    return "+".join(sources)


# AKShare 真实采集必须显式传入 scope，避免默认扩大到全市场扫描。
def collect_akshare_evidence_snapshot(
    trade_date: str,
    scope: str = "market",
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
    refresh: bool = False,
) -> Path:
    output_path = output_dir / f"{trade_date}_snapshot.json"
    existing_data = read_json_mapping(output_path) if output_path.exists() else {}
    # 同日同 scope 已有有效 AKShare 事实时直接复用，避免重复请求触发上游限流。
    if not refresh and is_akshare_scope_covered(existing_data, scope):
        return output_path
    try:
        if scope == "market":
            raw_data = collect_akshare_market_evidence(trade_date)
        elif scope == "sentiment":
            raw_data = collect_akshare_sentiment_evidence(trade_date)
        elif scope == "sectors":
            raw_data = collect_akshare_sector_evidence(trade_date)
        elif scope == "stocks":
            existing_sectors = existing_data.get("sectors")
            sectors = existing_sectors if isinstance(existing_sectors, list) else []
            raw_data = collect_akshare_stock_evidence(trade_date, sectors=sectors)
        else:
            raise AkshareSourceError("当前仅支持 scope=market、sentiment、sectors 或 stocks 的最小采集。")
    except AkshareSourceError as error:
        raise EvidenceSnapshotError(str(error)) from error

    output_dir.mkdir(parents=True, exist_ok=True)
    if existing_data:
        raw_data = merge_evidence_data(existing_data, raw_data)
    else:
        raw_data = attach_field_sources(raw_data)

    snapshot = build_evidence_snapshot(trade_date, raw_data)
    output_path.write_text(
        json.dumps(snapshot.to_mapping(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    repository = EvidenceSnapshotRepository(database_path)
    repository.save_snapshot(snapshot)
    return output_path


# 单日多 scope 编排只复用既有单 scope 采集；每个 scope 独立失败，避免单点异常掩盖其它事实。
def collect_akshare_evidence_scopes(
    trade_date: str,
    scopes: list[str],
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
    refresh: bool = False,
    collector: Callable[..., Path] = collect_akshare_evidence_snapshot,
) -> tuple[EvidenceScopeCollectionResult, ...]:
    if not scopes:
        raise EvidenceSnapshotError("每日采集至少需要显式传入一个 scope。")
    if len(set(scopes)) != len(scopes):
        raise EvidenceSnapshotError("每日采集 scope 不能重复传入。")

    results: list[EvidenceScopeCollectionResult] = []
    for scope in scopes:
        try:
            output_path = collector(
                trade_date,
                scope=scope,
                output_dir=output_dir,
                database_path=database_path,
                refresh=refresh,
            )
        except EvidenceSnapshotError as error:
            results.append(EvidenceScopeCollectionResult(scope, None, str(error)))
        else:
            results.append(EvidenceScopeCollectionResult(scope, output_path, None))
    return tuple(results)


# hhxg 只允许采集返回日期与指定交易日一致的最近快照，避免把最新事实错误回补到历史日期。
def collect_hhxg_evidence_snapshot(
    trade_date: str,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> Path:
    try:
        raw_data = collect_hhxg_snapshot_evidence(trade_date)
    except HhxgSourceError as error:
        raise EvidenceSnapshotError(str(error)) from error

    output_path = output_dir / f"{trade_date}_snapshot.json"
    existing_data = read_json_mapping(output_path) if output_path.exists() else {}
    if existing_data:
        raw_data = merge_evidence_data(existing_data, raw_data)
    else:
        raw_data = attach_field_sources(raw_data)
    snapshot = build_evidence_snapshot(trade_date, raw_data)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(snapshot.to_mapping(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    EvidenceSnapshotRepository(database_path).save_snapshot(snapshot)
    return output_path


def is_akshare_scope_covered(snapshot_data: dict[str, Any], scope: str) -> bool:
    # 复用只针对已标记为 AKShare 的同日快照，避免把手工事实误当作可刷新来源。
    if snapshot_data.get("source") != "akshare":
        return False
    if scope == "market":
        market = snapshot_data.get("market")
        if isinstance(market, dict) and market.get("total_amount_source") == "tencent_index_kline_amount":
            return False
        # market scope 只有指数与交易所成交额都齐全时才可复用；缺额日期由用户主动重跑时允许再次补采。
        return (
            isinstance(market, dict)
            and bool(market.get("indices"))
            and market.get("total_amount") is not None
        )
    if scope == "sentiment":
        sentiment = snapshot_data.get("sentiment")
        required_fields = ("limit_up_count", "limit_down_count", "broken_board_rate", "highest_board")
        return isinstance(sentiment, dict) and all(sentiment.get(field) is not None for field in required_fields)
    if scope in ("sectors", "stocks"):
        items = snapshot_data.get(scope)
        return isinstance(items, list) and bool(items)
    return False


def merge_evidence_data(existing_data: dict[str, Any], incoming_data: dict[str, Any]) -> dict[str, Any]:
    # 分 scope 采集会写入同一个交易日快照；这里只按标准字段合并，避免后采集的空字段覆盖已有证据。
    merged_data = dict(existing_data)
    merged_data["source"] = incoming_data.get("source") or existing_data.get("source") or "manual"
    merged_data["sample_date"] = incoming_data.get("sample_date") or existing_data.get("sample_date")
    field_sources = read_field_sources(existing_data)
    incoming_field_sources = read_field_sources(incoming_data)
    incoming_source = str(incoming_data.get("source") or "manual")
    for field_name in ("market", "sentiment"):
        incoming_mapping = incoming_data.get(field_name)
        if isinstance(incoming_mapping, dict) and incoming_mapping:
            merged_data[field_name] = incoming_mapping
            field_sources[field_name] = incoming_field_sources.get(field_name, incoming_source)
    incoming_sectors = incoming_data.get("sectors")
    if isinstance(incoming_sectors, list) and incoming_sectors:
        existing_sectors = existing_data.get("sectors")
        merged_data["sectors"] = merge_sector_records(
            existing_sectors if isinstance(existing_sectors, list) else [],
            incoming_sectors,
            incoming_field_sources.get("sectors", incoming_source),
        )
        field_sources["sectors"] = merge_source_names(
            field_sources.get("sectors", ""),
            incoming_field_sources.get("sectors", incoming_source),
        )

    incoming_stocks = incoming_data.get("stocks")
    if isinstance(incoming_stocks, list) and incoming_stocks:
        merged_data["stocks"] = incoming_stocks
        field_sources["stocks"] = incoming_field_sources.get("stocks", incoming_source)

    existing_events = existing_data.get("events") if isinstance(existing_data.get("events"), list) else []
    incoming_events = incoming_data.get("events") if isinstance(incoming_data.get("events"), list) else []
    merged_data["events"] = deduplicate_events([*existing_events, *incoming_events])
    merged_data["field_sources"] = field_sources
    merged_data.pop("missing_fields", None)
    return merged_data


# AKShare 与 hhxg 的板块榜单口径相互独立；同源刷新替换旧组，异源采集则并列保留。
def merge_sector_records(
    existing_records: list[Any],
    incoming_records: list[Any],
    incoming_source: str,
) -> list[dict[str, Any]]:
    incoming_family = sector_source_family_from_name(incoming_source)
    retained_records = [
        dict(item)
        for item in existing_records
        if isinstance(item, dict) and sector_record_source_family(item) != incoming_family
    ]
    return [
        *retained_records,
        *(dict(item) for item in incoming_records if isinstance(item, dict)),
    ]


def sector_record_source_family(record: dict[str, Any]) -> str:
    source_type = str(record.get("source_type") or "").lower()
    if source_type.startswith("hhxg_"):
        return "hhxg"
    if source_type in {"concept", "industry", "concept_ths", "industry_ths"}:
        return "akshare"
    return sector_source_family_from_name(str(record.get("source") or "manual"))


def sector_source_family_from_name(source: str) -> str:
    lowered = source.lower()
    if "hhxg" in lowered:
        return "hhxg"
    if "akshare" in lowered:
        return "akshare"
    return lowered or "manual"


# 新建快照为每个实际写入的标准字段记录来源；空字段不产生虚假来源。
def attach_field_sources(raw_data: dict[str, Any]) -> dict[str, Any]:
    normalized_data = dict(raw_data)
    field_sources = read_field_sources(raw_data)
    source = str(raw_data.get("source") or "manual")
    for field_name in ("market", "sentiment"):
        if isinstance(raw_data.get(field_name), dict) and raw_data[field_name]:
            field_sources.setdefault(field_name, source)
    for field_name in ("sectors", "stocks"):
        if isinstance(raw_data.get(field_name), list) and raw_data[field_name]:
            field_sources.setdefault(field_name, source)
    normalized_data["field_sources"] = field_sources
    return normalized_data


def read_field_sources(raw_data: dict[str, Any]) -> dict[str, str]:
    value = raw_data.get("field_sources")
    if not isinstance(value, dict):
        return {}
    return {
        field_name: str(source)
        for field_name, source in value.items()
        if field_name in {"market", "sentiment", "sectors", "stocks"} and source not in (None, "")
    }


def deduplicate_events(events: list[Any]) -> list[dict[str, Any]]:
    deduplicated_events: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        key = (
            str(event.get("title") or ""),
            str(event.get("source") or ""),
            str(event.get("note") or ""),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduplicated_events.append(event)
    return deduplicated_events


# 检查命令只读取已生成快照，不写入新业务状态。
def check_evidence_snapshot(
    trade_date: str,
    snapshot_dir: Path = DEFAULT_EVIDENCE_DIR,
) -> EvidenceSnapshot:
    snapshot_path = snapshot_dir / f"{trade_date}_snapshot.json"
    if not snapshot_path.exists():
        raise EvidenceSnapshotError(f"未找到证据快照：{snapshot_path}")

    snapshot_data = read_json_mapping(snapshot_path)
    return build_evidence_snapshot(trade_date, snapshot_data)


def read_json_mapping(file_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise EvidenceSnapshotError(f"JSON 格式不可识别：{file_path}：{error}") from error

    if not isinstance(data, dict):
        raise EvidenceSnapshotError(f"证据文件必须是 JSON 对象：{file_path}")
    return data
