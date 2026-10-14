# 本文件覆盖核心异动票跟踪的确定性计算、采集装配、候选收集、池管理与渲染。
# 全部用例只用内存假客户端，不联网、不写正式产物目录。

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
import tempfile

from stock_review.watchlist.collect_watchlist_candidates import collect_candidates
from stock_review.watchlist.collect_watchlist_inputs import (
    collect_watchlist_inputs,
    parse_cn_amount,
    to_prefixed_symbol,
)
from stock_review.watchlist.compute_watchlist_facts import (
    REGULATION_ABNORMAL,
    REGULATION_NONE,
    REGULATION_SEVERE,
    ZONE_AT,
    ZONE_BELOW,
    ZONE_FAR,
    build_facts,
    build_reminders,
    deviation_pairs,
    interval_extremes,
    is_mid_bullish,
    moving_average,
    position_zone,
    rolling_deviation,
    scan_regulation_events,
    trading_days_since,
    volume_ratio_5d,
)
from stock_review.watchlist.manage_watchlist_pool import (
    WatchlistPoolError,
    active_items,
    add_item,
    pool_items,
    set_status,
)
from stock_review.watchlist.render_watchlist import render_watchlist
from stock_review.watchlist.watchlist_inputs import (
    DailyBar,
    WatchlistInputs,
    benchmark_for_board,
    board_for_code,
    build_item,
)


TRADE_DATE = "2026-10-14"


class _Frame:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = [dict(row) for row in rows]

    def to_dict(self, orient: str = "records") -> list[dict]:
        return [dict(row) for row in self._rows]


class FakeClient:
    def __init__(
        self,
        stock_rows: dict[str, list[dict]] | None = None,
        index_rows: dict[str, list[dict]] | None = None,
        longhubang_rows: list[dict] | None = None,
        fund_flow_rows: list[dict] | None = None,
        limit_up_rows: list[dict] | None = None,
        strong_rows: list[dict] | None = None,
    ) -> None:
        self.stock_rows = stock_rows or {}
        self.index_rows = index_rows or {}
        self.longhubang_rows = longhubang_rows or []
        self.fund_flow_rows = fund_flow_rows or []
        self.limit_up_rows = limit_up_rows or []
        self.strong_rows = strong_rows or []

    def stock_zh_a_daily(self, symbol: str, start_date: str, end_date: str, adjust: str) -> _Frame:
        return _Frame(self.stock_rows.get(symbol, []))

    def stock_zh_index_daily(self, symbol: str) -> _Frame:
        return _Frame(self.index_rows.get(symbol, []))

    def stock_lhb_detail_em(self, start_date: str, end_date: str) -> _Frame:
        return _Frame(self.longhubang_rows)

    def stock_fund_flow_individual(self, symbol: str) -> _Frame:
        return _Frame(self.fund_flow_rows)

    def stock_zt_pool_em(self, date: str) -> _Frame:
        return _Frame(self.limit_up_rows)

    def stock_zt_pool_strong_em(self, date: str) -> _Frame:
        return _Frame(self.strong_rows)


def _daily_rows(
    closes: list[float],
    volumes: list[float] | None = None,
    end_date: str = TRADE_DATE,
) -> list[dict]:
    """构造单调日期的日线；开盘=前收，高=收*1.01，低=收*0.99，保证 OHLC 合法。"""
    volumes = volumes or [1000.0] * len(closes)
    last = date.fromisoformat(end_date)
    rows: list[dict] = []
    for offset, close in enumerate(closes):
        day = last - timedelta(days=len(closes) - 1 - offset)
        rows.append(
            {
                "date": day.isoformat(),
                "open": round(close * 0.995, 4),
                "high": round(close * 1.01, 4),
                "low": round(close * 0.99, 4),
                "close": close,
                "volume": volumes[offset],
                "amount": close * volumes[offset],
                "turnover": 0.05,
            }
        )
    return rows


def _flat_rows(count: int, close: float, volume: float = 1000.0) -> list[dict]:
    return _daily_rows([close] * count, [volume] * count)


def _index_rows(closes: list[float]) -> list[dict]:
    return [
        {
            "date": row["date"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        }
        for row in _daily_rows(closes)
    ]


def _inputs_with(items: tuple, bars: dict, index: dict | None = None) -> WatchlistInputs:
    return WatchlistInputs(
        trade_date=TRADE_DATE,
        items=items,
        daily_bars={code: tuple(_to_bars(rows)) for code, rows in bars.items()},
        index_bars={code: tuple(_to_bars(rows)) for code, rows in (index or {}).items()},
        longhubang={},
        fund_flow={},
        sources={"stock_daily": "test"},
        gaps=(),
    )


def _to_bars(rows: list[dict]) -> list[DailyBar]:
    return [
        DailyBar(
            trade_date=row["date"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            amount=row.get("amount"),
            turnover=row.get("turnover"),
        )
        for row in rows
    ]


# 板块与基准指数映射：决定偏离值配对，不允许错配。
def test_board_and_benchmark_mapping() -> None:
    assert board_for_code("301251") == "创业板"
    assert board_for_code("600519") == "沪市主板"
    assert board_for_code("000001") == "深市主板"
    assert board_for_code("688981") == "科创板"
    assert board_for_code("830799") == "北交所"
    assert board_for_code("abc") == "待确认"
    assert benchmark_for_board("创业板") == "sz399006"
    assert benchmark_for_board("沪市主板") == "sh000001"


# build_item 必须由代码推导板块与基准，避免手工填写造成配对错误。
def test_build_item_derives_board() -> None:
    item = build_item("301132", "满坤科技", TRADE_DATE, "严重异常波动")
    assert item.board == "创业板"
    assert item.benchmark == "sz399006"
    assert item.status == "active"


def test_moving_average_and_zero_when_insufficient() -> None:
    assert moving_average([1.0, 2.0, 3.0], 3) == 2.0
    assert moving_average([1.0, 2.0], 5) is None


def test_interval_extremes_and_days_since() -> None:
    rows = _daily_rows([10.0, 8.0, 20.0, 15.0])
    bars = tuple(_to_bars(rows))
    low, low_date, high, high_date = interval_extremes(bars, 60)
    assert low == round(8.0 * 0.99, 4)
    assert high == round(20.0 * 1.01, 4)
    assert low_date < high_date
    # 高点之后还有一根，故距高点为 1 个交易日。
    assert trading_days_since(bars, high_date) == 1
    assert trading_days_since(bars, "1999-01-01") is None


def test_volume_ratio_uses_previous_five_days() -> None:
    closes = [10.0] * 6
    volumes = [1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 2000.0]
    bars = tuple(_to_bars(_daily_rows(closes, volumes)))
    assert volume_ratio_5d(bars) == 2.0


# 偏离值为同交易日「个股涨跌幅 − 基准指数涨跌幅」，基准不动时等于个股涨幅。
def test_deviation_pairs_subtract_benchmark() -> None:
    stock = tuple(_to_bars(_daily_rows([10.0, 11.0, 12.1])))
    index = tuple(_to_bars(_index_rows([100.0, 100.0, 100.0])))
    pairs = deviation_pairs(stock, index)
    assert len(pairs) == 2
    assert abs(pairs[0][1] - 10.0) < 1e-6
    assert abs(pairs[1][1] - 10.0) < 1e-6
    assert rolling_deviation(pairs, 2) == 20.0


# 连续 10 日累计偏离 ≥100% 必须判为严重异常波动。
def test_scan_regulation_detects_severe_deviation() -> None:
    closes = [10.0]
    for _ in range(30):
        closes.append(round(closes[-1] * 1.1, 4))
    stock = tuple(_to_bars(_daily_rows(closes)))
    index = tuple(_to_bars(_index_rows([100.0] * len(closes))))
    pairs = deviation_pairs(stock, index)
    events = scan_regulation_events(pairs, "创业板")
    kinds = {event["kind"] for event in events}
    assert REGULATION_SEVERE in kinds
    assert any(event["window"] == 10 for event in events)


# 3 日累计偏离达板块阈值（创业板 30%）判为异常波动。
def test_scan_regulation_detects_abnormal_deviation() -> None:
    closes = [10.0, 11.0, 12.1, 13.3, 20.0, 20.0]
    stock = tuple(_to_bars(_daily_rows(closes)))
    index = tuple(_to_bars(_index_rows([100.0] * len(closes))))
    pairs = deviation_pairs(stock, index)
    events = scan_regulation_events(pairs, "创业板")
    assert any(event["kind"] == REGULATION_ABNORMAL and event["window"] == 3 for event in events)


def test_scan_regulation_quiet_when_below_threshold() -> None:
    closes = [10.0] * 12
    stock = tuple(_to_bars(_daily_rows(closes)))
    index = tuple(_to_bars(_index_rows([100.0] * len(closes))))
    assert scan_regulation_events(deviation_pairs(stock, index), "创业板") == []


# 位置分层：远离 / 接近 / 到线 / 跌破，阈值只做客观距离分层。
def test_position_zone_layers() -> None:
    assert position_zone(None) == "不可判定"
    assert position_zone(15.0) == ZONE_FAR
    assert position_zone(5.0) == "接近均线"
    assert position_zone(0.5) == ZONE_AT
    assert position_zone(-5.0) == ZONE_BELOW


# 全平收盘时 MA21 等于收盘价，应落到「到线」并产生提醒。
def test_build_facts_and_reminder_at_ma21() -> None:
    item = build_item("301251", "威尔高", TRADE_DATE, "严重异常波动")
    bars = {"301251": _flat_rows(25, 10.0)}
    index = {"sz399006": _index_rows([100.0] * 25)}
    facts = build_facts(_inputs_with((item,), bars, index))
    assert len(facts) == 1
    fact = facts[0]
    assert fact.ma21 == 10.0
    assert fact.close_vs_ma21_percent == 0.0
    assert fact.position_zone == ZONE_AT
    assert any("[到线]" in item_text for item_text in build_reminders(facts))


# 放量中阳（+6%、量比 2 倍）即便远离均线也应提示「可能不等 21 日线即启动」。
def test_mid_bullish_reminder_without_touching_ma21() -> None:
    item = build_item("301132", "满坤科技", TRADE_DATE, "严重异常波动")
    closes = [10.0] * 24 + [10.0 * 1.06]
    volumes = [1000.0] * 24 + [2000.0]
    bars = {"301132": _daily_rows(closes, volumes)}
    index = {"sz399006": _index_rows([100.0] * len(closes))}
    facts = build_facts(_inputs_with((item,), bars, index))
    fact = facts[0]
    assert fact.volume_surge is True
    assert is_mid_bullish(fact) is True
    text = "\n".join(build_reminders(facts))
    assert "[放量中阳]" in text


# 停用对象不进入事实装配。
def test_paused_item_excluded() -> None:
    item = build_item("301251", "威尔高", TRADE_DATE, "x", status="paused")
    bars = {"301251": _flat_rows(25, 10.0)}
    assert build_facts(_inputs_with((item,), bars)) == ()


# 采集层：假客户端装配日线、指数、龙虎榜与资金流，缺口应为空。
def test_collect_inputs_with_fake_client() -> None:
    item = build_item("301251", "威尔高", TRADE_DATE, "严重异常波动")
    client = FakeClient(
        stock_rows={"sz301251": _daily_rows([10.0] * 30)},
        index_rows={"sz399006": _index_rows([100.0] * 30)},
        longhubang_rows=[
            {"代码": "301251", "名称": "威尔高", "上榜日": "2026-09-16", "上榜原因": "连续三个交易日内，涨幅偏离值累计达到30%的证券", "龙虎榜净买额": 1000.0, "龙虎榜买入额": 2000.0, "龙虎榜卖出额": 1000.0, "换手率": 5.0},
        ],
        fund_flow_rows=[
            {"股票代码": "301251", "股票简称": "威尔高", "净额": "1.23亿", "流入资金": "2.00亿", "流出资金": "0.77亿", "涨跌幅": "6.41%"},
        ],
    )
    inputs = collect_watchlist_inputs(TRADE_DATE, (item,), ak_client=client)
    assert inputs.gaps == ()
    assert len(inputs.daily_bars["301251"]) == 30
    assert len(inputs.index_bars["sz399006"]) == 30
    assert inputs.longhubang["301251"]["count"] == 1
    assert inputs.fund_flow["301251"]["net_amount"] == 123000000.0


# 指数接口返回全历史（创业板指约 4000 根），必须按回看窗口下限截断，
# 否则每日快照会被十余年的指数历史撑爆（实测单日 632KB）。
def test_collect_inputs_trims_index_to_lookback_window() -> None:
    item = build_item("301251", "威尔高", TRADE_DATE, "严重异常波动")
    ancient = [dict(row, date="2010-01-04") for row in _index_rows([1.0])]
    client = FakeClient(
        stock_rows={"sz301251": _daily_rows([10.0] * 5)},
        index_rows={"sz399006": ancient + _index_rows([100.0] * 5)},
    )
    inputs = collect_watchlist_inputs(TRADE_DATE, (item,), ak_client=client)
    bars = inputs.index_bars["sz399006"]
    window_start = (date.fromisoformat(TRADE_DATE) - timedelta(days=250)).isoformat()
    assert len(bars) == 5
    assert all(bar.trade_date >= window_start for bar in bars)
    assert "2010-01-04" not in {bar.trade_date for bar in bars}


def test_collect_inputs_records_gap_on_failure() -> None:
    class BrokenClient(FakeClient):
        def stock_zh_a_daily(self, symbol: str, start_date: str, end_date: str, adjust: str) -> _Frame:
            raise RuntimeError("boom")

    item = build_item("301251", "威尔高", TRADE_DATE, "x")
    inputs = collect_watchlist_inputs(TRADE_DATE, (item,), ak_client=BrokenClient())
    assert inputs.daily_bars["301251"] == ()
    assert any("日线采集失败" in gap for gap in inputs.gaps)


# 候选收集：龙虎榜两次上榜触发入围，10 日翻倍应判为严重异常波动。
def test_collect_candidates_flags_severe_deviation() -> None:
    closes = [10.0]
    for _ in range(30):
        closes.append(round(closes[-1] * 1.1, 4))
    client = FakeClient(
        stock_rows={"sz301251": _daily_rows(closes)},
        index_rows={"sz399006": _index_rows([100.0] * len(closes))},
        longhubang_rows=[
            {"代码": "301251", "名称": "威尔高", "上榜日": "2026-09-16", "上榜原因": "连续三个交易日内，涨幅偏离值累计达到30%的证券"},
            {"代码": "301251", "名称": "威尔高", "上榜日": "2026-08-06", "上榜原因": "日涨幅偏离值达到7%的前5只证券"},
        ],
    )
    payload = collect_candidates(TRADE_DATE, pool_codes=("301251",), ak_client=client, top_n=10)
    assert payload["evaluated_count"] == 1
    record = payload["candidates"][0]
    assert record["code"] == "301251"
    assert record["regulation_level"] == REGULATION_SEVERE
    assert record["in_pool"] is True
    assert record["interval_gain_percent"] > 100


# ST 类不纳入候选。
def test_collect_candidates_skips_st() -> None:
    client = FakeClient(
        longhubang_rows=[
            {"代码": "000530", "名称": "ST冰山", "上榜日": "2026-10-14", "上榜原因": "连续三个交易日内，涨幅偏离值累计达到20%的证券"},
            {"代码": "000530", "名称": "ST冰山", "上榜日": "2026-10-13", "上榜原因": "日涨幅偏离值达到7%的前5只证券"},
        ],
        stock_rows={"sz000530": _daily_rows([10.0] * 30)},
        index_rows={"sz399001": _index_rows([100.0] * 30)},
    )
    payload = collect_candidates(TRADE_DATE, ak_client=client, top_n=10)
    assert payload["candidates"] == []


def test_parse_cn_amount() -> None:
    assert parse_cn_amount("5.15亿") == 515000000.0
    assert parse_cn_amount("-1.98亿") == -198000000.0
    assert parse_cn_amount("3282.77万") == 32827700.0
    assert parse_cn_amount("--") is None
    assert parse_cn_amount("abc") is None


def test_to_prefixed_symbol() -> None:
    assert to_prefixed_symbol("301251") == "sz301251"
    assert to_prefixed_symbol("600519") == "sh600519"
    assert to_prefixed_symbol("830799") == "bj830799"


# 池管理：显式加入、去重、状态变更后不再进入启用列表。
def test_pool_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pool.json"
        add_item("301251", "威尔高", "严重异常波动", TRADE_DATE, path=path)
        add_item("301132", "满坤科技", "异常波动", TRADE_DATE, path=path)
        assert len(pool_items(path)) == 2
        assert len(active_items(path)) == 2
        try:
            add_item("301251", "威尔高", "重复", TRADE_DATE, path=path)
        except WatchlistPoolError as error:
            assert "已存在" in str(error)
        else:
            raise AssertionError("重复入池必须报错")
        set_status("301251", "paused", "已破位", path=path)
        assert [item.code for item in active_items(path)] == ["301132"]


def test_pool_rejects_unknown_board_and_empty_reason() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pool.json"
        try:
            add_item("abc", "某票", "x", TRADE_DATE, path=path)
        except WatchlistPoolError as error:
            assert "板块" in str(error)
        else:
            raise AssertionError("无法判定板块时必须拒绝")
        try:
            add_item("301251", "威尔高", "", TRADE_DATE, path=path)
        except WatchlistPoolError as error:
            assert "原因" in str(error)
        else:
            raise AssertionError("缺少入池原因时必须拒绝")


# 渲染：五个小节齐备，无数据时不留空表格。
def test_render_watchlist_sections() -> None:
    item = build_item("301251", "威尔高", TRADE_DATE, "严重异常波动")
    bars = {"301251": _flat_rows(25, 10.0)}
    index = {"sz399006": _index_rows([100.0] * 25)}
    inputs = _inputs_with((item,), bars, index)
    facts = build_facts(inputs)
    markdown = render_watchlist(inputs, facts, generated_at="2026-10-14T21:00:00+08:00")
    for header in ("## 一、提醒", "## 二、位置与量价", "## 三、异动认定与资金", "## 四、逐票事实", "## 五、数据来源与缺口"):
        assert header in markdown
    assert "威尔高" in markdown
    assert "未达认定" in markdown


def test_render_watchlist_empty_pool() -> None:
    inputs = WatchlistInputs(
        trade_date=TRADE_DATE,
        items=(),
        daily_bars={},
        index_bars={},
        longhubang={},
        fund_flow={},
        sources={"stock_daily": "test"},
        gaps=(),
    )
    markdown = render_watchlist(inputs, (), generated_at="2026-10-14T21:00:00+08:00")
    assert "跟踪池为空" in markdown or "本次无位置或量价提醒" in markdown


# 端到端：临时池 + 假客户端 → 跟踪表与 JSON 同源产出，不触碰正式目录。
def test_run_watchlist_end_to_end() -> None:
    from stock_review.watchlist.run_watchlist import run_watchlist

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pool_path = root / "pool.json"
        add_item("301251", "威尔高", "严重异常波动", TRADE_DATE, path=pool_path)
        client = FakeClient(
            stock_rows={"sz301251": _daily_rows([10.0] * 30)},
            index_rows={"sz399006": _index_rows([100.0] * 30)},
        )
        result = run_watchlist(
            TRADE_DATE,
            pool_path=pool_path,
            report_dir=root / "reports",
            data_dir=root / "data",
            ak_client=client,
        )
        assert result["fact_count"] == 1
        assert Path(result["report_path"]).exists()
        payload = json.loads(Path(result["data_path"]).read_text(encoding="utf-8"))
        assert payload["facts"][0]["code"] == "301251"
        assert "威尔高" in result["markdown"]
