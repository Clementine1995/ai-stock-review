# 本文件验证决策卡的规则判定与渲染边界，全部使用合成输入，不访问网络、真实数据库或正式报告目录。

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from stock_review.decision.collect_decision_inputs import sector_source_label
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
    read_divergence_quality,
    read_market_stage,
    read_participation,
    read_position_band,
    read_sector_leadership,
    read_sentiment_stage,
)
from stock_review.decision.render_decision_card import render_decision_card
from stock_review.decision.run_decision_card import (
    inputs_from_snapshot,
    run_decision_card_from_snapshot,
)
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


# 退潮期必须统一映射为不参与：不得再出现「谨慎参与 + 仓位 0%」这类自相矛盾组合。
def test_participation_unifies_fade_to_no_participation() -> None:
    assert read_participation({"label": "下降期"}, {"label": "退潮期"})["label"] == "不参与"
    assert read_participation({"label": "主升期"}, {"label": "退潮期"})["label"] == "不参与"


def test_decision_keeps_position_zero_on_fade() -> None:
    today = _sentiment("2026-01-10", limit_up=40, limit_down=6, broken=10, highest_board=4)
    previous = _sentiment("2026-01-09", limit_up=80, limit_down=5, broken=10, highest_board=7)
    inputs = DecisionInputs(
        trade_date="2026-01-10",
        indices={},
        total_amount=1_900_000_000_000.0,
        total_amount_source="test",
        breadth=None,
        sentiment_today=today,
        sentiment_history=(previous, today),
        amount_history=(),
        index_history={
            "sh000001": _index_days([110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 104.0, 103.0, 102.0, 100.0])
        },
        sectors=(),
        limit_up_stocks=(),
        broken_board_stocks=(),
        sources={},
        gaps=(),
    )
    reading = read_decision(inputs)
    assert reading.sentiment_stage["label"] == "退潮期"
    assert reading.participation["label"] == "不参与"
    assert reading.position_band["total_range"] == (0, 0)


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
    assert "涨停逻辑（通达信，覆盖连板梯队 1/1 只）" in markdown
    assert "焦点复盘" in markdown


# 候选区必须标注总数与截断关系，避免把「展示前 10 条」误读为全量；首板候选必须渲染。
def test_render_annotates_candidate_coverage() -> None:
    ladder = tuple(
        {
            "code": f"6000{index:02d}",
            "name": f"连板{index}",
            "board_count": 2,
            "sector": "电池",
            "change_percent": 10.0,
            "seal_amount": 1e8,
        }
        for index in range(12)
    )
    first_boards = tuple(
        {
            "code": f"3000{index:02d}",
            "name": f"首板{index}",
            "board_count": 1,
            "sector": "软件",
            "change_percent": 20.0,
            "seal_amount": 2e8 - index,
        }
        for index in range(15)
    )
    broken = tuple({"code": f"0000{index:02d}", "name": f"炸板{index}"} for index in range(14))
    tdx = TdxEnrichment(
        provider="tdx-connector",
        generated_at=None,
        code_lookup={},
        board_quotes=(),
        stock_themes={
            "600000": TdxTheme(code="600000", theme="测试主题"),
            "600001": TdxTheme(code="600001", theme="测试主题二"),
        },
        news=(),
    )
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
        limit_up_stocks=(*ladder, *first_boards),
        broken_board_stocks=broken,
        sources={},
        gaps=(),
        tdx=tdx,
    )
    markdown = render_decision_card(inputs, read_decision(inputs), generated_at="2026-10-09T22:00:00+08:00")
    assert "共 12 只，展示前 10 只" in markdown
    assert "连板10" not in markdown and "连板11" not in markdown
    assert "首板强势" in markdown
    assert "共 15 只，展示前 10 只" in markdown
    assert "共 14 只，展示前 10 只" in markdown
    assert "覆盖连板梯队 2/12 只" in markdown


# 快照存在但对连板梯队零覆盖时必须写明覆盖率，禁止静默省略「涨停逻辑」一节。
def test_render_marks_theme_coverage_zero() -> None:
    tdx = TdxEnrichment(
        provider="tdx-connector",
        generated_at=None,
        code_lookup={},
        board_quotes=(),
        stock_themes={"999999": TdxTheme(code="999999", theme="非连板主题")},
        news=(),
    )
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
        tdx=tdx,
    )
    markdown = render_decision_card(inputs, read_decision(inputs), generated_at="2026-10-09T22:00:00+08:00")
    assert "覆盖连板梯队 0/1 只" in markdown
    assert "未包含连板股的涨停逻辑" in markdown


# 单票上限必须注明分母（总资金口径），消除与总仓位区间的口径歧义。
def test_render_position_cap_states_denominator() -> None:
    today = _sentiment("2026-01-10", limit_up=60, limit_down=5, broken=10, highest_board=5)
    inputs = DecisionInputs(
        trade_date="2026-01-10",
        indices={},
        total_amount=1_900_000_000_000.0,
        total_amount_source="test",
        breadth=None,
        sentiment_today=today,
        sentiment_history=(today,),
        amount_history=(),
        index_history={
            "sh000001": _index_days([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 110.0])
        },
        sectors=(),
        limit_up_stocks=(),
        broken_board_stocks=(),
        sources={},
        gaps=(),
    )
    reading = read_decision(inputs)
    assert reading.participation["label"] == "可参与"
    markdown = render_decision_card(inputs, reading, generated_at="2026-01-10T22:00:00+08:00")
    assert "总仓位 30%–50%" in markdown
    assert "单票 ≤ 总资金的 30%" in markdown


# 板块来源必须按实际返回口径标注：同花顺退化口径不得写成东财接口名。
def test_sector_source_label_matches_actual_channel() -> None:
    assert (
        sector_source_label([{"source_type": "industry_ths"}])
        == "akshare:stock_board_industry_summary_ths（同花顺口径）"
    )
    assert (
        sector_source_label([{"source_type": "concept"}, {"source_type": "industry"}])
        == "akshare:stock_board_concept_name_em+stock_board_industry_name_em"
    )
    assert sector_source_label([]) == "akshare:板块接口"


# 分歧质量（结构）：连板高度环比骤降必须标记结构转弱，补上情绪阶段只看当日横截面的盲点。
def test_divergence_quality_flags_board_collapse() -> None:
    previous = _sentiment("2026-10-12", limit_up=46, limit_down=19, broken=25, highest_board=10)
    today = _sentiment("2026-10-13", limit_up=67, limit_down=8, broken=19, highest_board=3)
    reading = read_divergence_quality(today, (previous, today))
    assert reading["label"] == "结构转弱"
    assert any("高标断板" in item for item in reading["flags"])


# 分歧质量（结构）：炸板率环比明显抬升同样标记结构转弱。
def test_divergence_quality_flags_broken_rate_rise() -> None:
    previous = _sentiment("2026-10-09", limit_up=69, limit_down=8, broken=14, highest_board=9)
    today = _sentiment("2026-10-12", limit_up=46, limit_down=19, broken=25, highest_board=10)
    reading = read_divergence_quality(today, (previous, today))
    assert reading["label"] == "结构转弱"
    assert any("封板质量转差" in item for item in reading["flags"])


def test_divergence_quality_is_healthy_when_structure_stable() -> None:
    previous = _sentiment("2026-01-09", limit_up=60, limit_down=5, broken=10, highest_board=5)
    today = _sentiment("2026-01-10", limit_up=64, limit_down=4, broken=11, highest_board=6)
    reading = read_divergence_quality(today, (previous, today))
    assert reading["label"] == "结构健康"
    assert reading["flags"] == []
    assert any("最高连板" in item for item in reading["evidence"])


def test_divergence_quality_is_undetermined_without_previous() -> None:
    today = _sentiment("2026-01-10", limit_up=64, limit_down=4, broken=11, highest_board=6)
    assert read_divergence_quality(today, (today,))["label"] == "不可判定"
    assert read_divergence_quality(None, ())["label"] == "不可判定"


# 结构转弱必须同时出现在四问表的情绪结论与情绪面板，让读者在四问层就看到高度警示。
def test_render_surfaces_divergence_warning() -> None:
    previous = _sentiment("2026-10-12", limit_up=46, limit_down=19, broken=25, highest_board=10)
    today = _sentiment("2026-10-13", limit_up=67, limit_down=8, broken=19, highest_board=3)
    inputs = DecisionInputs(
        trade_date="2026-10-13",
        indices={},
        total_amount=1_600_000_000_000.0,
        total_amount_source="test",
        breadth={"up_count": 2790, "down_count": 2267},
        sentiment_today=today,
        sentiment_history=(previous, today),
        amount_history=(),
        index_history={},
        sectors=(),
        limit_up_stocks=(),
        broken_board_stocks=(),
        sources={},
        gaps=(),
    )
    reading = read_decision(inputs)
    assert reading.sentiment_stage["label"] == "上升期"
    assert reading.divergence_quality["label"] == "结构转弱"
    markdown = render_decision_card(inputs, reading, generated_at="2026-10-13T20:00:00+08:00")
    assert "结构转弱" in markdown
    assert "分歧质量（结构）" in markdown
    assert "高标断板" in markdown


# 2026-10-13 真实路径：阴跌至 10 日箱体下沿，当日低开高走、盘中创新低后收回。
_INDEX_ROWS_2026_10_13 = (
    ("2026-09-22", 3963.81, 3967.68, 3945.42, 3952.13),
    ("2026-09-23", 3951.37, 3951.52, 3934.46, 3936.52),
    ("2026-09-24", 3925.32, 3930.50, 3888.37, 3888.37),
    ("2026-09-28", 3878.41, 3878.41, 3806.67, 3823.62),
    ("2026-09-29", 3816.15, 3843.84, 3810.81, 3830.45),
    ("2026-09-30", 3839.25, 3851.22, 3833.09, 3842.19),
    ("2026-10-08", 3838.98, 3864.12, 3795.37, 3811.90),
    ("2026-10-09", 3804.09, 3824.71, 3754.14, 3813.79),
    ("2026-10-12", 3806.98, 3828.22, 3791.00, 3797.24),
    ("2026-10-13", 3760.80, 3822.11, 3741.53, 3800.86),
)


def _index_history(rows: tuple[tuple[str, float, float, float, float], ...]) -> dict[str, tuple[IndexDay, ...]]:
    return {
        "sh000001": tuple(
            IndexDay(trade_date=date, close=close, open=open_, high=high, low=low)
            for date, open_, high, low, close in rows
        )
    }


# 止跌迹象：下降期内的低位低开高走必须被标记，且不得改变指数阶段结论。
def test_stabilization_flags_low_open_high_close_at_box_bottom() -> None:
    stage = read_market_stage(_index_history(_INDEX_ROWS_2026_10_13))
    assert stage["label"] == "下降期"
    assert stage["detail"]["close_strength"] == 0.736
    assert stage["detail"]["strong_close"] is True
    assert stage["detail"]["box_low"] == 3797.24
    assert any("收盘强度" in item for item in stage["evidence"])
    assert not any("下影" in item for item in stage["evidence"])

    stabilization = stage["stabilization"]
    assert stabilization["label"] == "止跌迹象"
    assert any("低开高走" in item for item in stabilization["flags"])
    assert any("新低" in item for item in stabilization["flags"])


def test_stabilization_absent_when_close_below_open() -> None:
    rows = (*_INDEX_ROWS_2026_10_13[:-1], ("2026-10-13", 3806.98, 3828.22, 3791.00, 3797.24))
    stabilization = read_market_stage(_index_history(rows))["stabilization"]
    assert stabilization["label"] == "无止跌迹象"
    assert stabilization["flags"] == []
    assert any("收盘未高于开盘" in item for item in stabilization["evidence"])


# 开/高/低缺失时不得臆造形态：必须明确不可判定，而不是按 0 或缺省值计算。
def test_stabilization_is_undetermined_without_intraday_fields() -> None:
    closes = [110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 104.0, 103.0, 102.0, 100.0]
    assert read_market_stage({"sh000001": _index_days(closes)})["stabilization"]["label"] == "不可判定"
    assert read_market_stage({"sh000001": _index_days([100.0, 101.0])})["stabilization"]["label"] == "不可判定"


def test_stabilization_not_applicable_in_uptrend() -> None:
    rows = tuple(
        (
            f"2026-01-{index + 1:02d}",
            100.0 + index - 0.5,
            100.0 + index + 0.5,
            100.0 + index - 1.0,
            100.0 + index,
        )
        for index in range(10)
    )
    stage = read_market_stage(_index_history(rows))
    assert stage["label"] == "主升期"
    stabilization = stage["stabilization"]
    assert stabilization["label"] == "无止跌迹象"
    assert any("不适用止跌判断" in item for item in stabilization["evidence"])


# 触发时必须在第三节的指数阶段旁与止跌块同时出现；同时不得再出现「下影占比」这一错误措辞。
def test_render_surfaces_stabilization_marker() -> None:
    previous = _sentiment("2026-10-12", limit_up=46, limit_down=19, broken=25, highest_board=10)
    today = _sentiment("2026-10-13", limit_up=67, limit_down=8, broken=19, highest_board=3)
    inputs = DecisionInputs(
        trade_date="2026-10-13",
        indices={"sh000001": {"name": "上证指数", "close": 3800.86, "change_percent": 0.1}},
        total_amount=1_677_830_000_000.0,
        total_amount_source="test",
        breadth={"up_count": 2790, "down_count": 2267},
        sentiment_today=today,
        sentiment_history=(previous, today),
        amount_history=(),
        index_history=_index_history(_INDEX_ROWS_2026_10_13),
        sectors=(),
        limit_up_stocks=(),
        broken_board_stocks=(),
        sources={},
        gaps=(),
    )
    reading = read_decision(inputs)
    assert reading.market_stage["label"] == "下降期"
    markdown = render_decision_card(inputs, reading, generated_at="2026-10-13T20:00:00+08:00")
    assert "下降期｜止跌迹象" in markdown
    assert "止跌迹象（指数）：**止跌迹象**" in markdown
    assert "低开高走" in markdown
    assert "下影占比" not in markdown


# 失效条件必须与阶段方向一致：已在下降期时不得再写「跌破 MA10 → 按下降期处理」这类已成立的空条款。
def test_render_invalidation_follows_stage_direction() -> None:
    previous = _sentiment("2026-10-12", limit_up=46, limit_down=19, broken=25, highest_board=10)
    today = _sentiment("2026-10-13", limit_up=67, limit_down=8, broken=19, highest_board=3)
    downtrend = DecisionInputs(
        trade_date="2026-10-13",
        indices={},
        total_amount=1_677_830_000_000.0,
        total_amount_source="test",
        breadth=None,
        sentiment_today=today,
        sentiment_history=(previous, today),
        amount_history=(),
        index_history=_index_history(_INDEX_ROWS_2026_10_13),
        sectors=(),
        limit_up_stocks=(),
        broken_board_stocks=(),
        sources={},
        gaps=(),
    )
    downtrend_markdown = render_decision_card(
        downtrend, read_decision(downtrend), generated_at="2026-10-13T20:00:00+08:00"
    )
    assert "10 日箱底（3797.24）" in downtrend_markdown
    assert "按下降期处理" not in downtrend_markdown

    uptrend_today = _sentiment("2026-01-10", limit_up=60, limit_down=5, broken=10, highest_board=5)
    uptrend = DecisionInputs(
        trade_date="2026-01-10",
        indices={},
        total_amount=1_900_000_000_000.0,
        total_amount_source="test",
        breadth=None,
        sentiment_today=uptrend_today,
        sentiment_history=(uptrend_today,),
        amount_history=(),
        index_history={
            "sh000001": _index_days([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 110.0])
        },
        sectors=(),
        limit_up_stocks=(),
        broken_board_stocks=(),
        sources={},
        gaps=(),
    )
    uptrend_markdown = render_decision_card(
        uptrend, read_decision(uptrend), generated_at="2026-01-10T20:00:00+08:00"
    )
    assert "跌破 MA10（104.60）且量能萎缩 → 按下降期处理" in uptrend_markdown


# 重渲染用的最小快照：结构与 run_decision_card._serialize 写出的 JSON 一致，不依赖真实归档文件。
def _snapshot_payload() -> dict:
    return {
        "inputs": {
            "trade_date": "2026-10-13",
            "indices": {"sh000001": {"name": "上证指数", "close": 3800.86, "change_percent": 0.1}},
            "total_amount": 1_677_830_618_112.38,
            "total_amount_source": "akshare:stock_sse_deal_daily+akshare:stock_szse_summary",
            "breadth": {"up_count": 2790, "down_count": 2267, "activity_percent": "53.40"},
            "sentiment_today": {
                "trade_date": "2026-10-13",
                "limit_up_count": 67,
                "limit_down_count": 8,
                "broken_board_count": 19,
                "broken_board_rate": 0.2209,
                "highest_board": 3,
            },
            "sentiment_history": [
                {
                    "trade_date": "2026-10-12",
                    "limit_up_count": 46,
                    "limit_down_count": 19,
                    "broken_board_count": 25,
                    "broken_board_rate": 0.3521,
                    "highest_board": 10,
                },
                {
                    "trade_date": "2026-10-13",
                    "limit_up_count": 67,
                    "limit_down_count": 8,
                    "broken_board_count": 19,
                    "broken_board_rate": 0.2209,
                    "highest_board": 3,
                },
            ],
            "amount_history": [
                ["2026-10-12", 1_539_169_791_852.2],
                ["2026-10-13", 1_677_830_618_112.38],
            ],
            "index_history": {
                "sh000001": [
                    {"trade_date": day, "open": open_, "high": high, "low": low, "close": close, "amount": None}
                    for day, open_, high, low, close in _INDEX_ROWS_2026_10_13
                ]
            },
            "sectors": [],
            "limit_up_stocks": [],
            "broken_board_stocks": [],
            "sources": {"index": "akshare:stock_zh_index_daily_em"},
            "gaps": [],
            "tdx": None,
        },
        "reading": {},
        "generated_at": "2026-10-13T20:52:36+08:00",
    }


# 快照反序列化必须还原全部输入事实；缺失的数值不得用 0 冒充，K 线的开/高/低要逐日保留。
def test_inputs_from_snapshot_restores_frozen_facts() -> None:
    inputs = inputs_from_snapshot(_snapshot_payload())
    assert inputs.trade_date == "2026-10-13"
    assert inputs.total_amount == 1_677_830_618_112.38
    assert [day.trade_date for day in inputs.sentiment_history] == ["2026-10-12", "2026-10-13"]
    assert inputs.sentiment_today.highest_board == 3
    assert inputs.amount_history[-1] == ("2026-10-13", 1_677_830_618_112.38)
    assert len(inputs.index_history["sh000001"]) == 10
    latest = inputs.index_history["sh000001"][-1]
    assert (latest.open, latest.high, latest.low, latest.close) == (3760.80, 3822.11, 3741.53, 3800.86)
    assert inputs.tdx is None
    # 历史快照把活跃度存成了字符串，读回时必须还原为数值，否则渲染会按字符串处理而失真。
    assert inputs.breadth["activity_percent"] == 53.4


# 家数等必需字段缺失时该日视为不可用，不用 0 替代；快照缺 inputs 节点必须显式报错。
def test_inputs_from_snapshot_refuses_defaults_and_missing_node() -> None:
    broken = _snapshot_payload()
    del broken["inputs"]["sentiment_today"]["limit_up_count"]
    assert inputs_from_snapshot(broken).sentiment_today is None

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "2026-10-13.json"
        path.write_text(json.dumps({"reading": {}}, ensure_ascii=False), encoding="utf-8")
        for call in (
            lambda: inputs_from_snapshot({"reading": {}}),
            lambda: run_decision_card_from_snapshot(path, write=False),
        ):
            try:
                call()
            except ValueError as error:
                assert "inputs" in str(error)
            else:  # pragma: no cover - 走到这里说明未按预期报错
                raise AssertionError("缺少 inputs 节点时必须报错")


# 从快照重渲染必须：读回已冻结事实、产出新标记、沿用快照生成时间，且不触碰采集通道。
def test_run_decision_card_from_snapshot_rerenders_without_collecting() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "source.json"
        snapshot.write_text(json.dumps(_snapshot_payload(), ensure_ascii=False), encoding="utf-8")
        result = run_decision_card_from_snapshot(
            snapshot,
            report_dir=Path(tmp) / "reports",
            data_dir=Path(tmp) / "data",
        )
        assert result["trade_date"] == "2026-10-13"
        # 沿用快照记录的生成时间，保证与归档稿的差异只来自规则层。
        assert result["generated_at"] == "2026-10-13T20:52:36+08:00"
        assert result["answers"]["market_stage"] == "下降期"
        assert result["answers"]["stabilization"] == "止跌迹象"
        assert result["answers"]["divergence_quality"] == "结构转弱"
        markdown = result["markdown"]
        assert "下降期｜止跌迹象" in markdown
        assert "结构转弱" in markdown
        assert "市场活跃度 | 53.40%" in markdown
        # 落盘路径按交易日命名，内容可回读。
        written = Path(result["report_path"])
        assert written.is_file() and written.read_text(encoding="utf-8") == markdown
        assert Path(result["data_path"]).is_file()
