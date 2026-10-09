# 本文件验证决策卡的规则判定与渲染边界，全部使用合成输入，不访问网络、真实数据库或正式报告目录。

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from stock_review.decision.decision_inputs import (
    DecisionInputs,
    IndexDay,
    SentimentDay,
    TdxBoardQuote,
    TdxEnrichment,
    TdxNews,
    TdxTheme,
)
from stock_review.decision.read_market_state import (
    build_candidates,
    read_amount_band,
    read_decision,
    read_market_stage,
    read_participation,
    read_position_band,
    read_sector_leadership,
    read_sentiment_stage,
)
from stock_review.decision.render_decision_card import render_decision_card
from stock_review.decision.tdx_snapshot import inspect_tdx_snapshot, load_tdx_enrichment


def _index_days(closes: list[float]) -> tuple[IndexDay, ...]:
    return tuple(
        IndexDay(trade_date=f"2026-01-{index + 1:02d}", close=value)
        for index, value in enumerate(closes)
    )


def _sentiment(
    trade_date: str,
    limit_up: int,
    limit_down: int,
    broken: int,
    highest_board: int | None,
) -> SentimentDay:
    touched = limit_up + broken
    return SentimentDay(
        trade_date=trade_date,
        limit_up_count=limit_up,
        limit_down_count=limit_down,
        broken_board_count=broken,
        broken_board_rate=(round(broken / touched, 4) if touched else None),
        highest_board=highest_board,
    )


# 量能档位必须按 stock-review.md 的三档口径分档，并输出环比变化。
def test_amount_band_splits_by_total_amount_and_reports_change() -> None:
    reading = read_amount_band(
        1_900_000_000_000.0,
        (("2026-10-08", 1_680_000_000_000.0), ("2026-10-09", 1_900_000_000_000.0)),
    )
    assert reading["label"] == "1万亿–2万亿"
    assert reading["amount_yi"] == 19000.0
    assert any("环比" in item for item in reading["evidence"])


def test_amount_band_is_undetermined_without_total_amount() -> None:
    reading = read_amount_band(None)
    assert reading["label"] == "不可判定"


# 指数阶段应区分主升、下降与震荡，并在交易日不足时拒绝判断。
def test_market_stage_detects_uptrend_near_box_top() -> None:
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 110.0]
    assert read_market_stage({"sh000001": _index_days(closes)})["label"] == "主升期"


def test_market_stage_detects_downtrend_below_moving_averages() -> None:
    closes = [110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 104.0, 103.0, 102.0, 100.0]
    assert read_market_stage({"sh000001": _index_days(closes)})["label"] == "下降期"


def test_market_stage_is_undetermined_with_short_history() -> None:
    assert read_market_stage({"sh000001": _index_days([100.0, 101.0])})["label"] == "不可判定"


# 情绪阶段按有序规则命中，且必须在缺失当日事实时拒绝判断。
def test_sentiment_stage_detects_ice_point() -> None:
    today = _sentiment("2026-10-09", limit_up=20, limit_down=40, broken=10, highest_board=3)
    breadth = {"up_count": 800, "down_count": 3000}
    assert read_sentiment_stage(today, (today,), breadth)["label"] == "冰点日"


def test_sentiment_stage_detects_main_uptrend() -> None:
    today = _sentiment("2026-10-09", limit_up=90, limit_down=3, broken=10, highest_board=7)
    breadth = {"up_count": 3200, "down_count": 1500}
    assert read_sentiment_stage(today, (today,), breadth)["label"] == "主升期"


def test_sentiment_stage_detects_fade_against_previous_day() -> None:
    previous = _sentiment("2026-10-08", limit_up=80, limit_down=5, broken=10, highest_board=7)
    today = _sentiment("2026-10-09", limit_up=40, limit_down=6, broken=10, highest_board=4)
    assert read_sentiment_stage(today, (previous, today), None)["label"] == "退潮期"


def test_sentiment_stage_is_undetermined_without_today_facts() -> None:
    assert read_sentiment_stage(None, (), None)["label"] == "不可判定"


# 参与倾向由指数阶段与情绪阶段共同决定，任一不可判定时降级为待确认。
def test_participation_refuses_when_stage_undetermined() -> None:
    reading = read_participation({"label": "不可判定"}, {"label": "上升期"})
    assert reading["label"] == "待确认"


def test_participation_blocks_on_sentiment_fade_in_oscillating_market() -> None:
    reading = read_participation({"label": "震荡期"}, {"label": "退潮期"})
    assert reading["label"] == "不参与"


# 仓位区间必须随参与倾向与指数阶段收缩，并在不参与时归零。
def test_position_band_halves_in_downtrend() -> None:
    band = read_position_band(
        {"label": "2万亿以上", "evidence": []},
        {"label": "下降期"},
        {"label": "上升期"},
        {"label": "谨慎参与", "evidence": []},
    )
    assert band["total_range"] == (5, 10)


def test_position_band_is_zero_when_not_participating() -> None:
    band = read_position_band(
        {"label": "2万亿以上", "evidence": []},
        {"label": "震荡期"},
        {"label": "退潮期"},
        {"label": "不参与", "evidence": []},
    )
    assert band["total_range"] == (0, 0)


# 渲染必须给出四个问题的结论，并在缺数据时写出待确认而不是编造结论。
def test_render_outputs_four_answers_and_gaps() -> None:
    inputs = DecisionInputs(
        trade_date="2026-10-09",
        indices={},
        total_amount=None,
        total_amount_source=None,
        breadth=None,
        sentiment_today=None,
        sentiment_history=(),
        amount_history=(),
        index_history={},
        sectors=(),
        limit_up_stocks=(),
        broken_board_stocks=(),
        sources={},
        gaps=("指数日线历史为空，STEP 1 阶段判断不可判定。",),
    )
    markdown = render_decision_card(inputs, read_decision(inputs), generated_at="2026-10-09T22:00:00+08:00")
    assert "当前市场情绪" in markdown
    assert "明天能不能买" in markdown
    assert "买什么" in markdown
    assert "买多少" in markdown
    assert "指数日线历史为空" in markdown
    assert "不可判定" in markdown


def _tdx() -> TdxEnrichment:
    return TdxEnrichment(
        provider="tdx-connector",
        generated_at="2026-10-09T22:38:00+08:00",
        code_lookup={"捷成股份": {"code": "300182", "setcode": "0"}},
        board_quotes=(
            TdxBoardQuote(code="880710", name="种业", kind="concept", change_percent=6.89, up_count=13),
            TdxBoardQuote(code="881262", name="电池", kind="industry", change_percent=2.40, up_count=71, down_count=28),
        ),
        stock_themes={"600825": TdxTheme(code="600825", theme="出版业+资产重组", reason="收购界面财联社")},
        news=(TdxNews(title="焦点复盘", source="财联社", published_at="2026-10-09 17:40", summary="69 股涨停。"),),
    )


# 快照缺失是合法状态：必须返回 None，而不是抛出异常或用默认值冒充通达信证据。
def test_load_tdx_enrichment_returns_none_when_snapshot_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert load_tdx_enrichment("2026-10-09", Path(tmp)) is None


def test_load_tdx_enrichment_parses_snapshot_and_skips_invalid_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "2026-10-09.json"
        path.write_text(
            json.dumps(
                {
                    "provider": "tdx-connector",
                    "code_lookup": {"捷成股份": {"code": "300182"}, "坏数据": {"nope": 1}},
                    "board_quotes": [
                        {"code": "880710", "name": "种业", "kind": "concept", "change_percent": 6.89},
                        {"code": "", "name": "缺代码", "kind": "concept"},
                        {"code": "999999", "name": "非法类型", "kind": "other"},
                    ],
                    "stock_themes": {"600825": {"theme": "出版业+资产重组"}},
                    "news": [{"title": "焦点复盘", "summary": "69 股涨停。"}, {"title": ""}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loaded = load_tdx_enrichment("2026-10-09", Path(tmp))
        assert loaded is not None
        assert loaded.code_lookup == {"捷成股份": {"code": "300182"}}
        assert [item.name for item in loaded.board_quotes] == ["种业"]
        assert loaded.stock_themes["600825"].theme == "出版业+资产重组"
        assert len(loaded.news) == 1


def test_load_tdx_enrichment_returns_none_on_malformed_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "2026-10-09.json").write_text("{not json", encoding="utf-8")
        assert load_tdx_enrichment("2026-10-09", Path(tmp)) is None


# 自校验：合法快照必须 ok，且逐字段报出覆盖条数，供无人值守采集后自检。
def test_inspect_snapshot_reports_counts_for_valid_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "2026-10-09.json").write_text(
            json.dumps(
                {
                    "provider": "tdx-connector",
                    "generated_at": "2026-10-09T22:38:00+08:00",
                    "code_lookup": {"捷成股份": {"code": "300182"}},
                    "board_quotes": [{"code": "880710", "name": "种业", "kind": "concept"}],
                    "stock_themes": {"600825": {"theme": "出版业+资产重组"}},
                    "news": [{"title": "焦点复盘"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        report = inspect_tdx_snapshot("2026-10-09", Path(tmp))
        assert report["ok"] is True
        assert report["counts"] == {"code_lookup": 1, "board_quotes": 1, "stock_themes": 1, "news": 1}
        assert report["dropped"] == []
        assert report["warnings"] == []


# 有非法条目被解析器跳过时，自校验必须 ok=False 并写明跳过了几条，绝不静默。
def test_inspect_snapshot_flags_dropped_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "2026-10-09.json").write_text(
            json.dumps(
                {
                    "board_quotes": [
                        {"code": "880710", "name": "种业", "kind": "concept"},
                        {"code": "", "name": "缺代码", "kind": "concept"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        report = inspect_tdx_snapshot("2026-10-09", Path(tmp))
        assert report["ok"] is False
        assert report["counts"]["board_quotes"] == 1
        assert any("board_quotes" in item for item in report["dropped"])


# 空字段属于覆盖度提示：ok 仍为真，但每条空字段都要出现在 warnings 里。
def test_inspect_snapshot_warns_on_empty_sections() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "2026-10-09.json").write_text(
            json.dumps({"news": [{"title": "焦点复盘"}]}, ensure_ascii=False), encoding="utf-8"
        )
        report = inspect_tdx_snapshot("2026-10-09", Path(tmp))
        assert report["ok"] is True
        assert len(report["warnings"]) == 3


def test_inspect_snapshot_handles_missing_and_malformed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = inspect_tdx_snapshot("2026-10-09", Path(tmp))
        assert missing["exists"] is False and missing["ok"] is False
        (Path(tmp) / "2026-10-09.json").write_text("{not json", encoding="utf-8")
        malformed = inspect_tdx_snapshot("2026-10-09", Path(tmp))
        assert malformed["exists"] is True and malformed["ok"] is False
        assert any("无法解析" in item for item in malformed["warnings"])


# 板块领涨股未命中涨停池时，代码必须由通达信解析，而不是停留在待确认。
def test_candidates_resolve_leading_stock_code_via_tdx_lookup() -> None:
    sectors = (
        {
            "name": "影视院线",
            "source_type": "industry_ths",
            "leading_stock": "捷成股份",
            "leading_stock_change_percent": 10.98,
        },
        {
            "name": "贵金属",
            "source_type": "industry_ths",
            "leading_stock": "未知股",
            "leading_stock_change_percent": 9.98,
        },
    )
    candidates = build_candidates((), (), sectors, _tdx())
    codes = {row["name"]: row["code"] for row in candidates["sector_leaders"]}
    assert codes["捷成股份"] == "300182"
    assert codes["未知股"] == "待确认"


# 通达信概念板块必须在板块主线的证据与候选区同时出现，并带上涨停逻辑。
def test_tdx_boards_and_themes_reach_leadership_and_candidates() -> None:
    leadership = read_sector_leadership((), (), _tdx())
    assert [item.name for item in leadership["tdx_concepts"]] == ["种业"]
    assert any("通达信概念板块" in item for item in leadership["evidence"])

    ladder_stock = {"code": "600825", "name": "新华传媒", "board_count": 9}
    candidates = build_candidates((ladder_stock,), (), (), _tdx())
    assert candidates["themes"][0]["theme"] == "出版业+资产重组"


# 缺通达信快照时，渲染必须显式写出消息面缺口，而不是静默省略。
def test_render_marks_missing_tdx_sections() -> None:
    inputs = DecisionInputs(
        trade_date="2026-10-09",
        indices={},
        total_amount=None,
        total_amount_source=None,
        breadth=None,
        sentiment_today=None,
        sentiment_history=(),
        amount_history=(),
        index_history={},
        sectors=(),
        limit_up_stocks=(),
        broken_board_stocks=(),
        sources={},
        gaps=(),
    )
    markdown = render_decision_card(inputs, read_decision(inputs), generated_at="2026-10-09T22:00:00+08:00")
    assert "消息面（通达信）" in markdown
    assert "本次未接入通达信资讯" in markdown


# 接入通达信快照时，渲染必须出现概念板块表、涨停逻辑与消息面三块新证据。
def test_render_includes_tdx_evidence_when_snapshot_present() -> None:
    inputs = DecisionInputs(
        trade_date="2026-10-09",
        indices={},
        total_amount=None,
        total_amount_source=None,
        breadth=None,
        sentiment_today=None,
        sentiment_history=(),
        amount_history=(),
        index_history={},
        sectors=(),
        limit_up_stocks=({"code": "600825", "name": "新华传媒", "board_count": 9},),
        broken_board_stocks=(),
        sources={},
        gaps=(),
        tdx=_tdx(),
    )
    markdown = render_decision_card(inputs, read_decision(inputs), generated_at="2026-10-09T22:00:00+08:00")
    assert "通达信板块行情" in markdown
    assert "涨停逻辑（通达信）" in markdown
    assert "焦点复盘" in markdown
