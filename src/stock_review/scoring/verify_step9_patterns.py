# 本文件负责按当前 stock-review.md 的 STEP 9 模式清单核验已有证据与硬缺口，不输出买卖建议。

from __future__ import annotations

from dataclasses import dataclass

from stock_review.evidence.evidence_snapshot import EvidenceSnapshot


@dataclass(frozen=True)
class Step9PatternDefinition:
    pattern_number: int
    name: str
    required_evidence: tuple[str, ...]
    invalidation_boundary: str


@dataclass(frozen=True)
class Step9PatternCheck:
    pattern_number: int
    name: str
    status: str
    available_evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    invalidation_boundary: str


# 当前 12 项只对应现行 STEP 9 文本；框架语义变化后必须随 M10.6 一并复核。
STEP9_PATTERN_DEFINITIONS = (
    Step9PatternDefinition(1, "题材上升初期和发酵阶段的弹性标", ("sectors", "stocks", "daily_kline", "call_auction", "intraday"), "题材未延续或个股缺少弹性事实时失效。"),
    Step9PatternDefinition(2, "退潮末期弱转强或大分歧后超预期高开", ("sentiment", "stocks", "previous_day_feedback", "call_auction", "intraday"), "未出现弱转强或高开反馈时失效。"),
    Step9PatternDefinition(3, "轮动期间主线长上影线", ("sectors", "daily_kline", "intraday"), "上影线不大于下影线或板块不属于轮动主线时失效。"),
    Step9PatternDefinition(4, "核心盘中被动炸板回封", ("stocks", "sectors", "intraday", "previous_day_feedback"), "无法证明被动炸板、回封或板块联动时失效。"),
    Step9PatternDefinition(5, "大单一字板块中的近期图形较好 20cm", ("sectors", "stocks", "daily_kline", "call_auction", "intraday"), "缺少一字板块、20cm 标识或近期图形事实时失效。"),
    Step9PatternDefinition(6, "情绪低迷时十点前抗跌的容量核心", ("sentiment", "stocks", "intraday"), "情绪不低迷或容量对象无法证明盘中抗跌时失效。"),
    Step9PatternDefinition(7, "容量票启动后炸板回封及长上影尾盘", ("stocks", "daily_kline", "intraday", "next_day_feedback"), "无法证明启动、回封、非跌停深水或次日反馈时失效。"),
    Step9PatternDefinition(8, "指数午盘前逼空后的核心或后排共振", ("market", "sectors", "stocks", "intraday"), "指数未逼空、板块未共振或对象非核心事实时失效。"),
    Step9PatternDefinition(9, "前期核心 21 日线低吸", ("stocks", "daily_kline", "regulatory_event"), "缺少前期核心、21 日线或异动监管事实时失效。"),
    Step9PatternDefinition(10, "确定核心后的开盘下杀承接", ("stocks", "call_auction", "intraday"), "无法证明核心身份、开盘下杀或承接时失效。"),
    Step9PatternDefinition(11, "大涨次日前排切换", ("sectors", "stocks", "call_auction", "intraday"), "板块开盘强度或前后排分化事实不满足时失效。"),
    Step9PatternDefinition(12, "情绪指数双低迷尾盘炸板", ("market", "sentiment", "stocks", "intraday"), "未同时满足情绪和指数低迷、尾盘炸板或 3-4 板事实时失效。"),
)


# 快照只提供部分盘后事实；竞价、分时、前日反馈和日线形态未接入时必须逐项保留为硬缺口。
def verify_step9_patterns(snapshot: EvidenceSnapshot) -> tuple[Step9PatternCheck, ...]:
    available_evidence = collect_available_evidence(snapshot)
    return tuple(
        build_pattern_check(definition, available_evidence)
        for definition in STEP9_PATTERN_DEFINITIONS
    )


def collect_available_evidence(snapshot: EvidenceSnapshot) -> set[str]:
    available: set[str] = set()
    if snapshot.market.get("indices"):
        available.add("market")
    if snapshot.sentiment:
        available.add("sentiment")
    if snapshot.sectors:
        available.add("sectors")
    if snapshot.stocks:
        available.add("stocks")
    return available


def build_pattern_check(definition: Step9PatternDefinition, available_evidence: set[str]) -> Step9PatternCheck:
    available = tuple(field_name for field_name in definition.required_evidence if field_name in available_evidence)
    missing = tuple(field_name for field_name in definition.required_evidence if field_name not in available_evidence)
    return Step9PatternCheck(
        pattern_number=definition.pattern_number,
        name=definition.name,
        status="pending_confirmation" if not missing else "unavailable",
        available_evidence=available,
        missing_evidence=missing,
        invalidation_boundary=definition.invalidation_boundary,
    )
