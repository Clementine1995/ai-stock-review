# 本文件负责为决策卡采集当日与近期事实，并把采集失败转成显式缺口而不是中断整次运行。
# 采集统一复用 stock_review.evidence.akshare_source 的适配器，避免在本包内另建第二套外部调用。

from __future__ import annotations

from typing import Any

from stock_review.evidence import akshare_source as akshare
from stock_review.decision.decision_inputs import (
    DecisionInputs,
    IndexDay,
    SentimentDay,
    TdxEnrichment,
)

# 情绪序列默认回看天数；STEP 3 需要前一交易日对比，少于 2 天时退潮/修复方向不可判定。
DEFAULT_LOOKBACK_DAYS = 5
# 指数阶段默认回看天数；STEP 1 需要区分箱体位置与短趋势。
INDEX_LOOKBACK_DAYS = 10

INDEX_SYMBOLS = akshare.INDEX_SYMBOLS
SENTIMENT_POOL_METHODS = ("stock_zt_pool_em", "stock_zt_pool_zbgc_em", "stock_zt_pool_dtgc_em")


# 采集入口：每个来源独立捕获失败并登记缺口，保证部分来源可用时仍能产出决策卡。
def collect_decision_inputs(
    trade_date: str,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ak_client: Any | None = None,
    capture_sentiment_history: bool = True,
    tdx_enrichment: TdxEnrichment | None = None,
) -> DecisionInputs:
    client = ak_client or akshare.import_akshare_client()
    pacer = akshare.AkshareRequestPacer()
    events: list[dict[str, str]] = []
    gaps: list[str] = []
    sources: dict[str, str] = {}

    indices, total_amount, total_amount_source = _collect_index_and_amount(
        client, trade_date, pacer, sources, gaps
    )
    breadth = _collect_market_breadth(client, gaps)
    if breadth is not None:
        sources["breadth"] = "akshare:stock_market_activity_legu"

    query_dates = _recent_trade_dates(client, trade_date, lookback_days, gaps)
    if trade_date not in query_dates:
        query_dates = [*query_dates, trade_date]

    sentiment_today, sentiment_history, today_limit_up_rows, today_broken_rows = _collect_sentiment_series(
        client, trade_date, query_dates, pacer, capture_sentiment_history, sources, gaps
    )
    amount_history = _collect_amount_history(client, query_dates, pacer, events, gaps)

    sectors = _collect_sectors(client, trade_date, tdx_enrichment, sources, gaps)
    index_history = _collect_index_history(client, pacer, events, sources, gaps)

    if tdx_enrichment is not None and (
        tdx_enrichment.board_quotes or tdx_enrichment.stock_themes or tdx_enrichment.news
    ):
        sources["tdx"] = tdx_enrichment.provider

    return DecisionInputs(
        trade_date=trade_date,
        indices=indices,
        total_amount=total_amount,
        total_amount_source=total_amount_source,
        breadth=breadth,
        sentiment_today=sentiment_today,
        sentiment_history=tuple(sentiment_history),
        amount_history=tuple(amount_history),
        index_history=index_history,
        sectors=tuple(sectors),
        limit_up_stocks=tuple(_select_limit_up_stocks(today_limit_up_rows)),
        broken_board_stocks=tuple(_select_broken_board_stocks(today_broken_rows)),
        sources=sources,
        gaps=tuple(gaps),
        tdx=tdx_enrichment,
    )


# 指数与两市成交额沿用已验证的市场层适配器，成交额缺失时不写部分值。
def _collect_index_and_amount(
    client: Any,
    trade_date: str,
    pacer: akshare.AkshareRequestPacer,
    sources: dict[str, str],
    gaps: list[str],
) -> tuple[dict[str, dict[str, Any]], float | None, str | None]:
    try:
        evidence = akshare.collect_akshare_market_evidence(trade_date, client)
    except Exception as error:  # noqa: BLE001
        gaps.append(f"指数与成交额采集失败：{_brief_error(error)}")
        return {}, None, None

    market = evidence.get("market") or {}
    indices = {str(row.get("symbol")): row for row in market.get("indices") or []}
    if indices:
        sources["index"] = "akshare:stock_zh_index_daily_em"
    else:
        gaps.append("指数日线未返回任何指数记录。")

    total_amount = market.get("total_amount")
    total_amount_source = market.get("total_amount_source")
    if total_amount is None:
        gaps.append("两市 A 股成交额未取得，STEP 1 量能档位不可判定。")
    return indices, total_amount, total_amount_source


# 涨跌家数用于判断普涨/普跌，是情绪修复与否的关键对照；取不到时只登记缺口。
def _collect_market_breadth(client: Any, gaps: list[str]) -> dict[str, Any] | None:
    try:
        frame = client.stock_market_activity_legu()
    except Exception as error:  # noqa: BLE001
        gaps.append(f"全市场涨跌家数采集失败：{_brief_error(error)}")
        return None

    if not hasattr(frame, "to_dict"):
        gaps.append("全市场涨跌家数返回格式不可识别。")
        return None

    rows = frame.to_dict("records")
    breadth: dict[str, Any] = {"raw": {}, "source": "akshare:stock_market_activity_legu"}
    for row in rows:
        item = akshare.format_optional_text(row.get("item"))
        value = row.get("value")
        if not item:
            continue
        if item == "统计日期":
            breadth["sample_time"] = akshare.format_optional_text(value)
            continue
        # 活跃度是百分比字符串，其余为家数整数；分开解析，避免把 57.22% 当成 57 家。
        if item == "活跃度":
            breadth["activity_percent"] = _parse_percent_text(value)
        else:
            breadth["raw"][item] = akshare.parse_int(value)
    for key, label in (
        ("上涨", "up_count"),
        ("下跌", "down_count"),
        ("平盘", "flat_count"),
        ("涨停", "limit_up_count"),
        ("跌停", "limit_down_count"),
    ):
        if breadth["raw"].get(key) is not None:
            breadth[label] = breadth["raw"][key]
    return breadth


# 情绪序列按交易日逐个回看；单日失败只影响该日，不覆盖其它交易日结果。
def _collect_sentiment_series(
    client: Any,
    trade_date: str,
    query_dates: list[str],
    pacer: akshare.AkshareRequestPacer,
    capture_history: bool,
    sources: dict[str, str],
    gaps: list[str],
) -> tuple[SentimentDay | None, list[SentimentDay], list[dict[str, Any]], list[dict[str, Any]]]:
    if not capture_history:
        query_dates = [trade_date]

    history: list[SentimentDay] = []
    today_row: SentimentDay | None = None
    today_limit_up_rows: list[dict[str, Any]] = []
    today_broken_rows: list[dict[str, Any]] = []

    for query_date in query_dates:
        try:
            limit_up = akshare.get_sentiment_rows(client, "stock_zt_pool_em", query_date.replace("-", ""), pacer)
            broken = akshare.get_sentiment_rows(client, "stock_zt_pool_zbgc_em", query_date.replace("-", ""), pacer)
            limit_down = akshare.get_sentiment_rows(client, "stock_zt_pool_dtgc_em", query_date.replace("-", ""), pacer)
        except Exception as error:  # noqa: BLE001
            gaps.append(f"{query_date} 情绪池采集失败：{_brief_error(error)}")
            continue

        day = _build_sentiment_day(query_date, limit_up, broken, limit_down)
        history.append(day)
        if query_date == trade_date:
            today_row = day
            today_limit_up_rows = limit_up
            today_broken_rows = broken

    if today_row is None:
        gaps.append("当日涨停/跌停/炸板池未取得，STEP 3 情绪阶段不可判定。")
    else:
        sources["sentiment"] = "akshare:stock_zt_pool_em+stock_zt_pool_zbgc_em+stock_zt_pool_dtgc_em"
    return today_row, history, today_limit_up_rows, today_broken_rows


# 板块来源名映射：适配器实际可能返回东财概念/行业榜，或整体退化为同花顺行业口径。
SECTOR_SOURCE_LABELS = {
    "concept": "stock_board_concept_name_em",
    "industry": "stock_board_industry_name_em",
    "industry_ths": "stock_board_industry_summary_ths（同花顺口径）",
}


def sector_source_label(sectors: list[dict[str, Any]]) -> str:
    """按板块记录的实际 source_type 生成来源描述，保证来源栏与采集口径一致。"""
    seen: list[str] = []
    for row in sectors:
        source_type = str(row.get("source_type") or "")
        if source_type and source_type not in seen:
            seen.append(source_type)
    if not seen:
        return "akshare:板块接口"
    return "akshare:" + "+".join(SECTOR_SOURCE_LABELS.get(item, item) for item in seen)


# 板块证据同时承载 STEP 4 的方向候选和候选对象的领涨股来源。
def _collect_sectors(
    client: Any,
    trade_date: str,
    tdx_enrichment: TdxEnrichment | None,
    sources: dict[str, str],
    gaps: list[str],
) -> list[dict[str, Any]]:
    try:
        evidence = akshare.collect_akshare_sector_evidence(trade_date, client)
    except Exception as error:  # noqa: BLE001
        gaps.append(f"板块涨幅榜采集失败：{_brief_error(error)}")
        return []
    sectors = list(evidence.get("sectors") or [])
    if sectors:
        # 来源按实际返回口径标注：东财接口缺失时适配器会退化为同花顺行业口径，不能仍写东财接口名。
        sources["sectors"] = sector_source_label(sectors)
    else:
        gaps.append("板块接口未返回有效板块。")
    # 概念与行业互为补充：若 akshare 概念榜缺失，但通达信快照已提供概念板块行情，则不再登记该缺口。
    has_concept = any(row.get("source_type") == "concept" for row in sectors)
    tdx_has_concept = bool(
        tdx_enrichment and any(item.kind == "concept" for item in tdx_enrichment.board_quotes)
    )
    if sectors and not has_concept and not tdx_has_concept:
        gaps.append("概念板块证据缺失，板块主线仅由行业板块与涨停聚集度支撑。")
    return sectors


# 两市成交额序列用于 STEP 1.2 的「量能急剧变化」判断；单日失败保留空值并登记缺口。
def _collect_amount_history(
    client: Any,
    query_dates: list[str],
    pacer: akshare.AkshareRequestPacer,
    events: list[dict[str, str]],
    gaps: list[str],
) -> list[tuple[str, float | None]]:
    history: list[tuple[str, float | None]] = []
    missing = 0
    for query_date in query_dates:
        try:
            total_amount, _ = akshare.collect_exchange_a_share_total_amount(
                client, query_date, pacer, events
            )
        except Exception:  # noqa: BLE001
            total_amount = None
        if total_amount is None:
            missing += 1
        history.append((query_date, float(total_amount) if total_amount is not None else None))
    if missing == len(query_dates):
        gaps.append("两市成交额序列全部缺失，量能变化不可判定。")
    elif missing:
        gaps.append(f"两市成交额序列有 {missing}/{len(query_dates)} 个交易日缺失。")
    return history


# 指数历史用于 STEP 1 的箱体位置与短趋势判断，逐指数独立采集。
def _collect_index_history(
    client: Any,
    pacer: akshare.AkshareRequestPacer,
    events: list[dict[str, str]],
    sources: dict[str, str],
    gaps: list[str],
) -> dict[str, tuple[IndexDay, ...]]:
    history: dict[str, tuple[IndexDay, ...]] = {}
    for symbol, name in INDEX_SYMBOLS:
        try:
            rows = akshare.get_index_rows(client, symbol, events, pacer)
        except Exception as error:  # noqa: BLE001
            gaps.append(f"{name} 日线历史采集失败：{_brief_error(error)}")
            continue
        days = tuple(_build_index_day(row) for row in rows if isinstance(row, dict))
        days = tuple(day for day in days if day is not None)
        if days:
            history[symbol] = days[-INDEX_LOOKBACK_DAYS:]
    if history:
        sources["index_history"] = "akshare:stock_zh_index_daily_em"
    else:
        gaps.append("指数日线历史为空，STEP 1 阶段判断不可判定。")
    return history


# 交易日历来自 AKShare；取不到时退化为只采集当日，不臆造交易日序列。
def _recent_trade_dates(
    client: Any, trade_date: str, lookback_days: int, gaps: list[str]
) -> list[str]:
    try:
        frame = client.tool_trade_date_hist_sina()
        dates = sorted(akshare.normalize_date(value) for value in frame["trade_date"].tolist())
    except Exception as error:  # noqa: BLE001
        gaps.append(f"交易日历采集失败，情绪序列退化为仅当日：{_brief_error(error)}")
        return [trade_date]
    return [value for value in dates if value <= trade_date][-lookback_days:] or [trade_date]


def _build_sentiment_day(
    trade_date: str,
    limit_up_rows: list[dict[str, Any]],
    broken_rows: list[dict[str, Any]],
    limit_down_rows: list[dict[str, Any]],
) -> SentimentDay:
    limit_up_count = len(limit_up_rows)
    broken_board_count = len(broken_rows)
    touched = limit_up_count + broken_board_count
    highest_board = max(
        (
            value
            for row in limit_up_rows
            if (value := akshare.parse_int(akshare.get_first_value(row, ("连板数",)))) is not None
        ),
        default=None,
    )
    return SentimentDay(
        trade_date=trade_date,
        limit_up_count=limit_up_count,
        limit_down_count=len(limit_down_rows),
        broken_board_count=broken_board_count,
        broken_board_rate=(round(broken_board_count / touched, 4) if touched else None),
        highest_board=highest_board,
    )


def _build_index_day(row: dict[str, Any]) -> IndexDay | None:
    trade_date = akshare.normalize_date(akshare.get_first_value(row, ("date", "日期")))
    close = akshare.parse_decimal(akshare.get_first_value(row, ("close", "收盘")))
    if not trade_date or close is None:
        return None
    return IndexDay(
        trade_date=trade_date,
        close=float(close),
        open=_optional_number(row, ("open", "开盘")),
        high=_optional_number(row, ("high", "最高")),
        low=_optional_number(row, ("low", "最低")),
        amount=_optional_number(row, ("amount", "成交额")),
    )


# 涨停池只保留可追溯到连板数的记录，并把板数、所属行业、封板质量带到候选区。
def _select_limit_up_stocks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stocks: list[dict[str, Any]] = []
    for row in rows:
        board_count = akshare.parse_int(akshare.get_first_value(row, ("连板数",)))
        code = akshare.format_optional_text(akshare.get_first_value(row, ("代码",)))
        name = akshare.format_optional_text(akshare.get_first_value(row, ("名称",)))
        if not code or not name:
            continue
        stocks.append(
            {
                "code": code,
                "name": name,
                "board_count": board_count,
                "sector": akshare.format_optional_text(akshare.get_first_value(row, ("所属行业",))),
                "change_percent": _optional_number(row, ("涨跌幅",)),
                "seal_amount": _optional_number(row, ("封板资金",)),
                "broken_times": akshare.parse_int(akshare.get_first_value(row, ("炸板次数",))),
                "turnover_rate": _optional_number(row, ("换手率",)),
                "role": "连板事实候选",
                "role_source": f"akshare:stock_zt_pool_em（连板数 {board_count}）" if board_count else "akshare:stock_zt_pool_em",
            }
        )
    return stocks


# 炸板池用于判断封板质量与次日反包候选，只保留代码、名称与当日涨幅。
def _select_broken_board_stocks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stocks: list[dict[str, Any]] = []
    for row in rows:
        code = akshare.format_optional_text(akshare.get_first_value(row, ("代码",)))
        name = akshare.format_optional_text(akshare.get_first_value(row, ("名称",)))
        if not code or not name:
            continue
        stocks.append(
            {
                "code": code,
                "name": name,
                "change_percent": _optional_number(row, ("涨跌幅",)),
                "turnover_rate": _optional_number(row, ("换手率",)),
                "role": "炸板事实候选",
                "role_source": "akshare:stock_zt_pool_zbgc_em",
            }
        )
    return stocks


def _optional_number(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    value = akshare.parse_decimal(akshare.get_first_value(row, keys))
    return float(value) if value is not None else None


def _parse_percent_text(value: Any) -> float | None:
    text = akshare.format_optional_text(value)
    if not text:
        return None
    parsed = akshare.parse_decimal(text.replace("%", ""))
    # 活跃度按百分数存放；显式转 float，避免 Decimal 经 JSON 序列化后退化成字符串。
    return float(parsed) if parsed is not None else None


def _brief_error(error: Exception) -> str:
    return f"{type(error).__name__}: {str(error)[:160]}"
