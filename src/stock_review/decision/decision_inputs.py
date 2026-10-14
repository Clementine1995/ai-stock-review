# 本文件定义决策卡的输入值对象与输出结论结构。
# 输入只承载已经采集到的事实、来源和缺口；结论只承载规则判定结果，两者都不做采集或渲染。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SentimentDay:
    # 单个交易日的短线情绪横截面事实；缺失字段保留 None，不用 0 代替。
    trade_date: str
    limit_up_count: int
    limit_down_count: int
    broken_board_count: int
    broken_board_rate: float | None
    highest_board: int | None


@dataclass(frozen=True)
class IndexDay:
    # 指数单日 K 线事实；amount 与 up_count/down_count 在来源不提供时保留 None。
    trade_date: str
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    amount: float | None = None


@dataclass(frozen=True)
class TdxBoardQuote:
    # 通达信板块（概念/行业）当日行情；kind 取值 concept 或 industry，缺失字段保留 None。
    code: str
    name: str
    kind: str
    change_percent: float | None = None
    up_count: int | None = None
    down_count: int | None = None
    amount: float | None = None


@dataclass(frozen=True)
class TdxTheme:
    # 通达信个股涨停逻辑；theme 是涨停主题，reason 是当日原因揭秘。
    code: str
    theme: str
    reason: str | None = None
    first_limit_time: str | None = None
    seal_amount: float | None = None


@dataclass(frozen=True)
class TdxNews:
    # 通达信资讯摘要；只保留标题、来源、时间与摘要，不外链正文。
    title: str
    source: str | None = None
    published_at: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class TdxEnrichment:
    # 通达信连接器补充证据；由 Agent 采集后落成快照文件，再在决策卡运行时读入。
    # 采集通道（MCP）与 Python 运行通道相互独立，因此本对象只承载已落盘的事实。
    provider: str
    generated_at: str | None
    code_lookup: dict[str, dict[str, Any]]
    board_quotes: tuple[TdxBoardQuote, ...]
    stock_themes: dict[str, TdxTheme]
    news: tuple[TdxNews, ...]


@dataclass(frozen=True)
class DecisionInputs:
    # 一次决策卡运行的全部输入事实；gaps 记录采集失败或字段缺失，渲染时必须原样展示。
    trade_date: str
    indices: dict[str, dict[str, Any]]
    total_amount: float | None
    total_amount_source: str | None
    breadth: dict[str, Any] | None
    sentiment_today: SentimentDay | None
    sentiment_history: tuple[SentimentDay, ...]
    amount_history: tuple[tuple[str, float | None], ...]
    index_history: dict[str, tuple[IndexDay, ...]]
    sectors: tuple[dict[str, Any], ...]
    limit_up_stocks: tuple[dict[str, Any], ...]
    broken_board_stocks: tuple[dict[str, Any], ...]
    sources: dict[str, str]
    gaps: tuple[str, ...]
    tdx: TdxEnrichment | None = None


@dataclass(frozen=True)
class DecisionReading:
    # 规则判定结果；每个结论都带 evidence 与 rule，便于回溯到 stock-review.md 的哪条规则。
    market_stage: dict[str, Any]
    amount_band: dict[str, Any]
    sentiment_stage: dict[str, Any]
    divergence_quality: dict[str, Any]
    participation: dict[str, Any]
    position_band: dict[str, Any]
    sector_leadership: dict[str, Any]
    candidates: dict[str, Any]
