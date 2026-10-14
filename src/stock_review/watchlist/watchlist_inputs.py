# 本文件定义核心异动票跟踪的事实模型与板块/基准指数映射。
# 它只承载可追溯事实字段，不包含任何买卖判定或形态成立结论。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# 核心跟踪均线：对应 STEP 9 模式 9「前期核心 21 日线低吸」的观察位置。
CORE_MA_WINDOW = 21
SHORT_MA_WINDOW = 5
MID_MA_WINDOW = 10

# 板块归属：决定基准指数（偏离值配对）与「异常波动」3 日偏离阈值。
BOARD_SHANGHAI_MAIN = "沪市主板"
BOARD_STAR = "科创板"
BOARD_SHENZHEN_MAIN = "深市主板"
BOARD_CHINEXT = "创业板"
BOARD_BSE = "北交所"
BOARD_UNKNOWN = "待确认"

# 基准指数映射：偏离值 = 个股日涨跌幅 − 对应基准指数同日涨跌幅。
BENCHMARK_BY_BOARD = {
    BOARD_SHANGHAI_MAIN: "sh000001",
    BOARD_STAR: "sh000688",
    BOARD_SHENZHEN_MAIN: "sz399001",
    BOARD_CHINEXT: "sz399006",
    BOARD_BSE: "bj899050",
    BOARD_UNKNOWN: "sh000001",
}

BENCHMARK_NAMES = {
    "sh000001": "上证指数",
    "sh000688": "科创50",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "bj899050": "北证50",
}

# 交易所「异常波动」认定阈值：连续三个交易日收盘价涨跌幅偏离值累计，按板块区分。
ABNORMAL_DEVIATION_3D = {
    BOARD_SHANGHAI_MAIN: 20.0,
    BOARD_SHENZHEN_MAIN: 20.0,
    BOARD_CHINEXT: 30.0,
    BOARD_STAR: 30.0,
    BOARD_BSE: 30.0,
    BOARD_UNKNOWN: 20.0,
}

# 交易所「严重异常波动」认定阈值：连续 10 日 / 30 日收盘价涨跌幅偏离值累计。
SEVERE_DEVIATION_10D = 100.0
SEVERE_DEVIATION_30D = 200.0


@dataclass(frozen=True)
class WatchlistItem:
    """池内一只跟踪对象；板块与基准指数由代码推导，不手工填写以免配对错误。"""

    code: str
    name: str
    board: str
    benchmark: str
    added_date: str
    reason: str
    status: str = "active"


@dataclass(frozen=True)
class DailyBar:
    """单根日线。价格按前复权口径；volume 单位随来源，只用于相对比较。"""

    trade_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None = None
    turnover: float | None = None


@dataclass(frozen=True)
class WatchlistInputs:
    """一次跟踪运行的全部输入事实；缺哪个留空并由 gaps 如实说明，不用推算值填充。"""

    trade_date: str
    items: tuple[WatchlistItem, ...]
    daily_bars: dict[str, tuple[DailyBar, ...]]
    index_bars: dict[str, tuple[DailyBar, ...]]
    longhubang: dict[str, dict[str, Any]]
    fund_flow: dict[str, dict[str, Any]]
    sources: dict[str, str]
    gaps: tuple[str, ...] = ()


def board_for_code(code: str) -> str:
    """按证券代码前缀判定板块；无法判定时返回待确认，不猜测交易所。"""
    text = str(code or "").strip()
    if not text.isdigit() or len(text) != 6:
        return BOARD_UNKNOWN
    if text.startswith("688"):
        return BOARD_STAR
    if text.startswith("689"):
        return BOARD_STAR
    if text.startswith("6"):
        return BOARD_SHANGHAI_MAIN
    if text.startswith("30"):
        return BOARD_CHINEXT
    if text.startswith("0"):
        return BOARD_SHENZHEN_MAIN
    if text.startswith(("4", "8", "9")):
        return BOARD_BSE
    return BOARD_UNKNOWN


def benchmark_for_board(board: str) -> str:
    return BENCHMARK_BY_BOARD.get(board, BENCHMARK_BY_BOARD[BOARD_UNKNOWN])


def build_item(code: str, name: str, added_date: str, reason: str, status: str = "active") -> WatchlistItem:
    """由代码推导板块与基准指数，保证池记录与偏离值配对一致。"""
    board = board_for_code(code)
    return WatchlistItem(
        code=str(code).strip(),
        name=str(name).strip(),
        board=board,
        benchmark=benchmark_for_board(board),
        added_date=added_date,
        reason=reason,
        status=status,
    )
