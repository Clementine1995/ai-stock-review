# 本文件负责按 stock-review.md 的 STEP 1-10 编排详细复盘，模型只生成受本地证据约束的解释性草案。

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import logging
import math
from pathlib import Path
import re
from typing import Any, Callable

from stock_review.evidence.summarize_review_facts import (
    HARD_GAP_LABELS,
    ReviewFactsSummary,
    StepEvidence,
    build_step_evidence,
    has_complete_sector_kline_record,
    has_verified_sector_kline,
    is_complete_fact,
    summarize_review_facts,
)
from stock_review.llm.openai_compatible_client import OpenAICompatibleSettings, request_json_completion
from stock_review.review_documents.build_review_context import ReviewContext, build_review_context
from stock_review.review_documents.create_final_draft import build_evidence_facts
from stock_review.review_documents.manage_manual_review import ManualFinalResponse, list_manual_final_responses
from stock_review.review_framework.parse_framework import ReviewFramework, ReviewStep, parse_framework_file
from stock_review.scoring.verify_step9_patterns import Step9PatternCheck, verify_step9_patterns


DEFAULT_DETAILED_REPORT_DIR = Path("reports") / "daily"
DEFAULT_LOG_PATH = Path("logs") / "stock_review.log"
REQUIRED_STEP_NUMBERS = tuple(range(1, 11))
MODEL_STEP_NUMBERS = tuple(range(1, 8))
MAX_PROSE_LENGTH = 800
MAX_FOCUS_CANDIDATES = 3
DETAILED_REQUEST_SCHEMA_VERSION = 1
DETAILED_MODEL_SOURCE_LABELS = {
    "openai_compatible_llm": "项目配置的 OpenAI-compatible LLM（经本地校验）",
    "workbuddy_builtin_llm": "WorkBuddy 内置模型（经本地校验）",
}
# 中文紧邻数字时也必须识别；\w 会把中文视为单词字符，曾漏过“价格75元”一类模型新造数字。
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?%?")
STEP_LABEL_PATTERN = re.compile(r"(?i)STEP\s*(?:\d+|[一二三四五六七八九十]+)")
STOCK_CODE_PATTERN = re.compile(r"(?<!\d)\d{6}(?!\d)")
PROSE_SENTENCE_PATTERN = re.compile(r"[^。！？!?]+[。！？!?]?")
PROSE_CLAUSE_PATTERN = re.compile(r"[^，,；;]+")
SECTOR_SEPARATOR_PATTERN = re.compile(r"[、,，/|；;]+")
NON_EXECUTABLE_POSITION_MARKERS = (
    "不设置可执行仓位",
    "不配置可执行仓位",
    "不设置仓位",
    "不配置仓位",
    "不建仓",
    "保持空仓",
    "仓位待确认",
    "仓位仍需确认",
    "仓位仍需人工确认",
    "仓位由用户确认",
    "仅观察",
)
EXECUTABLE_POSITION_MARKERS = (
    "仓位",
    "建仓",
    "试仓",
    "轻仓",
    "半仓",
    "重仓",
    "满仓",
    "加仓",
    "减仓",
)
CHINESE_POSITION_PATTERN = re.compile(r"[一二三四五六七八九十]+成")
POSITION_CAP_PATTERN = re.compile(
    r"(?:上限|不超过|至多)[^0-9一二三四五六七八九十]{0,8}"
    r"(?:\d+(?:\.\d+)?\s*%|[一二三四五六七八九十]+成)"
)
BOARD_COUNT_PATTERN = re.compile(r"（(\d+)板）")
UNSUPPORTED_DAILY_KLINE_PATTERN = re.compile(
    r"(?:\d+\s*日线|[一二三四五六七八九十]+日线|均线|前高|放量|缩量|成交量|成交(?!额)[^。！？!?]{0,8}(?:放大|萎缩|活跃|持续|保持)|换手[^。！？!?]{0,4}(?:放大|萎缩|活跃))"
)
VAGUE_PLAN_CONDITION_MARKERS = (
    "近期箱体",
    "显著",
    "明显",
    "强势",
    "走强",
    "走弱",
    "转强",
    "转弱",
    "维持活跃",
    "关键位置",
    "大幅",
    "符合预期",
    "一致反馈",
    "关键证据失效",
    "更多",
    "多只",
    "多数",
    "普遍",
    "大面积",
    "进一步放大",
    "明确突破",
    "集体",
)
# 数量范围词即使与涨停、封板等离散事件同句，也必须给出可复核的数字阈值。
STRICTLY_QUANTIFIED_PLAN_CONDITION_MARKERS = (
    "更多",
    "多只",
    "多数",
    "普遍",
    "大面积",
    "进一步放大",
    "明确突破",
    "集体",
)
# 这些词自身没有定义可复核对象或幅度，句中其它无关数字不能替它们补足边界。
UNDEFINED_PLAN_CONDITION_PATTERNS = (
    re.compile(r"(?:板块)?(?:前排|前列|头部)(?:个股|标的)?"),
    re.compile(r"候选(?:次日)?(?:整体|总体)"),
    re.compile(r"(?:(?:向|到)细分方向扩散|扩散至细分方向)"),
    re.compile(r"(?:所属|所在)(?:方向|板块)(?:延续|持续)"),
    re.compile(r"(?:所属|所在)板块(?:跟涨|跟跌)"),
    re.compile(r"(?:外围|海外|外盘)(?:市场)?连续(?:大跌|下挫)"),
    re.compile(r"(?:盘面)?急剧变化"),
)
DISCRETE_PLAN_CONDITION_MARKERS = (
    "涨停",
    "跌停",
    "炸板未回封",
    "炸板后未回封",
    "回封",
    "晋级",
    "净流入转负",
    "前收",
    "一字板",
    "封板",
)
# 集合事件必须说明任一对象、数量或聚合比较，不能用“有个股”“盘面出现”代替作用域。
GROUP_EVENT_OBJECT_MARKERS = ("候选", "个股", "标的", "盘面")
GROUP_EVENT_MARKERS = (
    "红盘",
    "收涨",
    "收跌",
    "高开",
    "低开",
    "涨停",
    "跌停",
    "封板",
    "炸板",
    "晋级",
    "跟涨",
    "跟跌",
    "前收",
)
GROUP_QUANTITY_PATTERN = re.compile(
    r"(?:至少|至多|超过|不超过|多于|少于|不低于|不高于)?\s*"
    r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)\s*(?:只|家|个|%|成)"
)
GROUP_SELECTOR_PATTERN = re.compile(
    r"(?:(?:候选|个股|标的)(?:中|里)(?:的)?(?:任一|任意|每一|全部|所有)|"
    r"(?:任一|任意一|每一|全部|所有)(?:只)?(?:候选|个股|标的))"
)
FRINGE_CANDIDATE_OVERRIDE_PATTERN = re.compile(
    r"(?:任一|任意(?:一)?|至少\s*(?:1|一)\s*只|有\s*(?:1|一)\s*只)"
    r"(?:候选|个股|标的)?"
)
GROUP_AGGREGATE_COMPARISON_PATTERN = re.compile(
    r"(?:涨停|跌停|封板|炸板|晋级|红盘|收涨|收跌)(?:家数|数量|只数|比例|率)"
    r"[^。！？!?]{0,16}(?:高于|低于|不高于|不低于|多于|少于|超过|不超过)"
)
UNSCOPED_GROUP_EVENT_PATTERN = re.compile(
    r"(?:(?:有|出现)(?:个股|标的)|盘面(?:出现)?)"
    r"[^，,；;。！？!?]{0,4}(?:红盘|收涨|收跌|高开|低开|涨停|跌停|封板|炸板|晋级)"
)
NAMED_STOCK_EVENT_PATTERN = re.compile(
    r"(?<!\d)\d{6}(?!\d)[^，,；;。！？!?]{0,6}"
    r"(?:红盘|收涨|收跌|高开|低开|涨停|跌停|封板|炸板|晋级|前收)"
)
UNBOUNDED_MARKET_PATH_MARKERS = ("高开高走", "低开低走", "同步上行")
MARKET_PATH_PRICE_THRESHOLD_PATTERN = re.compile(
    r"(?:高开|低开|收盘(?:涨幅|跌幅)?|收涨|收跌|涨幅|跌幅)"
    r"[^，,；;。！？!?]{0,4}"
    r"(?:\d+(?:\.\d+)?\s*(?:%|点)|(?:高于|低于|不低于|不高于)前收)"
)
ABSOLUTE_UNBOUNDED_NEXT_DAY_MARKERS = (
    "各自触发条件",
    "逐候选触发条件",
    "关注对象满足触发条件",
)
SCOPED_NEXT_DAY_MARKERS = (
    "急剧变化",
    "延续强势",
    "盘中抗跌",
    "带动板块",
    "带动方向",
    "扩散",
    "走强",
    "走弱",
    "转强",
    "转弱",
)
SENTIMENT_HISTORY_METRIC_PATTERN = re.compile(
    r"(?:情绪|涨停(?:数|家数)?|跌停(?:数|家数)?|炸板率|最高连板(?:高度)?)"
)
SENTIMENT_HISTORY_STATE_PATTERN = re.compile(
    r"(?:仍在|仍处于|维持|持续|保持|连续|偏高|偏低|中等(?:水平)?|中高位|中低位|高位|低位)"
)
PREVIEW_SCENARIO_PUNCTUATION_PATTERN = re.compile(r"[\s，,；;。！？!?、]+")
MARKET_BALANCED_EXPECTATION_PATTERN = re.compile(r"涨跌幅(?:绝对值)?不超过")
MARKET_NEGATIVE_BRANCH_PATTERN = re.compile(r"(?:跌破|低于)前收|收跌")
MARKET_BALANCED_RANGE_VALUE_PATTERN = re.compile(
    r"涨跌幅(?:绝对值)?不超过\s*(?:(\d+(?:\.\d+)?)\s*%|(一个百分点))"
)
MARKET_DOWNSIDE_VALUE_PATTERN = re.compile(
    r"(?:跌幅|下跌|收跌)[^0-9，,；;。！？!?]{0,8}"
    r"(?:超过|高于|不低于)?\s*(\d+(?:\.\d+)?)\s*%"
)
SCENARIO_EVENT_QUANTITY_PATTERN = re.compile(
    r"至少\s*(\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)\s*(?:只|家)"
    r"(?:候选|个股|标的)?[^，,；;。！？!?]{0,8}"
    r"(收盘不低于前收|不低于前收|涨停|封板|跌停|晋级|收涨|红盘)"
)
OVER_EXPECTATION_ENHANCEMENT_MARKERS = (
    "高于复盘日",
    "多于复盘日",
    "超过复盘日",
    "突破",
    "创出新高",
    "一字板",
    "同步收涨",
    "同步上行",
    "全部收涨",
    "均收涨",
)
SCENARIO_POINT_THRESHOLD_PATTERN = re.compile(
    r"(?:站上|达到|突破|高于|不低于)\s*(\d+(?:\.\d+)?)\s*点"
)
SCENARIO_UPSIDE_PERCENT_THRESHOLD_PATTERN = re.compile(
    r"(?:涨幅|上涨|收涨)[^0-9，,；;。！？!?]{0,8}"
    r"(?:不低于|超过|高于|达到)?\s*(\d+(?:\.\d+)?)\s*%"
)
SCENARIO_AMOUNT_THRESHOLD_PATTERN = re.compile(
    r"成交额[^，,；;。！？!?]{0,8}?(\d+(?:\.\d+)?)\s*(万亿|亿元)"
)
SCENARIO_RATIO_THRESHOLD_PATTERN = re.compile(
    r"(?:比例|占比)[^0-9，,；;。！？!?]{0,8}"
    r"(?:不低于|超过|高于|达到|为)?\s*(\d+(?:\.\d+)?)\s*%"
)
OVER_EXPECTATION_UPPER_BOUND_PATTERN = re.compile(
    r"(?:涨幅|上涨|收涨|点位|成交额)[^，,；;。！？!?]{0,8}(?:不高于|不超过)"
)
REASSESSMENT_ACTION_MARKERS = ("重新评估", "重新核查", "重新筛选", "再人工确认")
REASSESSMENT_EVIDENCE_MARKERS = ("补齐", "取得", "获得", "已验证", "验证完成", "证据齐备")
NEGATED_REASSESSMENT_PATTERN = re.compile(
    r"(?:(?:仍|尚)?(?:未|无法|不能|不可|没有|尚未))"
    r"[^，,；;。！？!?]{0,4}(?:补齐|取得|获得|验证完成|已验证|证据齐备)"
)
NEGATED_REASSESSMENT_ACTION_PATTERN = re.compile(
    r"(?:不|无需|无法|不能|不可)[^，,；;。！？!?]{0,2}"
    r"(?:重新评估|重新核查|重新筛选|再人工确认)"
)
NEGATED_ACQUIRED_EVIDENCE_PATTERN = re.compile(
    r"并非(?:已经|已)?(?:补齐|取得|获得|验证完成|已验证|证据齐备)"
)
STILL_MISSING_EVIDENCE_PATTERN = re.compile(
    r"(?:仍|尚|依然|继续)[^，,；;。！？!?]{0,3}"
    r"(?:缺失|未补齐|未取得|未获得|不可确认|无法确认)"
)
FOCUSLESS_EXECUTION_MARKERS = (
    "参与",
    "执行",
    "买入",
    "建仓",
    "开仓",
    "下单",
    "加仓",
    "进场",
    "介入",
    "试仓",
    "配置仓位",
    "设置仓位",
)
FOCUSLESS_NEGATED_EXECUTION_MARKERS = (
    "不设置可执行仓位",
    "不设可执行仓位",
    "不参与",
    "不得参与",
    "不恢复参与",
    "不代表恢复参与",
    "不执行",
    "不得执行",
    "不买入",
    "不建仓",
    "不开仓",
    "不得开仓",
    "不下单",
    "不得下单",
    "不加仓",
    "不得加仓",
    "不进场",
    "不得进场",
    "不介入",
    "不得介入",
    "不试仓",
    "不配置仓位",
    "不设置仓位",
)
REPORT_ONLY_PARTICIPATION_MARKERS = (
    "不设置参与条件",
    "人工评估是否参与",
    "纳入人工参与评估",
    "参与条件",
    "是否参与",
    "能否参与",
)
SENTIMENT_LIMITATION_MARKERS = ("不支持", "不足以", "不证明", "不能据此判定", "不能据此认定", "不得据此")
UNVERIFIED_STOCK_IDENTITY_MARKERS = ("核心票", "龙头", "中军")
RELATIVE_STRENGTH_CLAIM_MARKERS = (
    "相对抗跌",
    "相对偏强",
    "相对偏弱",
    "相对强势",
    "相对弱势",
    "相对表现偏强",
    "相对表现偏弱",
    "相对领先",
    "相对落后",
    "强势候选",
    "弱势候选",
    "强区间对象",
    "弱区间对象",
    "近期走强对象",
    "近期走弱对象",
    "走势独立",
    "带动所属方向",
    "涨幅突出",
    "涨幅有限",
    "趋势延续价值",
    "趋势延续观察价值",
)
STEP_SEVEN_NO_FOCUS_MARKERS = (
    "无法筛出重点对象",
    "不能筛出重点对象",
    "不足以筛出重点对象",
    "无法确定重点对象",
    "不能确定重点对象",
)
SECTOR_NET_INFLOW_SCOPE_MARKERS = ("热门题材榜", "强势行业榜", "强势概念榜")
MARKET_AMOUNT_MARKERS = ("成交额", "量能")
MARKET_AMOUNT_HISTORY_STATE_MARKERS = ("维持", "持续", "高位", "较高水平")
MARKET_ACTIVITY_CONTINUITY_PATTERN = re.compile(
    r"(?:资金|交投)(?:仍然|依然|持续|保持)活跃"
)
MARKET_PATH_CLAIM_MARKERS = (
    "未出现单边趋势",
    "呈现单边趋势",
    "形成单边趋势",
    "震荡格局",
    "延续震荡",
    "震荡延续",
    "连续上涨",
    "连续下跌",
    "逐日走高",
    "逐日走低",
)
ABSOLUTE_MARKET_PATH_CLAIM_MARKERS = (
    "未出现单边趋势",
    "呈现单边趋势",
    "形成单边趋势",
)
UNVERIFIED_BOX_POSITION_MARKERS = ("箱体中部", "箱体上沿", "箱体下沿")
SENTIMENT_DIRECTIONAL_COMPARISON_PATTERN = re.compile(
    r"(?:边际(?:改善|恶化|回升|回落|走强|走弱)|"
    r"较(?:前一日|上一日|昨日|前期|此前)[^，,；;。！？!?]{0,8}"
    r"(?:改善|恶化|回升|回落|上升|下降|走强|走弱))"
)
CANDIDATE_AVERAGE_PRICE_PATTERN = re.compile(
    r"(?:(?:5|五)\s*日|(?:20|二十)\s*日|"
    r"(?:5|五)\s*/\s*(?:20|二十)\s*日)(?:平均价|均价)"
)
SECTOR_PATH_CLAIM_MARKERS = (
    "创出新高",
    "首次回调",
    "重新走强",
    "连续两日强势",
)
UNCALIBRATED_PROBABILITY_MARKERS = (
    "可能性较高",
    "概率较高",
    "大概率",
    "胜率较高",
)
PROFIT_EFFECT_DIRECTION_PATTERN = re.compile(
    r"赚钱效应(?:代理)?(?P<context>[^，,；;。！？!?]{0,12})"
    r"(?P<direction>为正|为负|正值|负值|偏强|偏弱|偏暖|偏冷|改善|恶化|回升|下降|尚可)"
)
UNCERTAIN_CLAIM_MARKERS = (
    "倾向",
    "待确认",
    "待核查",
    "无法确认",
    "不能确认",
    "尚未确认",
    "未确认",
    "不认定",
    "未验证",
    "无法判断",
    "不能判断",
    "是否",
    "能否",
)
SENTIMENT_CYCLE_CLAIM_MARKERS = (
    "回暖",
    "退潮",
    "修复",
    "冰点",
    "上升期",
    "主升期",
    "混沌期",
)
SECTOR_STAGE_CLAIM_MARKERS = (
    "核心板块",
    "核心方向",
    "主线",
    "主升板块",
    "主升阶段",
    "主升期",
)
STEP_COUNTER_EVIDENCE = {
    1: "短区间指数路径与单日横截面可以形成阶段倾向，但不能单独确认完整箱体边界。",
    2: "连板梯队、候选容量与资金痕迹可以形成风格线索，但有限候选排序不等于全市场风格已确认。",
    3: "盘后情绪横截面可以形成回暖或弱反弹倾向，但不能单独确认连续情绪周期。",
    4: "同源排名与内部表现可以形成核心方向倾向，但重复上榜本身不等于核心板块身份。",
    5: "板块路径和量价事实可支持阶段倾向，但单一排名或单日强势不能独立确认主升阶段。",
    6: "角色来源、区间表现和容量排序只能形成未确认候选，不能把排序直接升级为核心票身份。",
    7: "证据排序可以收敛次日重点核验对象，但重点核验不等于交易资格或相对强弱结论。",
}

STEP_MODEL_GUIDANCE = {
    1: "完整五根指数收盘路径、复盘日成交额、涨跌家数和有效美股窗口可支持低或中置信度的市场阶段倾向（例如箱体震荡）；缺少 OHLC 或更长区间边界时不得写成已确认箱体。",
    2: "最高连板仅代表复盘日高度事实；结构化连板梯队、有限候选容量排序、板块排行和龙虎榜在本步可用时必须全部纳入证据字段并综合解释。缺少连续情绪历史时不能描述为仍在、维持或持续，也不能在没有阈值时判为高、中、低位。",
    3: "缺少连续情绪历史时可以给出低或中置信度的回暖、弱反弹、退潮或修复倾向，但必须保留连续历史缺口，不得写成已确认周期。",
    4: "重复上榜、当日排名和内部事实可形成低或中置信度的核心方向倾向，但必须保留连续 K 线和扩散缺口，不得写成已确认核心板块。",
    5: "缺少板块连续 K 线时可以给出低或中置信度的主线或主升候选倾向，但必须保留路径和量能缺口，不得写成已确认阶段。",
    6: "必须分别检查系统发现候选与用户真实池；可按板块领涨、连板高度、5/20 日表现、成交容量和流通市值形成最多三只未确认 AI 候选，不得把排序升级为核心票身份。",
    7: "只能把本地证据排序的最多三只对象列为次日重点核验候选；重点核验不等于已确认核心、交易资格或相对强弱结论。",
}

STEP_REQUIRED_AVAILABLE_FIELDS = {
    2: (
        "sentiment.board_ladder",
        "candidate_history.capacity_ranking",
        "sectors.rankings",
        "stocks.longhubang",
    ),
}

UNAVAILABLE_EVIDENCE_MARKERS = (
    "不存在",
    "为空",
    "未采集",
    "没有可确认代码",
    "没有来源可确认",
    "没有可验证",
    "事实不可用",
)

PREVIEW_OUTPUT_FIELDS = (
    ("market_preview", "大盘", (1, 2, 3)),
    ("sector_preview", "板块", (4, 5)),
    ("stock_preview", "个股", (6, 7)),
)

SECTOR_SOURCE_LABELS = {
    "hhxg_hot_theme": "热门题材榜",
    "hhxg_strong_industry": "强势行业榜",
    "hhxg_strong_concept": "强势概念榜",
    "hhxg_strong_sector": "强势板块榜",
    "akshare_sector": "板块排行",
}

MARKET_NAMED_OBJECT_MARKERS = (
    "上证指数",
    "上证",
    "深证成指",
    "深证",
    "创业板指",
    "创业板",
    "科创50",
    "沪深300",
    "中证500",
    "中证1000",
    "三大指数",
    "标普500",
    "纳斯达克",
    "纳指",
)
CANDIDATE_DENOMINATOR_PATTERN = re.compile(r"(\d+)\s*只(?:AI\s*)?候选(?:集)?(?:中|里|的)")

PREVIEW_SCENARIO_LABELS = {
    "expectation": "符合预期",
    "over_expectation": "超预期",
    "under_expectation": "不及预期",
    "abandon_condition": "放弃条件",
}

STEP9_EVIDENCE_LABELS = {
    "market": "市场事实",
    "sentiment": "情绪事实",
    "sectors": "板块事实",
    "stocks": "个股事实",
    "candidate_history": "候选趋势、成交容量与流通市值",
    "daily_kline": "日线形态",
    "call_auction": "竞价事实",
    "intraday": "分时事实",
    "previous_day_feedback": "前日反馈",
    "next_day_feedback": "次日反馈",
    "regulatory_event": "监管事件",
}

STEP9_STATUS_LABELS = {
    "unavailable": "基础证据不足",
    "not_matched": "当前背景未匹配",
    "base_evidence_available_trigger_pending": "基础证据可用，具体背景与触发待核验",
    "background_matched_trigger_pending": "当前背景已匹配，触发待核验",
    "confirmed": "现有证据已满足",
}

PARTICIPATION_DECISION_LABELS = {
    "yes": "可以参与",
    "conditional": "条件满足后参与",
    "no": "不参与",
}

CANDIDATE_ACTION_LABELS = {
    "buy": "条件买入",
    "watch": "观察",
    "avoid": "回避",
}

UNVERIFIED_IDENTITY_REPLACEMENTS = {
    "具备连板龙头特征": "具备连板候选特征",
    "作为连板龙头": "作为连板候选",
    "具备容量中军特征": "具备容量候选特征",
    "作为算力中军": "作为算力容量候选",
    "走势独立": "相对表现仍待确认",
}


class DetailedReviewError(ValueError):
    pass


@dataclass(frozen=True)
class DetailedStepAnalysis:
    step_number: int
    confidence: str
    conclusion: str
    interpretation: str
    evidence_field_keys: tuple[str, ...]
    next_day_validation: str


@dataclass(frozen=True)
class DetailedCandidate:
    code: str
    name: str
    sector: str
    role: str
    role_source: str
    origin: str
    change_percent: float | None
    close: float | None
    return_5d_percent: float | None
    return_20d_percent: float | None
    average_5d_amount: float | None
    circulating_market_value: float | None
    # 候选换手率在内部固定为 0-1 比率，只在报告渲染时转换为百分数。
    turnover: float | None
    board_count: int
    daily_bar_count: int
    daily_bars_complete: bool


@dataclass(frozen=True)
class DetailedPreviewScope:
    object_name: str
    source_name: str
    metric_name: str
    available_fields: tuple[str, ...]
    candidate_codes: tuple[str, ...] = ()
    denominator: int | None = None


@dataclass(frozen=True)
class DetailedScenarioBand:
    scenario: str
    condition: str


@dataclass(frozen=True)
class DetailedPreviewOutlook:
    primary_judgment: str
    odds_direction: str
    evidence_field_keys: tuple[str, ...]


@dataclass(frozen=True)
class DetailedPreviewDraft:
    target: str
    scope: DetailedPreviewScope
    scenario_bands: tuple[DetailedScenarioBand, ...]
    expectation: str
    over_expectation: str
    under_expectation: str
    abandon_condition: str
    evidence_field_keys: tuple[str, ...]


@dataclass(frozen=True)
class DetailedCandidateAction:
    code: str
    action: str
    rationale: str
    trigger: str
    position_guidance: str
    abandon_condition: str


@dataclass(frozen=True)
class DetailedFinalPlanDraft:
    participation_decision: str
    position_guidance: str
    summary: str
    participation_condition: str
    risk_boundary: str
    focus_codes: tuple[str, ...]
    candidate_actions: tuple[DetailedCandidateAction, ...]
    evidence_field_keys: tuple[str, ...]


@dataclass(frozen=True)
class DetailedModelOutput:
    analyses: tuple[DetailedStepAnalysis, ...]
    preview_outlook: DetailedPreviewOutlook
    preview_drafts: tuple[DetailedPreviewDraft, ...]
    final_plan_draft: DetailedFinalPlanDraft


@dataclass(frozen=True)
class DetailedReview:
    review_date: str
    framework: ReviewFramework
    context: ReviewContext
    analyses: tuple[DetailedStepAnalysis, ...]
    candidates: tuple[DetailedCandidate, ...]
    ranked_candidates: tuple[DetailedCandidate, ...]
    preview_outlook: DetailedPreviewOutlook
    preview_drafts: tuple[DetailedPreviewDraft, ...]
    final_plan_draft: DetailedFinalPlanDraft
    evidence_facts: dict[str, str]
    evidence_references: dict[str, tuple[str, ...]]
    step_evidence: dict[int, StepEvidence]
    step9_checks: tuple[Step9PatternCheck, ...]
    final_responses: tuple[ManualFinalResponse, ...]
    snapshot_reference: str
    model_source: str


@dataclass(frozen=True)
class DetailedReviewRequest:
    review_date: str
    request_id: str
    system_prompt: str
    user_prompt: str
    framework: ReviewFramework
    context: ReviewContext
    candidates: tuple[DetailedCandidate, ...]
    ranked_candidates: tuple[DetailedCandidate, ...]
    evidence_facts: dict[str, str]
    evidence_references: dict[str, tuple[str, ...]]
    step_evidence: dict[int, StepEvidence]
    local_evidence_fields: dict[int, tuple[str, ...]]
    candidate_history_records: tuple[dict[str, Any], ...]
    verified_sector_kline_names: tuple[str, ...]
    verified_relative_comparison_codes: tuple[str, ...]
    final_responses: tuple[ManualFinalResponse, ...]
    snapshot_reference: str


# 请求准备只读取当前框架、快照和 SQLite 中已有记录，不加载 LLM 配置、不联网也不写文件。
def prepare_detailed_review_request(
    review_date: str,
    framework_path: Path,
    snapshot_dir: Path,
    database_path: Path,
) -> DetailedReviewRequest:
    framework = parse_framework_file(framework_path)
    validate_framework(framework)
    context = build_review_context(review_date, snapshot_dir=snapshot_dir, database_path=database_path)
    facts_summary = summarize_review_facts(review_date, snapshot_dir, snapshot_dir)
    step_evidence = build_step_evidence(facts_summary)
    evidence_facts = build_evidence_facts(context, facts_summary)
    evidence_references = dict(facts_summary.evidence_references)
    evidence_references["pool.active_real"] = (
        (f"SQLite:{database_path}#active_real_pool",)
        if context.active_real_pool_items
        else ()
    )
    local_evidence_fields = build_local_evidence_fields(step_evidence, evidence_facts)
    candidates = build_detailed_candidates(context, facts_summary.candidate_history_records)
    ranked_candidates = rank_detailed_candidates(candidates)
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(
        framework,
        context,
        evidence_facts,
        step_evidence,
        local_evidence_fields,
        candidates,
        ranked_candidates,
    )
    request_id = build_detailed_request_id(review_date, system_prompt, user_prompt)
    return DetailedReviewRequest(
        review_date=review_date,
        request_id=request_id,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        framework=framework,
        context=context,
        candidates=candidates,
        ranked_candidates=ranked_candidates,
        evidence_facts=evidence_facts,
        evidence_references=evidence_references,
        step_evidence=step_evidence,
        local_evidence_fields=local_evidence_fields,
        candidate_history_records=facts_summary.candidate_history_records,
        verified_sector_kline_names=build_verified_sector_kline_names(facts_summary),
        verified_relative_comparison_codes=build_verified_relative_comparison_codes(
            context
        ),
        final_responses=tuple(
            list_manual_final_responses(review_date, database_path=database_path)
        ),
        snapshot_reference=str(snapshot_dir / f"{review_date}_snapshot.json"),
    )


# 请求指纹绑定目标日期和完整提示内容，防止旧草案套用到已经变化的本地上下文。
def build_detailed_request_id(review_date: str, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "schema_version": DETAILED_REQUEST_SCHEMA_VERSION,
        "review_date": review_date,
        "system_prompt": system_prompt,
        "user_prompt": json.loads(user_prompt),
    }
    canonical_payload = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


# WorkBuddy 读取单行 JSON 请求后用当前任务的内置模型生成响应，避免额外模型配置和临时仓库文件。
def render_detailed_review_request(request: DetailedReviewRequest) -> str:
    return json.dumps(
        {
            "schema_version": DETAILED_REQUEST_SCHEMA_VERSION,
            "review_date": request.review_date,
            "request_id": request.request_id,
            "system_prompt": request.system_prompt,
            "user_prompt": json.loads(request.user_prompt),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"


# WorkBuddy 响应必须匹配同日同上下文请求，随后仍进入与外部 LLM 相同的业务校验。
def parse_workbuddy_model_response(
    request: DetailedReviewRequest,
    response_text: str,
) -> dict[str, Any]:
    try:
        envelope = json.loads(response_text)
    except json.JSONDecodeError as error:
        raise DetailedReviewError("WorkBuddy 模型响应不是有效 JSON。") from error
    if not isinstance(envelope, dict):
        raise DetailedReviewError("WorkBuddy 模型响应根节点必须是 JSON 对象。")
    if envelope.get("schema_version") != DETAILED_REQUEST_SCHEMA_VERSION:
        raise DetailedReviewError("WorkBuddy 模型响应 schema_version 无效。")
    if envelope.get("review_date") != request.review_date:
        raise DetailedReviewError("WorkBuddy 模型响应日期与当前详细复盘日期不一致。")
    if envelope.get("request_id") != request.request_id:
        raise DetailedReviewError("WorkBuddy 模型响应请求指纹与当前本地上下文不一致。")
    response = envelope.get("model_response")
    if not isinstance(response, dict):
        raise DetailedReviewError("WorkBuddy 模型响应缺少 model_response JSON 对象。")
    return response


# Windows PowerShell 5.1 的原生管道会损坏中文，使用 UTF-8 Base64 只承载传输而不改变模型 JSON。
def decode_workbuddy_response_base64(encoded_response: str) -> str:
    try:
        response_bytes = base64.b64decode(encoded_response.strip(), validate=True)
        return response_bytes.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise DetailedReviewError("WorkBuddy 模型响应不是有效的 UTF-8 Base64。") from error


# 两种模型来源最终都只经过这一条本地校验和固定渲染数据组装链路。
def complete_detailed_review(
    request: DetailedReviewRequest,
    response: dict[str, Any],
    model_source: str,
) -> DetailedReview:
    model_output = validate_model_response(
        response,
        request.local_evidence_fields,
        request.candidates,
        request.ranked_candidates,
        request.step_evidence,
        build_known_sector_names(request.context, request.candidates),
        request.evidence_facts,
        request.context,
        request.verified_sector_kline_names,
        request.verified_relative_comparison_codes,
    )
    return DetailedReview(
        review_date=request.review_date,
        framework=request.framework,
        context=request.context,
        analyses=model_output.analyses,
        candidates=request.candidates,
        ranked_candidates=request.ranked_candidates,
        preview_outlook=model_output.preview_outlook,
        preview_drafts=model_output.preview_drafts,
        final_plan_draft=model_output.final_plan_draft,
        evidence_facts=request.evidence_facts,
        evidence_references=request.evidence_references,
        step_evidence=request.step_evidence,
        step9_checks=verify_step9_patterns(
            request.context.snapshot,
            request.candidate_history_records,
            tuple(candidate.code for candidate in request.ranked_candidates),
        ),
        final_responses=request.final_responses,
        snapshot_reference=request.snapshot_reference,
        model_source=model_source,
    )


# 详细复盘构建只读取本地输入并调用模型生成受约束解释，不写报告、数据库或业务对象。
def build_detailed_review(
    review_date: str,
    framework_path: Path,
    snapshot_dir: Path,
    database_path: Path,
    request_completion: Callable[[OpenAICompatibleSettings, str, str], dict[str, Any]] = request_json_completion,
    settings: OpenAICompatibleSettings | None = None,
) -> DetailedReview:
    request = prepare_detailed_review_request(
        review_date,
        framework_path,
        snapshot_dir,
        database_path,
    )
    resolved_settings = settings or OpenAICompatibleSettings.from_environment()
    response = request_completion(
        resolved_settings,
        request.system_prompt,
        request.user_prompt,
    )
    return complete_detailed_review(
        request,
        response,
        "openai_compatible_llm",
    )


# WorkBuddy 路径直接消费当前任务内置模型的响应，不读取项目 .env，也不回退调用外部接口。
def build_detailed_review_from_workbuddy_response(
    review_date: str,
    framework_path: Path,
    snapshot_dir: Path,
    database_path: Path,
    response_text: str,
) -> DetailedReview:
    request = prepare_detailed_review_request(
        review_date,
        framework_path,
        snapshot_dir,
        database_path,
    )
    response = parse_workbuddy_model_response(request, response_text)
    return complete_detailed_review(
        request,
        response,
        "workbuddy_builtin_llm",
    )


# 正式入口只把已校验草案写入 Markdown 和本地日志，不修改 SQLite、池子、计划或 Observation。
def create_detailed_review_report(
    review_date: str,
    framework_path: Path,
    snapshot_dir: Path,
    database_path: Path,
    output_dir: Path = DEFAULT_DETAILED_REPORT_DIR,
    log_path: Path = DEFAULT_LOG_PATH,
    request_completion: Callable[[OpenAICompatibleSettings, str, str], dict[str, Any]] = request_json_completion,
    settings: OpenAICompatibleSettings | None = None,
    workbuddy_response_text: str | None = None,
) -> Path:
    if workbuddy_response_text is None:
        review = build_detailed_review(
            review_date,
            framework_path,
            snapshot_dir,
            database_path,
            request_completion=request_completion,
            settings=settings,
        )
    else:
        review = build_detailed_review_from_workbuddy_response(
            review_date,
            framework_path,
            snapshot_dir,
            database_path,
            workbuddy_response_text,
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{review_date}_detailed_review.md"
    output_path.write_text(render_detailed_review(review), encoding="utf-8")
    write_detailed_review_log(review, framework_path, output_path, log_path)
    return output_path


def validate_framework(framework: ReviewFramework) -> None:
    numbers = tuple(step.number for step in framework.steps)
    if numbers != REQUIRED_STEP_NUMBERS:
        raise DetailedReviewError("详细复盘要求框架按顺序完整包含 STEP 1 至 STEP 10。")


# 候选范围只合并当前快照的可确认个股事实和有效真实池，同名领涨事实只合并到唯一可确认代码。
def build_detailed_candidates(
    context: ReviewContext,
    candidate_history_records: tuple[dict[str, Any], ...],
) -> tuple[DetailedCandidate, ...]:
    candidates: dict[str, DetailedCandidate] = {}
    coded_stocks_by_code: dict[str, dict[str, Any]] = {}
    history_by_code = {
        str(record.get("code")): record
        for record in candidate_history_records
        if record.get("code") not in (None, "")
    }
    coded_name_counts: dict[str, int] = {}
    uncoded_stocks_by_name: dict[str, list[dict[str, Any]]] = {}
    for stock in context.snapshot.stocks:
        code = str(stock.get("code") or "").strip()
        name_key = normalize_candidate_name(stock.get("name"))
        if not name_key:
            continue
        if code.isdigit() and len(code) == 6:
            coded_name_counts[name_key] = coded_name_counts.get(name_key, 0) + 1
            coded_stocks_by_code[code] = stock
        else:
            uncoded_stocks_by_name.setdefault(name_key, []).append(stock)
    for stock in context.snapshot.stocks:
        code = str(stock.get("code") or "").strip()
        name = str(stock.get("name") or "").strip()
        if not code.isdigit() or len(code) != 6 or not name:
            continue
        history = history_by_code.get(code, {})
        name_key = normalize_candidate_name(name)
        aliases = (
            uncoded_stocks_by_name.get(name_key, [])
            if coded_name_counts.get(name_key) == 1
            else []
        )
        sectors = merge_candidate_labels(
            str(stock.get("sector") or "待确认"),
            *(str(alias.get("sector") or "") for alias in aliases),
        )
        roles = merge_candidate_labels(
            str(stock.get("role") or "市场事实候选"),
            *(str(alias.get("role") or "") for alias in aliases),
        )
        role_source = merge_candidate_labels(
            str(stock.get("role_source") or stock.get("source") or "快照个股事实"),
            *(
                str(alias.get("role_source") or alias.get("source") or "")
                for alias in aliases
            ),
        )
        daily_bar_count = normalize_non_negative_int(history.get("daily_bar_count"))
        candidates[code] = DetailedCandidate(
            code=code,
            name=name,
            sector=sectors,
            role=roles,
            role_source=role_source,
            origin="系统发现候选",
            change_percent=stock.get("change_percent"),
            close=history.get("close"),
            return_5d_percent=history.get("return_5d_percent"),
            return_20d_percent=history.get("return_20d_percent"),
            average_5d_amount=history.get("average_5d_amount"),
            circulating_market_value=history.get("circulating_market_value"),
            turnover=resolve_candidate_turnover_ratio(history, stock),
            board_count=extract_board_count(role_source),
            daily_bar_count=daily_bar_count,
            daily_bars_complete=has_complete_candidate_daily_bars(
                history,
                context.review_date,
            ),
        )
    for item in context.active_real_pool_items:
        existing = candidates.get(item.code)
        history = history_by_code.get(item.code, {})
        daily_bar_count = normalize_non_negative_int(history.get("daily_bar_count"))
        candidates[item.code] = DetailedCandidate(
            code=item.code,
            name=item.name,
            sector="、".join(item.sectors) or (existing.sector if existing else "待确认"),
            role=existing.role if existing else "用户真实池对象",
            role_source=existing.role_source if existing else "用户真实池",
            origin="系统发现候选 + 用户真实池" if existing else "用户真实池",
            change_percent=existing.change_percent if existing else None,
            close=history.get("close") if history else (existing.close if existing else None),
            return_5d_percent=history.get("return_5d_percent") if history else (existing.return_5d_percent if existing else None),
            return_20d_percent=history.get("return_20d_percent") if history else (existing.return_20d_percent if existing else None),
            average_5d_amount=history.get("average_5d_amount") if history else (existing.average_5d_amount if existing else None),
            circulating_market_value=history.get("circulating_market_value") if history else (existing.circulating_market_value if existing else None),
            turnover=resolve_candidate_turnover_ratio(
                history,
                coded_stocks_by_code.get(item.code, {}),
            ),
            board_count=existing.board_count if existing else 0,
            daily_bar_count=(
                daily_bar_count if history else (existing.daily_bar_count if existing else 0)
            ),
            daily_bars_complete=(
                has_complete_candidate_daily_bars(history, context.review_date)
                if history
                else (existing.daily_bars_complete if existing else False)
            ),
        )
    return tuple(candidates.values())


# 新候选历史优先使用显式单位；旧文件只按已知来源兼容，未知来源不猜测单位。
def resolve_candidate_turnover_ratio(
    history: dict[str, Any],
    stock: dict[str, Any],
) -> float | None:
    history_turnover = history.get("turnover")
    if history_turnover is not None:
        turnover_unit = history.get("turnover_unit")
        if turnover_unit is None:
            turnover_unit = {
                "akshare:stock_zh_a_daily": "ratio",
                "akshare:stock_zh_a_hist": "percent",
            }.get(str(history.get("source") or ""))
        if turnover_unit == "ratio":
            return float(history_turnover)
        if turnover_unit == "percent":
            return float(history_turnover) / 100
    # 快照只有 turnover_rate 明确表示百分数；含义不明确的 turnover 不作为兜底。
    snapshot_turnover = stock.get("turnover_rate")
    return float(snapshot_turnover) / 100 if snapshot_turnover is not None else None


# 板块阶段和路径只能绑定到逐对象验证通过的 K 线，不能沿用全局缺口已解除的宽口径。
def build_verified_sector_kline_names(
    facts_summary: ReviewFactsSummary,
) -> tuple[str, ...]:
    verified_names = {
        str(sector.get("name") or "").strip()
        for sector in facts_summary.sectors
        if str(sector.get("name") or "").strip()
        and has_verified_sector_kline(sector)
    }
    evidence = facts_summary.sector_kline_evidence
    try:
        lookback_bars = int(evidence.get("lookback_bars") or 0)
    except (TypeError, ValueError):
        lookback_bars = 0
    verified_names.update(
        str(record.get("name") or "").strip()
        for record in evidence.get("records") or []
        if isinstance(record, dict)
        and str(record.get("name") or "").strip()
        and has_complete_sector_kline_record(
            record,
            facts_summary.trade_date,
            lookback_bars,
        )
    )
    return tuple(sorted(verified_names))


# 相对强弱结论必须逐候选同时具备已验证的相对表现和连续比较事实。
def build_verified_relative_comparison_codes(
    context: ReviewContext,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(stock.get("code") or "").strip()
                for stock in context.snapshot.stocks
                if str(stock.get("code") or "").strip().isdigit()
                and len(str(stock.get("code") or "").strip()) == 6
                and is_complete_fact(stock.get("relative_strength"))
                and is_complete_fact(stock.get("continuous_comparison"))
            }
        )
    )


def normalize_candidate_name(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def merge_candidate_labels(*values: str) -> str:
    labels = tuple(
        dict.fromkeys(
            label.strip()
            for value in values
            for label in SECTOR_SEPARATOR_PATTERN.split(value)
            if label.strip() and label.strip() not in {"待确认", "未知", "无"}
        )
    )
    return "、".join(labels) or "待确认"


def extract_board_count(role_source: str) -> int:
    matches = tuple(int(value) for value in BOARD_COUNT_PATTERN.findall(role_source))
    return max(matches, default=0)


def normalize_non_negative_int(value: Any) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return normalized if normalized >= 0 else 0


# 完整日 K 能力必须同时有明确标记、21 根未复权 OHLCV/成交额/换手记录，旧证据不会误解锁模式核验。
def has_complete_candidate_daily_bars(record: dict[str, Any], review_date: str) -> bool:
    bars = record.get("daily_bars")
    recent_bars = bars[-21:] if isinstance(bars, list) else []
    bar_dates = tuple(
        str(bar.get("date") or "") for bar in recent_bars if isinstance(bar, dict)
    )
    try:
        tuple(date.fromisoformat(bar_date) for bar_date in bar_dates)
    except (TypeError, ValueError):
        return False
    return bool(
        record.get("daily_bars_complete") is True
        and record.get("adjustment") == "none"
        and normalize_non_negative_int(record.get("daily_bar_count")) >= 21
        and isinstance(bars, list)
        and len(bars) >= 21
        and len(bar_dates) == 21
        and len(set(bar_dates)) == 21
        and tuple(sorted(bar_dates)) == bar_dates
        and bar_dates[-1] == review_date
        and all(has_complete_candidate_daily_bar(bar) for bar in recent_bars)
    )


def has_complete_candidate_daily_bar(value: Any) -> bool:
    if not isinstance(value, dict) or not value.get("date"):
        return False
    numeric_fields = ("open", "high", "low", "close", "volume", "amount", "turnover")
    if not all(
        isinstance(value.get(field_name), (int, float))
        and not isinstance(value.get(field_name), bool)
        and math.isfinite(float(value[field_name]))
        for field_name in numeric_fields
    ):
        return False
    open_price = float(value["open"])
    high_price = float(value["high"])
    low_price = float(value["low"])
    close_price = float(value["close"])
    return bool(
        min(open_price, close_price) >= low_price > 0
        and max(open_price, close_price) <= high_price
        and float(value["volume"]) >= 0
        and float(value["amount"]) >= 0
        and float(value["turnover"]) >= 0
    )


# 本地排序只形成待核验候选：先看板块领涨与连板事实的交叉，再看高度、区间涨幅和成交容量。
def rank_detailed_candidates(
    candidates: tuple[DetailedCandidate, ...],
) -> tuple[DetailedCandidate, ...]:
    def descending_value(value: float | None) -> float:
        return value if isinstance(value, (int, float)) else float("-inf")

    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                -int("板块领涨" in candidate.role and candidate.board_count > 0),
                -candidate.board_count,
                -descending_value(candidate.return_5d_percent),
                -descending_value(candidate.return_20d_percent),
                -descending_value(candidate.average_5d_amount),
                candidate.code,
            ),
        )[:MAX_FOCUS_CANDIDATES]
    )


# 板块名只来自当前快照排行和候选自身归属，用于阻止模型把已知板块错绑到其它候选。
def build_known_sector_names(
    context: ReviewContext,
    candidates: tuple[DetailedCandidate, ...],
) -> tuple[str, ...]:
    sector_names = {
        str(sector.get("name") or "").strip()
        for sector in context.snapshot.sectors
        if str(sector.get("name") or "").strip()
    }
    for candidate in candidates:
        sector_names.update(split_candidate_sector_names(candidate.sector))
    sector_names.difference_update({"待确认", "未知", "无"})
    return tuple(sorted(sector_names, key=lambda name: (-len(name), name)))


def split_candidate_sector_names(value: str) -> set[str]:
    return {
        name.strip()
        for name in SECTOR_SEPARATOR_PATTERN.split(value)
        if name.strip() and name.strip() not in {"待确认", "未知", "无"}
    }


# 本地白名单是证据路由的唯一权威；缺失字段不传给模型，也不进入报告证据链。
def build_local_evidence_fields(
    step_evidence: dict[int, StepEvidence],
    evidence_facts: dict[str, str],
) -> dict[int, tuple[str, ...]]:
    return {
        step_number: tuple(
            field_name
            for field_name in evidence.allowed_field_keys
            if is_local_evidence_available(evidence_facts.get(field_name, ""))
        )
        for step_number, evidence in step_evidence.items()
    }


def is_local_evidence_available(value: str) -> bool:
    if not value.strip() or any(marker in value for marker in UNAVAILABLE_EVIDENCE_MARKERS):
        return False
    primary_clause = value.split("；", maxsplit=1)[0].strip()
    return not primary_clause.endswith(("：待确认", "：无"))


def merge_local_evidence_fields(
    local_evidence_fields: dict[int, tuple[str, ...]],
    step_numbers: tuple[int, ...],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            field_name
            for step_number in step_numbers
            for field_name in local_evidence_fields[step_number]
        )
    )


def build_system_prompt() -> str:
    return (
        "你是 A 股短线盘后复盘的证据解释助手。只能返回用户要求的 JSON 对象。"
        "必须严格遵循每个 STEP 的 framework_rules，并且只能使用该 STEP 的 local_evidence_fields。"
        "结论必须写成基于证据的推断，不得把缺失证据补成事实，不得跨 STEP 搬运字段。"
        "STEP 1 至 STEP 7 不得创造历史数字、消息、股票代码、股票名称、板块归属或行情走势；具体历史数值统一由本地证据链展示。"
        "STEP 1 至 STEP 7 只输出本步推断、证据解释和次日验证点，不输出逐候选行动、买卖或仓位方案。"
        "缺少身份验证时只能称为候选，不得把候选直接认定为龙头、核心票或中军。"
        "缺少相对强弱或连续比较证据时，仍可使用候选自身的板块领涨、连板高度、5/20 日表现、成交容量和流通市值排出最多三只未确认 AI 候选，"
        "但不得据此断言相对抗跌、近期走弱、走势独立、带动方向或强势候选。"
        "STEP 8 和 STEP 10 是明确标记的 AI 条件计划，可以提出数字阈值、买卖方向和仓位方案，但不得把计划参数写成已发生事实。"
        "计划中的股票代码只能来自 candidate_universe，逐只计划必须同时包含触发、仓位和放弃条件。"
        "必须完整输出 STEP 1 至 STEP 7，并生成 STEP 8 的主判断、三类四情景草案和 STEP 10 的条件化行动计划。"
        "每个 STEP 分析必须返回非空 evidence_field_keys，并且只能列出该 STEP local_evidence_fields 中实际用于结论的键。"
        "候选行动不得把本地已知但不属于该候选的板块名称写成该候选归属。"
        "candidate_universe 中 daily_bars_complete=true 只表示本地 STEP 9 可核验其日 K 证据；模型不得据此自行生成日线、均线、前高、放量、缩量或换手活跃判断。"
        "市场历史只提供指数收盘序列时，STEP 1、STEP 8 主判断和 STEP 10 总纲只能把成交额写成当日达到的事实，"
        "不得确认成交额或量能维持、持续、处于高位或较高水平，也不得用资金仍然活跃等措辞暗示连续性；"
        "指数历史仅为连续性未验证的本地样本路径时，不得确认区间走势；通达信连续五根日 K 的完整收盘路径可形成低或中置信度倾向；带数字阈值的次日条件不受此限制。"
        "当前本地证据不提供可验证的指数箱体边界，不得把指数确定定位为箱体中部、上沿或下沿。"
        "赚钱效应公开公式待确认时，只能保留原始比例与公式缺口，不得解释为正负、强弱、改善或恶化。"
        "缺少连续情绪历史时，不得把回暖、退潮、修复、冰点等情绪周期写成已确认结论，也不得声称情绪边际改善或边际恶化。"
        "缺少板块连续 K 线时，不得把当日排行直接认定为核心板块、主线或主升阶段，"
        "也不得声称创出新高、首次回调、重新走强或连续两日强势。"
        "STEP 8 和 STEP 10 的条件必须使用数字阈值或涨跌停、炸板未回封、晋级、净流入转负、前收等离散明确事件，"
        "不得只写近期箱体、显著、明显、强势、走强、走弱或关键位置。"
        "更多、多只、多数、普遍、大面积、进一步放大、明确突破或集体等词即使与涨停、封板同句也必须给出数字阈值。"
        "前排个股、候选整体、扩散至细分方向、所属方向延续、外围市场连续大跌或急剧变化没有可复核边界，"
        "不得用句中其它无关数字绕过，必须改写为命名对象、数量或明确参照。"
        "候选、个股、标的或盘面的涨跌停、红盘、高低开等集合事件必须写明任一对象、数量、比例或家数比较。"
        "STEP 8 每类四情景必须可区分；封板与涨停等同义改写不能分别充当符合和超预期，放弃条件不能复述不及预期。"
        "情景命中顺序固定为放弃条件、不及预期、超预期、符合预期；高开高走、低开低走或同步上行还必须给出本分句价格阈值。"
        "STEP 1 至 STEP 7 的次日验证点不得使用急剧变化、延续强势、盘中抗跌、带动板块或未定义的各自触发条件。"
        "STEP 8 主判断和 STEP 10 总纲不得使用可能性较高、概率较高、大概率或胜率较高等无校准概率。"
        "STEP 8 板块条件和 STEP 10 使用净流入时，必须明确写热门题材榜、强势行业榜或强势概念榜口径。"
        "AI shortlist 没有用户明确确认身份字段，STEP 10 只能输出条件参与或不参与、watch/avoid 和明确的仓位上限，不得输出 yes、已确认买点或 buy。"
        "STEP 8 和 STEP 10 的条件不得把 AI shortlist 当作已确认核心票、龙头或中军。"
        "候选证据中的 5 日与 20 日字段是涨跌幅，不得改写成五日均价、二十日均价或平均价。"
        "所有计划都必须写成尚未执行的条件方案，不得声称已经成交或自动执行。"
    )


# 提示词只提供框架规则和本地证据；人工记录不进入模型输入，只在报告渲染阶段后置对照。
def build_user_prompt(
    framework: ReviewFramework,
    context: ReviewContext,
    evidence_facts: dict[str, str],
    step_evidence: dict[int, StepEvidence],
    local_evidence_fields: dict[int, tuple[str, ...]],
    candidates: tuple[DetailedCandidate, ...],
    ranked_candidates: tuple[DetailedCandidate, ...],
) -> str:
    sentiment_history_missing = "missing_sentiment_history" in step_evidence[3].hard_missing_fields
    sector_kline_missing = any(
        "missing_sector_kline" in step_evidence[number].hard_missing_fields
        for number in (4, 5)
    )
    relative_comparison_missing = any(
        gap in step_evidence[number].hard_missing_fields
        for number in (6, 7)
        for gap in ("missing_relative_strength", "missing_continuous_comparison")
    )
    market_amount_history_available = "成交额" in evidence_facts.get("market.history_range", "")
    market_index_path_unverified_samples = (
        "连续性未验证" in evidence_facts.get("market.history_range", "")
        or "样本收盘路径" in evidence_facts.get("market.history_range", "")
    )
    profit_effect_calculation = context.snapshot.sentiment.get(
        "profit_effect_calculation"
    )
    profit_effect_formula_unverified = has_unverified_profit_effect(
        context.snapshot.sentiment
    )
    steps = []
    for step in framework.steps[:7]:
        evidence = step_evidence[step.number]
        steps.append(
            {
                "step_number": step.number,
                "title": step.title,
                "framework_rules": step.body,
                "local_evidence_fields": {
                    field_name: evidence_facts[field_name]
                    for field_name in local_evidence_fields[step.number]
                },
                "hard_missing_fields": list(evidence.hard_missing_fields),
                "analysis_guidance": STEP_MODEL_GUIDANCE.get(step.number, "按框架规则和本步证据归纳，不补写缺失事实。"),
            }
        )
    preview_scopes = build_preview_scopes(context, candidates)
    payload = {
        "review_date": context.review_date,
        "steps": steps,
        "active_real_pool_items": [
            {"code": item.code, "name": item.name, "sectors": list(item.sectors)}
            for item in context.active_real_pool_items
        ],
        "candidate_universe": [candidate.__dict__ for candidate in candidates],
        "ai_candidate_shortlist": [candidate.__dict__ for candidate in ranked_candidates],
        "ai_candidate_boundary": "本地证据排序的未确认候选，最多三只；不代表核心票身份、买点或自动交易。",
        "preview_scopes": {
            target: {
                **scope.__dict__,
                "scenario_bands": [band.__dict__ for band in build_local_scenario_bands(target)],
            }
            for target, scope in preview_scopes.items()
        },
        "local_claim_boundaries": {
            "sentiment_cycle": (
                "连续情绪历史缺失；可写低或中置信度的回暖、弱反弹、退潮或修复倾向，必须保留该缺口，不得写成已确认周期"
                if sentiment_history_missing
                else "可按连续情绪事实形成待核查推断"
            ),
            "sector_stage": (
                "板块连续 K 线缺失；可写低或中置信度的核心方向、主线或主升候选倾向，必须保留路径、量能和扩散缺口，不得写成已确认阶段"
                if sector_kline_missing
                else "可按板块连续事实形成待核查推断"
            ),
            "stock_identity": "代码和名称验证不等于用户确认核心身份；AI shortlist 始终只作未确认候选条件计划",
            "relative_comparison": (
                "缺少相对强弱或连续比较证据；可使用 ai_candidate_shortlist 做证据排序和次日重点核验，但不得下相对强弱、核心身份或交易资格结论"
                if relative_comparison_missing
                else "可按相对强弱和连续比较事实筛选有限重点对象"
            ),
            "market_amount_history": (
                "仅有当日成交额；STEP 1、STEP 8 主判断和 STEP 10 总纲不得确认成交额或量能维持、持续或处于高位"
                if not market_amount_history_available
                else "可按连续成交额历史形成待核查推断"
            ),
            "market_index_path": (
                "指数历史仅为连续性未验证的本地样本路径；不得确认区间走势"
                if market_index_path_unverified_samples
                else "可按通达信连续五根日 K 的完整收盘路径形成低或中置信度倾向"
            ),
            "profit_effect_formula": (
                (
                    "涨跌停家数比例代理不等同完整赚钱效应；只保留计算值，"
                    "不据此判断完整赚钱效应的正负、强弱、改善或恶化"
                    if profit_effect_calculation == "limit_up_ratio_proxy"
                    else "赚钱效应公开公式待确认；只保留原始比例，不据此判断正负、强弱、改善或恶化"
                )
                if profit_effect_formula_unverified
                else "可按已验证公式解释赚钱效应"
            ),
            "plan_conditions": (
                "必须使用数字阈值或涨跌停、炸板未回封、晋级、净流入转负、前收等离散明确事件；"
                "不得只写近期箱体、显著、明显、强势、走强、走弱或关键位置；"
                "更多、多只、多数、普遍、大面积、进一步放大、明确突破或集体必须给出数字阈值；"
                "候选、个股、标的或盘面事件必须写明任一对象、数量、比例或家数比较；"
                "四档不得同义改写，超预期必须比符合预期增加可复核强度，放弃条件不得复述不及预期；"
                "STEP 8 的固定对象、数据源、候选集分母和四档主指标由 preview_scopes 本地定义；"
                "模型只做补充说明，不得跨榜单拼接字段，不得用任一边缘候选事件覆盖整个候选集"
            ),
        },
        "required_output": {
            "step_analyses": [
                {
                    "step_number": "1 至 7 的单个编号",
                    "confidence": "low、medium 或 high；存在本步硬缺口时不得为 high",
                    "conclusion": "本步基于证据的简明推断，不含交易指令",
                    "interpretation": "结合框架规则解释证据与限制",
                    "evidence_field_keys": ["该 STEP local_evidence_fields 中实际使用的至少一个键"],
                    "next_day_validation": "下一交易日需要观察什么来确认或推翻当前推断",
                }
            ],
            "preview_outlook": {
                "primary_judgment": (
                    "次日主情景预演，不给出未经校准的概率高低；情绪周期只能写待确认，不得确认回暖、退潮或修复"
                    if sentiment_history_missing
                    else "次日主情景预演，不给出未经校准的概率高低；可以基于连续情绪事实形成待核查推断"
                ),
                "odds_direction": "rising、neutral、falling 或 unavailable",
            },
            "market_preview": {
                "expectation": "对 preview_scopes 大盘对象和主指标的符合预期补充说明",
                "over_expectation": "对超预期分档的补充说明，不得改写主指标边界",
                "under_expectation": "对不及预期分档的补充说明，不得改写主指标边界",
                "abandon_condition": "对放弃分档的补充说明，不得改写主指标边界",
            },
            "sector_preview": {
                "expectation": "只解释 preview_scopes 中固定板块对象、榜单源和现有字段",
                "over_expectation": "只解释同一对象与同一榜单源，不得跨榜单拼接涨停家数与净流入",
                "under_expectation": "只解释同一对象与同一榜单源",
                "abandon_condition": "只解释同一对象与同一榜单源",
            },
            "stock_preview": {
                "expectation": "只解释 candidate_universe 全部有限去重候选的整体反馈，分母和代码固定为 preview_scopes",
                "over_expectation": "只解释候选集整体反馈，不得用任一候选的单一事件覆盖整体",
                "under_expectation": "只解释候选集整体反馈",
                "abandon_condition": "只解释候选集整体反馈",
            },
            "final_plan_draft": {
                "participation_decision": "conditional 或 no；AI shortlist 未经用户确认不得输出 yes",
                "position_guidance": "必须写明不设置仓位，或写明仅报告内条件方案的总仓位上限（如总仓位上限 30%）",
                "summary": "次日条件化行动总纲，区分事实、推断和计划",
                "participation_condition": (
                    "哪些可复核条件满足后仅将候选纳入人工参与评估；不得声称已执行"
                ),
                "risk_boundary": "哪些事实出现后停止或降低计划",
                "focus_codes": (
                    ["ai_candidate_shortlist 中最多三个代码；仅作未确认候选"]
                ),
                "candidate_actions": [
                    {
                        "code": "必须来自 focus_codes",
                        "action": "只能为 watch 或 avoid",
                        "rationale": "只基于候选本地事实说明理由；身份未确认时只能写候选；缺少相对比较时不得写强弱标签",
                        "trigger": "次日满足什么条件后执行；允许数字阈值",
                        "position_guidance": "必须写不设置仓位，或写明单只仓位上限",
                        "abandon_condition": "什么情况下取消该对象计划",
                    }
                ],
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False)


# 模型输出必须保持固定结构，所有字段、数字和代码均需能在同 STEP 本地输入中找到来源。
def validate_model_response(
    response: dict[str, Any],
    local_evidence_fields: dict[int, tuple[str, ...]],
    candidates: tuple[DetailedCandidate, ...],
    ranked_candidates: tuple[DetailedCandidate, ...],
    step_evidence: dict[int, StepEvidence],
    known_sector_names: tuple[str, ...],
    evidence_facts: dict[str, str],
    context: ReviewContext,
    verified_sector_kline_names: tuple[str, ...],
    verified_relative_comparison_codes: tuple[str, ...],
) -> DetailedModelOutput:
    required_root_fields = {
        "step_analyses",
        "preview_outlook",
        "market_preview",
        "sector_preview",
        "stock_preview",
        "final_plan_draft",
    }
    if not required_root_fields.issubset(response):
        raise DetailedReviewError("详细复盘模型输出缺少 STEP 分析、预演草案或最终计划草案。")
    raw_analyses = response.get("step_analyses")
    if not isinstance(raw_analyses, list) or not all(isinstance(item, dict) for item in raw_analyses):
        raise DetailedReviewError("详细复盘模型输出 step_analyses 必须是对象列表。")

    analyses: list[DetailedStepAnalysis] = []
    numbers: set[int] = set()
    required_fields = {
        "step_number",
        "confidence",
        "conclusion",
        "interpretation",
        "evidence_field_keys",
        "next_day_validation",
    }
    for raw_analysis in raw_analyses:
        if not required_fields.issubset(raw_analysis):
            raise DetailedReviewError("每个 STEP 分析缺少规定字段。")
        number = normalize_model_step_number(raw_analysis.get("step_number"))
        if number in numbers:
            raise DetailedReviewError("详细复盘模型输出不能重复 STEP。")
        numbers.add(number)
        confidence = raw_analysis.get("confidence")
        if confidence not in {"low", "medium", "high"}:
            raise DetailedReviewError("每个 STEP 分析的 confidence 必须是 low、medium 或 high。")
        # 本步仍有硬缺口时，方向性判断只能保留低或中置信度，不能包装成高置信度事实。
        if step_evidence[number].hard_missing_fields and confidence == "high":
            raise DetailedReviewError(f"STEP {number} 存在硬缺口时 confidence 不得为 high。")
        selected_fields = validate_analysis_evidence_field_keys(
            raw_analysis.get("evidence_field_keys"),
            local_evidence_fields[number],
            number,
        )
        conclusion = validate_analysis_prose(raw_analysis.get("conclusion"), "本步结论")
        interpretation = validate_analysis_prose(raw_analysis.get("interpretation"), "证据解释")
        next_day_validation = validate_analysis_prose(raw_analysis.get("next_day_validation"), "次日验证点")
        validate_next_day_validation_prose(next_day_validation, number)
        if number in {6, 7}:
            validate_unsupported_daily_kline_prose(conclusion, f"STEP {number}本步结论")
            validate_unsupported_daily_kline_prose(interpretation, f"STEP {number}证据解释")
            validate_unsupported_daily_kline_prose(next_day_validation, f"STEP {number}次日验证点")
            conclusion = normalize_unverified_identity_claims(conclusion)
            interpretation = normalize_unverified_identity_claims(interpretation)
            next_day_validation = normalize_unverified_identity_claims(next_day_validation)
        analyses.append(
            DetailedStepAnalysis(
                number,
                confidence,
                conclusion,
                interpretation,
                selected_fields,
                next_day_validation,
            )
        )
    if numbers != set(MODEL_STEP_NUMBERS):
        raise DetailedReviewError("详细复盘模型输出必须完整覆盖 STEP 1 至 STEP 7。")
    preview_outlook = validate_preview_outlook(
        response.get("preview_outlook"),
        local_evidence_fields,
    )
    preview_drafts = validate_preview_drafts(
        response,
        local_evidence_fields,
        {candidate.code for candidate in candidates},
        True,
        build_preview_scopes(context, candidates),
    )
    final_plan_draft = validate_final_plan_draft(
        response.get("final_plan_draft"),
        local_evidence_fields,
        candidates,
        ranked_candidates,
        step_evidence,
        known_sector_names,
    )
    normalized_analyses = tuple(sorted(analyses, key=lambda item: item.step_number))
    validate_model_evidence_boundaries(
        normalized_analyses,
        preview_outlook,
        preview_drafts,
        final_plan_draft,
        step_evidence,
        "成交额" in evidence_facts.get("market.history_range", ""),
        (
            "连续性未验证" in evidence_facts.get("market.history_range", "")
            or "样本收盘路径" in evidence_facts.get("market.history_range", "")
        ),
        has_unverified_profit_effect(context.snapshot.sentiment),
        candidates,
        verified_sector_kline_names,
        verified_relative_comparison_codes,
    )
    return DetailedModelOutput(
        analyses=normalized_analyses,
        preview_outlook=preview_outlook,
        preview_drafts=preview_drafts,
        final_plan_draft=final_plan_draft,
    )


# 缺少连续证据时，模型可以描述待确认方向，但不能把周期、核心或主升标签写成已确认事实。
def validate_model_evidence_boundaries(
    analyses: tuple[DetailedStepAnalysis, ...],
    preview_outlook: DetailedPreviewOutlook,
    preview_drafts: tuple[DetailedPreviewDraft, ...],
    final_plan_draft: DetailedFinalPlanDraft,
    step_evidence: dict[int, StepEvidence],
    market_amount_history_available: bool,
    market_index_path_unverified_samples: bool,
    profit_effect_formula_unverified: bool,
    candidates: tuple[DetailedCandidate, ...],
    verified_sector_kline_names: tuple[str, ...],
    verified_relative_comparison_codes: tuple[str, ...],
) -> None:
    analysis_by_step = {analysis.step_number: analysis for analysis in analyses}
    preview_plan_prose = tuple(
        prose
        for preview in preview_drafts
        for prose in (
            preview.expectation,
            preview.over_expectation,
            preview.under_expectation,
            preview.abandon_condition,
        )
    )
    final_condition_prose = (
        final_plan_draft.summary,
        final_plan_draft.participation_condition,
        final_plan_draft.risk_boundary,
        *(action.trigger for action in final_plan_draft.candidate_actions),
        *(action.abandon_condition for action in final_plan_draft.candidate_actions),
    )
    shared_plan_prose = (
        preview_outlook.primary_judgment,
        *preview_plan_prose,
        *final_condition_prose,
    )
    all_analysis_prose = tuple(
        prose
        for analysis in analyses
        for prose in (
            analysis.conclusion,
            analysis.interpretation,
            analysis.next_day_validation,
        )
    )

    if "missing_sentiment_history" in step_evidence[3].hard_missing_fields:
        step_two = analysis_by_step[2]
        step_three = analysis_by_step[3]
        sentiment_claim_prose = (
            step_two.conclusion,
            step_two.interpretation,
            step_three.conclusion,
            step_three.interpretation,
        )
        validate_sentiment_history_state_claims(
            sentiment_claim_prose
        )
        validate_sentiment_directional_comparisons(sentiment_claim_prose)
        validate_unconfirmed_claims(
            (
                step_three.conclusion,
                step_three.interpretation,
                step_three.next_day_validation,
                *shared_plan_prose,
            ),
            SENTIMENT_CYCLE_CLAIM_MARKERS,
            "缺少连续情绪历史时不得确认回暖、退潮、修复或冰点",
        )

    sector_analysis_prose = tuple(
        prose
        for number in (4, 5)
        for prose in (
            analysis_by_step[number].conclusion,
            analysis_by_step[number].interpretation,
            analysis_by_step[number].next_day_validation,
        )
    )
    if any(
        "missing_sector_kline" in step_evidence[number].hard_missing_fields
        for number in (4, 5)
    ):
        validate_unconfirmed_claims(
            (*sector_analysis_prose, *shared_plan_prose),
            SECTOR_STAGE_CLAIM_MARKERS,
            "缺少板块连续 K 线时不得确认核心板块、主线或主升阶段",
        )
        sector_preview_prose = tuple(
            prose
            for preview in preview_drafts
            if preview.target == "板块"
            for prose in (
                preview.expectation,
                preview.over_expectation,
                preview.under_expectation,
                preview.abandon_condition,
            )
        )
        sector_related_plan_prose = (
            preview_outlook.primary_judgment,
            final_plan_draft.summary,
            final_plan_draft.participation_condition,
            final_plan_draft.risk_boundary,
        )
        validate_path_claims(
            (*sector_analysis_prose, *sector_preview_prose),
            SECTOR_PATH_CLAIM_MARKERS,
            "缺少板块连续 K 线时不得断言板块创出新高、回调后重新走强等路径",
        )
        validate_path_claims(
            sector_related_plan_prose,
            SECTOR_PATH_CLAIM_MARKERS,
            "缺少板块连续 K 线时不得断言板块创出新高、回调后重新走强等路径",
            required_context_markers=("板块", "方向"),
        )
    validate_sector_kline_object_claims(
        sector_analysis_prose,
        verified_sector_kline_names,
    )

    step_six = analysis_by_step[6]
    step_seven = analysis_by_step[7]
    validate_unconfirmed_claims(
        (
            step_six.conclusion,
            step_six.interpretation,
            step_seven.conclusion,
            step_seven.interpretation,
            preview_outlook.primary_judgment,
            final_plan_draft.summary,
            *(action.rationale for action in final_plan_draft.candidate_actions),
        ),
        UNVERIFIED_STOCK_IDENTITY_MARKERS,
        "AI shortlist 未经用户确认时不得确认核心票、龙头或中军",
    )

    step_six = analysis_by_step[6]
    step_seven = analysis_by_step[7]
    stock_preview_prose = tuple(
        prose
        for preview in preview_drafts
        if preview.target == "个股"
        for prose in (
            preview.expectation,
            preview.over_expectation,
            preview.under_expectation,
            preview.abandon_condition,
        )
    )
    validate_relative_strength_object_claims(
        (
            step_six.conclusion,
            step_six.interpretation,
            step_seven.conclusion,
            step_seven.interpretation,
            preview_outlook.primary_judgment,
            *stock_preview_prose,
            final_plan_draft.summary,
            *(action.rationale for action in final_plan_draft.candidate_actions),
        ),
        candidates,
        verified_relative_comparison_codes,
    )
    # 候选历史字段是区间涨跌幅，不提供区间均价；报告任何位置都不能偷换指标含义。
    validate_candidate_average_price_claims(
        (
            step_six.conclusion,
            step_six.interpretation,
            step_six.next_day_validation,
            step_seven.conclusion,
            step_seven.interpretation,
            step_seven.next_day_validation,
            *stock_preview_prose,
            final_plan_draft.summary,
            final_plan_draft.participation_condition,
            *(action.rationale for action in final_plan_draft.candidate_actions),
            *(action.trigger for action in final_plan_draft.candidate_actions),
            *(action.abandon_condition for action in final_plan_draft.candidate_actions),
        )
    )

    # 当前市场证据没有箱体上下沿定义；“中部/上沿/下沿”只能写成待确认，不能作定位结论。
    validate_unverified_box_position_claims(
        (
            *all_analysis_prose,
            preview_outlook.primary_judgment,
            final_plan_draft.summary,
        )
    )

    if not market_amount_history_available:
        step_one = analysis_by_step[1]
        validate_market_amount_history_claims(
            (
                step_one.conclusion,
                step_one.interpretation,
                preview_outlook.primary_judgment,
                final_plan_draft.summary,
            )
        )

    if market_index_path_unverified_samples:
        step_one = analysis_by_step[1]
        validate_path_claims(
            (
                step_one.conclusion,
                step_one.interpretation,
                preview_outlook.primary_judgment,
                final_plan_draft.summary,
            ),
            MARKET_PATH_CLAIM_MARKERS,
            "指数历史仅为连续性未验证的本地样本路径时不得确认区间走势",
            absolute_markers=ABSOLUTE_MARKET_PATH_CLAIM_MARKERS,
        )

    if profit_effect_formula_unverified:
        validate_unverified_profit_effect_claims((*all_analysis_prose, *shared_plan_prose))

    validate_forbidden_claims(
        (preview_outlook.primary_judgment, final_plan_draft.summary),
        UNCALIBRATED_PROBABILITY_MARKERS,
        "STEP 8 主判断和 STEP 10 总纲缺少校准依据时不得给出概率高低",
    )


# STEP 8 的主指标对象与数据源由本地快照锁定，模型无权把多个榜单拼成新对象。
def build_preview_scopes(
    context: ReviewContext,
    candidates: tuple[DetailedCandidate, ...],
) -> dict[str, DetailedPreviewScope]:
    indices = context.snapshot.market.get("indices")
    index_items = indices if isinstance(indices, list) else []
    market_index = next(
        (
            item
            for item in index_items
            if isinstance(item, dict) and item.get("name") == "上证指数"
        ),
        next((item for item in index_items if isinstance(item, dict)), {}),
    )
    named_sectors = [
        item
        for item in context.snapshot.sectors
        if str(item.get("name") or "").strip()
    ]
    hot_theme_sectors = [
        item for item in named_sectors if item.get("source_type") == "hhxg_hot_theme"
    ]
    # 固定对象优先取同源热门题材榜的最小有效排名；排名相同或缺失时保持输入顺序。
    sector_candidates = hot_theme_sectors or named_sectors
    sector = min(
        enumerate(sector_candidates),
        key=lambda indexed: (
            indexed[1].get("rank")
            if isinstance(indexed[1].get("rank"), (int, float))
            else float("inf"),
            indexed[0],
        ),
        default=(0, {}),
    )[1]
    sector_source_type = str(sector.get("source_type") or "未知榜单")
    sector_available_fields = tuple(
        field_name
        for field_name in ("rank", "limit_up_count", "net_inflow_yi", "bias_percent")
        if sector.get(field_name) not in (None, "")
    )
    candidate_codes = tuple(candidate.code for candidate in candidates)
    candidate_names = "、".join(
        f"{candidate.code} {candidate.name}" for candidate in candidates
    ) or "无"
    return {
        "大盘": DetailedPreviewScope(
            object_name=str(market_index.get("name") or "上证指数（待确认）"),
            source_name="market.indices",
            metric_name="次日收盘涨跌幅（相对前收）",
            available_fields=("name", "close", "change_percent"),
        ),
        "板块": DetailedPreviewScope(
            object_name=str(sector.get("name") or "板块对象待确认"),
            source_name=SECTOR_SOURCE_LABELS.get(sector_source_type, sector_source_type),
            metric_name="次日同源榜单排名",
            available_fields=sector_available_fields,
        ),
        "个股": DetailedPreviewScope(
            object_name=f"有限去重候选集：{candidate_names}",
            source_name="candidate_universe",
            metric_name="收盘不低于前收的候选数占比",
            available_fields=("code", "name", "close_vs_previous_close"),
            candidate_codes=candidate_codes,
            denominator=len(candidate_codes),
        ),
    }


# 四档主指标的边界在本地固定；等于 30% 和 50% 都有唯一归属，数据缺失统一进入放弃档。
def build_local_scenario_bands(target: str) -> tuple[DetailedScenarioBand, ...]:
    if target == "大盘":
        conditions = {
            "expectation": "0% ≤ 涨跌幅 < 1%",
            "over_expectation": "涨跌幅 ≥ 1%",
            "under_expectation": "-2% ≤ 涨跌幅 < 0%",
            "abandon_condition": "涨跌幅 < -2%，或该对象收盘数据缺失",
        }
    elif target == "板块":
        conditions = {
            "expectation": "同源榜单排名第 2-3 名",
            "over_expectation": "同源榜单排名第 1 名",
            "under_expectation": "同源榜单排名第 4-5 名",
            "abandon_condition": "退出同源榜单前 5 名，或同源榜单数据缺失",
        }
    else:
        conditions = {
            "expectation": "50% ≤ 候选集占比 < 80%",
            "over_expectation": "80% ≤ 候选集占比 ≤ 100%",
            "under_expectation": "30% ≤ 候选集占比 < 50%",
            "abandon_condition": "0% ≤ 候选集占比 < 30%，或任一候选缺少收盘/前收数据",
        }
    return tuple(
        DetailedScenarioBand(scenario, conditions[scenario])
        for scenario in (
            "expectation",
            "over_expectation",
            "under_expectation",
            "abandon_condition",
        )
    )


def validate_unconfirmed_claims(
    prose_values: tuple[str, ...],
    claim_markers: tuple[str, ...],
    error_message: str,
) -> None:
    for prose in prose_values:
        for sentence in PROSE_SENTENCE_PATTERN.findall(prose):
            for clause in PROSE_CLAUSE_PATTERN.findall(sentence):
                if not any(marker in clause for marker in claim_markers):
                    continue
                if any(marker in clause for marker in UNCERTAIN_CLAIM_MARKERS):
                    continue
                if any(f"{marker}候选" in clause for marker in claim_markers):
                    continue
                raise DetailedReviewError(error_message + "。")


# 全局板块缺口解除不代表每个板块都有 K 线；确定性阶段或路径必须点名已验证对象。
def validate_sector_kline_object_claims(
    prose_values: tuple[str, ...],
    verified_sector_names: tuple[str, ...],
) -> None:
    claim_markers = SECTOR_STAGE_CLAIM_MARKERS + SECTOR_PATH_CLAIM_MARKERS
    for prose in prose_values:
        for sentence in PROSE_SENTENCE_PATTERN.findall(prose):
            for clause in PROSE_CLAUSE_PATTERN.findall(sentence):
                matched_markers = tuple(
                    marker for marker in claim_markers if marker in clause
                )
                if not matched_markers:
                    continue
                if any(marker in clause for marker in UNCERTAIN_CLAIM_MARKERS):
                    continue
                if any(f"{marker}候选" in clause for marker in matched_markers):
                    continue
                if any(name in clause for name in verified_sector_names):
                    continue
                raise DetailedReviewError(
                    "STEP 4/5 的确定性板块阶段或路径必须绑定具备完整 K 线的明确板块名。"
                )


# 相对强弱必须绑定到自身比较证据完整的候选，不能由另一只股票的完整字段全局授权。
def validate_relative_strength_object_claims(
    prose_values: tuple[str, ...],
    candidates: tuple[DetailedCandidate, ...],
    verified_candidate_codes: tuple[str, ...],
) -> None:
    verified_codes = set(verified_candidate_codes)
    for prose in prose_values:
        for sentence in PROSE_SENTENCE_PATTERN.findall(prose):
            for clause in PROSE_CLAUSE_PATTERN.findall(sentence):
                if not any(
                    marker in clause for marker in RELATIVE_STRENGTH_CLAIM_MARKERS
                ):
                    continue
                if any(
                    marker in clause
                    for marker in (
                        "无法确认",
                        "不能确认",
                        "不支持",
                        "不足以",
                        "不证明",
                        "未验证",
                        "是否",
                        "能否",
                    )
                ):
                    continue
                mentioned_codes = {
                    candidate.code
                    for candidate in candidates
                    if candidate.code in clause or candidate.name in clause
                }
                if mentioned_codes and mentioned_codes <= verified_codes:
                    continue
                raise DetailedReviewError(
                    "缺少相对强弱或连续比较证据；STEP 6/7 的相对强弱结论必须绑定该候选自身完整的比较事实。"
                )


# 绝对禁用词不接受待确认改写，由调用方决定具体业务边界。
def validate_forbidden_claims(
    prose_values: tuple[str, ...],
    claim_markers: tuple[str, ...],
    error_message: str,
) -> None:
    for prose in prose_values:
        if any(marker in prose for marker in claim_markers):
            raise DetailedReviewError(error_message + "。")


# 路径词在确定性陈述中必须有逐日证据；明确写“无法确认/是否”的限制句仍可保留。
def validate_path_claims(
    prose_values: tuple[str, ...],
    claim_markers: tuple[str, ...],
    error_message: str,
    required_context_markers: tuple[str, ...] = (),
    absolute_markers: tuple[str, ...] = (),
) -> None:
    for prose in prose_values:
        for sentence in PROSE_SENTENCE_PATTERN.findall(prose):
            for clause in PROSE_CLAUSE_PATTERN.findall(sentence):
                if required_context_markers and not any(
                    marker in clause for marker in required_context_markers
                ):
                    continue
                matched_markers = tuple(
                    marker for marker in claim_markers if marker in clause
                )
                if not matched_markers:
                    continue
                if any(marker in clause for marker in absolute_markers):
                    raise DetailedReviewError(error_message + "。")
                if any(marker in clause for marker in UNCERTAIN_CLAIM_MARKERS):
                    continue
                raise DetailedReviewError(error_message + "。")


# 指数历史不等于成交额历史；没有连续成交额时只能写当日达到，不能确认其维持或处于高位。
def validate_market_amount_history_claims(prose_values: tuple[str, ...]) -> None:
    for prose in prose_values:
        for sentence in PROSE_SENTENCE_PATTERN.findall(prose):
            for clause in PROSE_CLAUSE_PATTERN.findall(sentence):
                if MARKET_ACTIVITY_CONTINUITY_PATTERN.search(clause):
                    if any(marker in clause for marker in UNCERTAIN_CLAIM_MARKERS):
                        continue
                    raise DetailedReviewError(
                        "缺少连续成交额历史时不得确认资金或交投仍然活跃。"
                    )
                if not any(marker in clause for marker in MARKET_AMOUNT_MARKERS):
                    continue
                if not any(marker in clause for marker in MARKET_AMOUNT_HISTORY_STATE_MARKERS):
                    continue
                if any(marker in clause for marker in UNCERTAIN_CLAIM_MARKERS):
                    continue
                raise DetailedReviewError(
                    "缺少连续成交额历史时不得确认成交额或量能维持、持续或处于高位。"
                )


# 连续情绪证据缺失时，当日涨跌停、炸板和连板高度不能被解释为持续状态或高中低等级。
def validate_sentiment_history_state_claims(prose_values: tuple[str, ...]) -> None:
    for prose in prose_values:
        for sentence in PROSE_SENTENCE_PATTERN.findall(prose):
            for clause in PROSE_CLAUSE_PATTERN.findall(sentence):
                if not SENTIMENT_HISTORY_METRIC_PATTERN.search(clause):
                    continue
                if not SENTIMENT_HISTORY_STATE_PATTERN.search(clause):
                    continue
                if any(
                    marker in clause
                    for marker in (*UNCERTAIN_CLAIM_MARKERS, *SENTIMENT_LIMITATION_MARKERS)
                ):
                    continue
                raise DetailedReviewError(
                    "缺少连续情绪历史或等级阈值时，不得确认情绪指标仍在、维持或处于高中低水平。"
                )


# 单日情绪横截面没有前序基准，不能形成改善或恶化方向的比较结论。
def validate_sentiment_directional_comparisons(prose_values: tuple[str, ...]) -> None:
    for prose in prose_values:
        if SENTIMENT_DIRECTIONAL_COMPARISON_PATTERN.search(prose):
            raise DetailedReviewError(
                "缺少连续情绪历史时不得形成边际改善、边际恶化等比较结论。"
            )


# 当前证据没有可复核的指数箱体上下沿；不确定性说明可以保留，确定性位置必须拒绝。
def validate_unverified_box_position_claims(prose_values: tuple[str, ...]) -> None:
    for prose in prose_values:
        for sentence in PROSE_SENTENCE_PATTERN.findall(prose):
            for clause in PROSE_CLAUSE_PATTERN.findall(sentence):
                if not any(marker in clause for marker in UNVERIFIED_BOX_POSITION_MARKERS):
                    continue
                if any(marker in clause for marker in UNCERTAIN_CLAIM_MARKERS):
                    continue
                raise DetailedReviewError(
                    "当前证据没有可验证的箱体边界，不得确定定位为箱体中部、上沿或下沿。"
                )


# 5/20 日候选历史是区间涨跌幅，不是区间平均价格，禁止在模型文字中偷换口径。
def validate_candidate_average_price_claims(prose_values: tuple[str, ...]) -> None:
    for prose in prose_values:
        if CANDIDATE_AVERAGE_PRICE_PATTERN.search(prose):
            raise DetailedReviewError(
                "候选证据只有 5/20 日涨跌幅，不得表述为五日均价或二十日均价。"
            )


# 次日验证点必须能在下一交易日复核，不能继续沿用无幅度、无对象或无因果证据的标签。
def validate_next_day_validation_prose(prose: str, step_number: int) -> None:
    if any(marker in prose for marker in ABSOLUTE_UNBOUNDED_NEXT_DAY_MARKERS):
        raise DetailedReviewError(
            f"STEP {step_number} 次日验证点包含无法复核的强弱、抗跌、带动或触发条件。"
        )
    for sentence in PROSE_SENTENCE_PATTERN.findall(prose):
        # “或”两侧可独立触发，必须分别具备作用域，不能由另一分支的对象范围兜底。
        for branch in re.split(r"或者|或", sentence):
            clauses = PROSE_CLAUSE_PATTERN.findall(branch)
            for index, clause in enumerate(clauses):
                scoped_markers = tuple(
                    marker for marker in SCOPED_NEXT_DAY_MARKERS if marker in clause
                )
                if not scoped_markers:
                    continue
                scope_value = clause
                if clause.strip().startswith(("以此", "据此")) and index > 0:
                    scope_value = clauses[index - 1] + clause
                if has_explicit_group_event_scope(scope_value):
                    continue
                raise DetailedReviewError(
                    f"STEP {step_number} 次日验证点提到{'、'.join(scoped_markers)}时"
                    "必须在同一验证项给出对象、数量、比例或家数比较。"
                )


# 赚钱效应公式未公开时，固定比例仍可展示，但模型不得把该比例解释成方向结论。
def validate_unverified_profit_effect_claims(prose_values: tuple[str, ...]) -> None:
    for prose in prose_values:
        for sentence in PROSE_SENTENCE_PATTERN.findall(prose):
            for clause in PROSE_CLAUSE_PATTERN.findall(sentence):
                match = PROFIT_EFFECT_DIRECTION_PATTERN.search(clause)
                if not match:
                    continue
                if any(
                    marker in match.group("context")
                    for marker in (
                        "无法确认",
                        "不能确认",
                        "无法据此确认",
                        "不能认定",
                        "不据此判断",
                        "不作判断",
                        "不判断",
                    )
                ):
                    continue
                raise DetailedReviewError(
                    "赚钱效应公开公式待确认，不得据此判断正负、强弱、改善或恶化。"
                )


# 当前只有“来源公式未验证”和“涨跌停比例代理”两种口径，二者都不能解读为完整赚钱效应方向。
def has_unverified_profit_effect(sentiment: dict[str, Any]) -> bool:
    return sentiment.get("profit_effect_percent") not in (None, "") and sentiment.get(
        "profit_effect_calculation"
    ) != "source_provided_formula_verified"


# STEP 分析只能引用本步实际可用字段；空列表或跨步字段都会让结论失去可核查来源。
def validate_analysis_evidence_field_keys(
    value: Any,
    allowed_fields: tuple[str, ...],
    step_number: int,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise DetailedReviewError(f"STEP {step_number} 证据字段必须是非空字符串列表。")
    normalized_fields = tuple(dict.fromkeys(value))
    if len(normalized_fields) != len(value):
        raise DetailedReviewError(f"STEP {step_number} 证据字段不能重复。")
    unavailable_fields = set(normalized_fields) - set(allowed_fields)
    if unavailable_fields:
        raise DetailedReviewError(
            f"STEP {step_number} 包含本 STEP 白名单之外或当前不可用的证据字段："
            f"{'、'.join(sorted(unavailable_fields))}。"
        )
    required_available_fields = tuple(
        field_name
        for field_name in STEP_REQUIRED_AVAILABLE_FIELDS.get(step_number, ())
        if field_name in allowed_fields
    )
    missing_required_fields = tuple(
        field_name
        for field_name in required_available_fields
        if field_name not in normalized_fields
    )
    if missing_required_fields:
        raise DetailedReviewError(
            f"STEP {step_number} 必须纳入本地已可用的关键证据字段："
            f"{'、'.join(missing_required_fields)}。"
        )
    return normalized_fields


def validate_preview_outlook(
    value: Any,
    local_evidence_fields: dict[int, tuple[str, ...]],
) -> DetailedPreviewOutlook:
    required_fields = {"primary_judgment", "odds_direction"}
    if not isinstance(value, dict) or not required_fields.issubset(value):
        raise DetailedReviewError("详细复盘模型 STEP 8 主判断缺少规定字段。")
    odds_direction = value.get("odds_direction")
    if odds_direction not in {"rising", "neutral", "falling", "unavailable"}:
        raise DetailedReviewError("STEP 8 主判断的值博率方向无效。")
    selected_fields = merge_local_evidence_fields(local_evidence_fields, MODEL_STEP_NUMBERS)
    primary_judgment = validate_analysis_prose(value.get("primary_judgment"), "STEP 8 主判断")
    return DetailedPreviewOutlook(primary_judgment, odds_direction, selected_fields)


# STEP 8 的三类名称和证据路由由本地固定，模型只填写各类四种情景的内容。
def validate_preview_drafts(
    response: dict[str, Any],
    local_evidence_fields: dict[int, tuple[str, ...]],
    allowed_codes: set[str],
    stock_identity_missing: bool,
    preview_scopes: dict[str, DetailedPreviewScope],
) -> tuple[DetailedPreviewDraft, ...]:
    required_fields = {
        "expectation",
        "over_expectation",
        "under_expectation",
        "abandon_condition",
    }
    drafts: list[DetailedPreviewDraft] = []
    for output_field, target, step_numbers in PREVIEW_OUTPUT_FIELDS:
        item = response.get(output_field)
        if not isinstance(item, dict) or not required_fields.issubset(item):
            raise DetailedReviewError(f"详细复盘模型 STEP 8 {target}预演草案缺少规定字段。")
        selected_fields = merge_local_evidence_fields(
            local_evidence_fields,
            step_numbers,
        )
        prose_values = (
            validate_plan_condition_prose(
                item.get("expectation"),
                f"STEP 8 {target}符合预期",
                allowed_codes,
                stock_identity_missing,
            ),
            validate_plan_condition_prose(
                item.get("over_expectation"),
                f"STEP 8 {target}超预期",
                allowed_codes,
                stock_identity_missing,
            ),
            validate_plan_condition_prose(
                item.get("under_expectation"),
                f"STEP 8 {target}不及预期",
                allowed_codes,
                stock_identity_missing,
            ),
            validate_plan_condition_prose(
                item.get("abandon_condition"),
                f"STEP 8 {target}放弃条件",
                allowed_codes,
                stock_identity_missing,
            ),
        )
        if target == "板块":
            validate_sector_net_inflow_scope(prose_values, "STEP 8 板块条件")
        scope = preview_scopes[target]
        validate_preview_scope_notes(target, prose_values, scope)
        raw_draft = DetailedPreviewDraft(
            target=target,
            scope=scope,
            scenario_bands=build_local_scenario_bands(target),
            expectation=prose_values[0],
            over_expectation=prose_values[1],
            under_expectation=prose_values[2],
            abandon_condition=prose_values[3],
            evidence_field_keys=selected_fields,
        )
        validate_preview_scenario_distinction(raw_draft)
        bound_prose_values = tuple(
            bind_preview_scope_to_note(scope, prose) for prose in prose_values
        )
        draft = DetailedPreviewDraft(
            target=target,
            scope=scope,
            scenario_bands=raw_draft.scenario_bands,
            expectation=bound_prose_values[0],
            over_expectation=bound_prose_values[1],
            under_expectation=bound_prose_values[2],
            abandon_condition=bound_prose_values[3],
            evidence_field_keys=selected_fields,
        )
        drafts.append(draft)
    return tuple(drafts)


# 模型补充说明统一附着本地对象、来源和候选分母；主分档始终以本地结构化边界为准。
def bind_preview_scope_to_note(scope: DetailedPreviewScope, prose: str) -> str:
    denominator = (
        f"；固定分母：{scope.denominator} 只"
        if scope.denominator is not None
        else ""
    )
    return (
        f"固定对象：{scope.object_name}；固定数据源：{scope.source_name}{denominator}；"
        f"本说明不改变本地主分档：{prose}"
    )


# 模型只能解释本地锁定的对象和字段；不同榜单的指标不得互相借用。
def validate_preview_scope_notes(
    target: str,
    prose_values: tuple[str, ...],
    scope: DetailedPreviewScope,
) -> None:
    if target == "大盘":
        object_aliases = tuple(
            alias
            for alias in (
                scope.object_name,
                scope.object_name.replace("指数", "").strip(),
            )
            if alias
        )
        for prose in prose_values:
            mentioned_objects = tuple(
                marker for marker in MARKET_NAMED_OBJECT_MARKERS if marker in prose
            )
            if mentioned_objects and not any(alias in prose for alias in object_aliases):
                raise DetailedReviewError(
                    "STEP 8 大盘补充说明不得改写本地固定对象。"
                )
    if target == "板块":
        source_markers = set(SECTOR_SOURCE_LABELS.values()) | {
            "强势行业榜",
            "强势概念榜",
        }
        unavailable_metric_markers = tuple(
            marker
            for field_name, marker in (
                ("rank", "排名"),
                ("limit_up_count", "涨停家数"),
                ("net_inflow_yi", "净流入"),
                ("bias_percent", "乖离率"),
            )
            if field_name not in scope.available_fields
        )
        for prose in prose_values:
            mismatched_sources = tuple(
                source for source in source_markers if source in prose and source != scope.source_name
            )
            if mismatched_sources:
                raise DetailedReviewError(
                    "STEP 8 板块条件不得引用固定对象数据源之外的榜单："
                    f"{'、'.join(sorted(mismatched_sources))}。"
                )
            if scope.source_name in prose and scope.object_name not in prose:
                raise DetailedReviewError(
                    "STEP 8 板块补充说明显式引用固定榜单时必须同时写明固定板块名。"
                )
            used_unavailable_metrics = tuple(
                marker for marker in unavailable_metric_markers if marker in prose
            )
            if used_unavailable_metrics:
                raise DetailedReviewError(
                    "STEP 8 板块条件使用了固定榜单未提供的字段："
                    f"{'、'.join(used_unavailable_metrics)}。"
                )
    if target == "个股":
        for prose in prose_values:
            if FRINGE_CANDIDATE_OVERRIDE_PATTERN.search(prose):
                raise DetailedReviewError(
                    "STEP 8 个股条件不得用任一或至少一只边缘候选事件覆盖整个候选集。"
                )
            if STOCK_CODE_PATTERN.search(prose):
                raise DetailedReviewError(
                    "STEP 8 个股补充说明只能描述完整 candidate_universe 聚合，不得命名单只代码。"
                )
            denominator_match = CANDIDATE_DENOMINATOR_PATTERN.search(prose)
            if (
                denominator_match
                and scope.denominator is not None
                and int(denominator_match.group(1)) != scope.denominator
            ):
                raise DetailedReviewError(
                    "STEP 8 个股补充说明中的候选集分母与本地 candidate_universe 不一致。"
                )
            has_aggregate_scope = bool(
                "候选集" in prose
                and any(
                    marker in prose
                    for marker in ("占比", "比例", "候选数", "候选数量")
                )
            ) or bool("候选" in prose and GROUP_QUANTITY_PATTERN.search(prose))
            if not has_aggregate_scope:
                raise DetailedReviewError(
                    "STEP 8 个股补充说明必须绑定完整 candidate_universe 候选集聚合指标。"
                )


# 四档情景先做最小语义去重；不解析完整行情，只拒绝同义分档和确定会交叉的宽区间。
def validate_preview_scenario_distinction(draft: DetailedPreviewDraft) -> None:
    scenario_values = (
        draft.expectation,
        draft.over_expectation,
        draft.under_expectation,
        draft.abandon_condition,
    )
    normalized_values = tuple(normalize_preview_scenario(value) for value in scenario_values)
    if len(set(normalized_values)) != len(normalized_values):
        raise DetailedReviewError(
            f"STEP 8 {draft.target}四情景不得使用同义条件重复分档。"
        )
    normalized_under = normalized_values[2]
    normalized_abandon = normalized_values[3]
    if normalized_abandon and normalized_abandon in normalized_under:
        raise DetailedReviewError(
            f"STEP 8 {draft.target}放弃情景不得完整复述不及预期中的同一条件。"
        )
    if not is_over_expectation_stronger(draft.expectation, draft.over_expectation):
        raise DetailedReviewError(
            f"STEP 8 {draft.target}超预期情景必须比符合预期增加可复核的强度条件。"
        )
    if draft.target == "大盘" and MARKET_BALANCED_EXPECTATION_PATTERN.search(
        draft.expectation
    ):
        expectation_range = extract_market_balanced_range(draft.expectation)
        for branch in re.split(r"或者|或", draft.under_expectation):
            if not MARKET_NEGATIVE_BRANCH_PATTERN.search(branch):
                continue
            downside_value = extract_market_downside_value(branch)
            if (
                expectation_range is not None
                and downside_value is not None
                and downside_value > expectation_range
            ):
                continue
            raise DetailedReviewError(
                "STEP 8 大盘四情景中，符合预期的涨跌宽区间与不及预期的收跌分支发生重叠。"
            )


def normalize_preview_scenario(value: str) -> str:
    normalized = value.replace("封住涨停", "涨停").replace("封板", "涨停")
    normalized = normalized.replace("达到涨停", "涨停")
    normalized = normalized.replace("炸板后未能回封", "炸板未回封")
    normalized = normalized.replace("炸板未能回封", "炸板未回封")
    normalized = normalized.replace("炸板后未回封", "炸板未回封")
    normalized = normalized.replace("保持为正", "为正").replace("维持为正", "为正")
    normalized = normalized.replace("保持为负", "为负").replace("维持为负", "为负")
    normalized = normalized.replace("有个股", "个股")
    # “任一候选”和“候选中至少一只”表达同一集合事件，统一后才能识别重复分档。
    normalized = re.sub(
        r"候选(?:中|里)(?:的)?(?:至少\s*(?:1|一)\s*只|(?:任一|任意)(?:只)?)",
        "任一候选",
        normalized,
    )
    normalized = re.sub(r"(?:任一|任意)(?:只)?候选", "任一候选", normalized)
    for filler in ("次日", "继续", "出现", "发生", "达到", "实现", "成功", "仍有"):
        normalized = normalized.replace(filler, "")
    return PREVIEW_SCENARIO_PUNCTUATION_PATTERN.sub("", normalized)


def is_over_expectation_stronger(expectation: str, over_expectation: str) -> bool:
    if OVER_EXPECTATION_UPPER_BOUND_PATTERN.search(over_expectation):
        return False
    expectation_strength = preview_event_strength(expectation)
    over_strength = preview_event_strength(over_expectation)
    if over_strength < expectation_strength:
        return False
    expectation_thresholds = preview_metric_thresholds(expectation)
    over_thresholds = preview_metric_thresholds(over_expectation)
    shared_metric_names = set(expectation_thresholds) & set(over_thresholds)
    if any(
        over_thresholds[metric_name] < expectation_thresholds[metric_name]
        for metric_name in shared_metric_names
    ):
        return False
    metric_strengthened = any(
        over_thresholds[metric_name] > expectation_thresholds[metric_name]
        for metric_name in shared_metric_names
    )
    expectation_quantities = preview_event_quantities(expectation)
    over_quantities = preview_event_quantities(over_expectation)
    shared_event_names = set(expectation_quantities) & set(over_quantities)
    if any(
        over_quantities[event_name] < expectation_quantities[event_name]
        for event_name in shared_event_names
    ):
        return False
    quantity_strengthened = any(
        over_quantities[event_name] > expectation_quantities[event_name]
        for event_name in shared_event_names
    )
    if metric_strengthened or quantity_strengthened:
        return True
    if shared_event_names and over_strength == expectation_strength:
        return False
    if over_strength > expectation_strength:
        return True
    if any(marker in over_expectation for marker in OVER_EXPECTATION_ENHANCEMENT_MARKERS):
        return True
    return bool(over_thresholds or over_quantities)


def preview_event_strength(value: str) -> int:
    if any(marker in value for marker in ("晋级", "一字板", "创出新高")):
        return 3
    if any(marker in value for marker in ("涨停", "封板")):
        return 2
    # 明确的正涨幅下限强于仅“不低于前收”，不能因措辞未重复“收涨”而被误判为降级。
    if (
        any(marker in value for marker in ("收涨", "红盘", "不低于前收"))
        or SCENARIO_UPSIDE_PERCENT_THRESHOLD_PATTERN.search(value)
    ):
        return 1
    return 0


def preview_event_quantities(value: str) -> dict[str, float]:
    normalized = value.replace("封板", "涨停").replace("达到涨停", "涨停")
    quantities: dict[str, float] = {}
    for quantity, event_name in SCENARIO_EVENT_QUANTITY_PATTERN.findall(normalized):
        normalized_event_name = (
            "不低于前收" if "不低于前收" in event_name else event_name
        )
        quantities[normalized_event_name] = parse_scenario_quantity(quantity)
    return quantities


def parse_scenario_quantity(value: str) -> float:
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100}
    total = 0
    current = 0
    for character in value:
        if character in digits:
            current = digits[character]
            continue
        if character in units:
            total += (current or 1) * units[character]
            current = 0
    return float(total + current)


def preview_metric_thresholds(value: str) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    if point_match := SCENARIO_POINT_THRESHOLD_PATTERN.search(value):
        thresholds["index_point"] = float(point_match.group(1))
    if percent_match := SCENARIO_UPSIDE_PERCENT_THRESHOLD_PATTERN.search(value):
        thresholds["upside_percent"] = float(percent_match.group(1))
    if amount_match := SCENARIO_AMOUNT_THRESHOLD_PATTERN.search(value):
        amount_value = float(amount_match.group(1))
        thresholds["market_amount_yi"] = (
            amount_value * 10000 if amount_match.group(2) == "万亿" else amount_value
        )
    if ratio_match := SCENARIO_RATIO_THRESHOLD_PATTERN.search(value):
        thresholds["candidate_ratio_percent"] = float(ratio_match.group(1))
    return thresholds


def extract_market_balanced_range(value: str) -> float | None:
    match = MARKET_BALANCED_RANGE_VALUE_PATTERN.search(value)
    if not match:
        return None
    return 1.0 if match.group(2) else float(match.group(1))


def extract_market_downside_value(value: str) -> float | None:
    match = MARKET_DOWNSIDE_VALUE_PATTERN.search(value)
    return float(match.group(1)) if match else None


# STEP 10 只允许从有限候选中生成少量条件行动，并要求总仓位与逐候选风险边界完整。
def validate_final_plan_draft(
    value: Any,
    local_evidence_fields: dict[int, tuple[str, ...]],
    candidates: tuple[DetailedCandidate, ...],
    ranked_candidates: tuple[DetailedCandidate, ...],
    step_evidence: dict[int, StepEvidence],
    known_sector_names: tuple[str, ...],
) -> DetailedFinalPlanDraft:
    required_fields = {
        "participation_decision",
        "position_guidance",
        "summary",
        "participation_condition",
        "risk_boundary",
        "focus_codes",
        "candidate_actions",
    }
    if not isinstance(value, dict) or not required_fields.issubset(value):
        raise DetailedReviewError("详细复盘模型 STEP 10 最终计划草案缺少规定字段。")
    candidate_codes = {candidate.code for candidate in candidates}
    ranked_candidate_codes = {candidate.code for candidate in ranked_candidates}
    focus_codes = value.get("focus_codes")
    if not isinstance(focus_codes, list) or not all(isinstance(code, str) for code in focus_codes):
        raise DetailedReviewError("STEP 10 关注代码必须是列表。")
    normalized_codes = tuple(dict.fromkeys(focus_codes))
    if len(normalized_codes) != len(focus_codes) or len(normalized_codes) > MAX_FOCUS_CANDIDATES:
        raise DetailedReviewError("STEP 10 只能选择不重复的少量关注代码。")
    if any(code not in ranked_candidate_codes for code in normalized_codes):
        raise DetailedReviewError("STEP 10 包含有限候选范围之外（AI 候选排序范围之外）的股票代码。")
    participation_decision = value.get("participation_decision")
    if participation_decision not in PARTICIPATION_DECISION_LABELS:
        raise DetailedReviewError("STEP 10 次日参与结论无效。")
    stock_identity_missing = "missing_stock_identity" in step_evidence[6].hard_missing_fields
    candidate_actions = validate_candidate_actions(
        value.get("candidate_actions"),
        normalized_codes,
        candidates,
        known_sector_names,
        True,
    )
    selected_fields = merge_local_evidence_fields(local_evidence_fields, MODEL_STEP_NUMBERS)
    position_guidance = validate_plan_prose(
        value.get("position_guidance"), "STEP 10总仓位", candidate_codes
    )
    summary = validate_plan_prose(value.get("summary"), "STEP 10总纲", candidate_codes)
    participation_condition = validate_plan_condition_prose(
        value.get("participation_condition"),
        "STEP 10参与条件",
        candidate_codes,
        True,
    )
    risk_boundary = validate_plan_condition_prose(
        value.get("risk_boundary"),
        "STEP 10风险边界",
        candidate_codes,
        True,
    )
    validate_sector_net_inflow_scope(
        (
            summary,
            participation_condition,
            risk_boundary,
            *(action.rationale for action in candidate_actions),
            *(action.trigger for action in candidate_actions),
            *(action.abandon_condition for action in candidate_actions),
        ),
        "STEP 10",
    )
    validate_unconfirmed_ai_shortlist_plan(
        participation_decision,
        position_guidance,
        summary,
        participation_condition,
        risk_boundary,
        candidate_actions,
    )
    if not normalized_codes:
        missing_fields = {
            gap
            for number in (6, 7)
            for gap in step_evidence[number].hard_missing_fields
        }
        required_reassessment_markers = tuple(
            marker
            for is_missing, marker in (
                (stock_identity_missing, "个股身份"),
                ("missing_relative_strength" in missing_fields, "相对强弱"),
                ("missing_continuous_comparison" in missing_fields, "连续比较"),
            )
            if is_missing
        )
        validate_plan_without_focus_candidates(
            participation_decision,
            position_guidance,
            participation_condition,
            required_reassessment_markers,
        )
    return DetailedFinalPlanDraft(
        participation_decision,
        position_guidance,
        summary,
        participation_condition,
        risk_boundary,
        normalized_codes,
        candidate_actions,
        selected_fields,
    )


def validate_candidate_actions(
    value: Any,
    focus_codes: tuple[str, ...],
    candidates: tuple[DetailedCandidate, ...],
    known_sector_names: tuple[str, ...],
    stock_identity_missing: bool,
) -> tuple[DetailedCandidateAction, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise DetailedReviewError("STEP 10 逐候选计划必须是对象列表。")
    required_fields = {
        "code",
        "action",
        "rationale",
        "trigger",
        "position_guidance",
        "abandon_condition",
    }
    candidate_by_code = {candidate.code: candidate for candidate in candidates}
    candidate_codes = set(candidate_by_code)
    actions: list[DetailedCandidateAction] = []
    seen_codes: set[str] = set()
    for item in value:
        if not required_fields.issubset(item):
            raise DetailedReviewError("STEP 10 逐候选计划缺少规定字段。")
        code = item.get("code")
        action = item.get("action")
        if not isinstance(code, str) or code not in candidate_codes or code not in focus_codes:
            raise DetailedReviewError("STEP 10 逐候选计划包含有限关注范围之外的股票代码。")
        if code in seen_codes:
            raise DetailedReviewError("STEP 10 逐候选计划不能重复股票代码。")
        if action not in CANDIDATE_ACTION_LABELS:
            raise DetailedReviewError("STEP 10 逐候选行动类型无效。")
        seen_codes.add(code)
        candidate_action = DetailedCandidateAction(
            code=code,
            action=action,
            rationale=normalize_unverified_identity_claims(
                validate_plan_prose(
                    item.get("rationale"), "STEP 10候选理由", candidate_codes
                )
            ),
            trigger=validate_plan_condition_prose(
                item.get("trigger"),
                "STEP 10候选触发条件",
                candidate_codes,
                stock_identity_missing,
            ),
            position_guidance=validate_plan_prose(
                item.get("position_guidance"), "STEP 10候选仓位", candidate_codes
            ),
            abandon_condition=validate_plan_condition_prose(
                item.get("abandon_condition"),
                "STEP 10候选放弃条件",
                candidate_codes,
                stock_identity_missing,
            ),
        )
        validate_candidate_sector_claims(
            candidate_action,
            candidate_by_code[code],
            known_sector_names,
        )
        validate_candidate_role_claims(candidate_action, candidate_by_code[code])
        actions.append(candidate_action)
    if seen_codes != set(focus_codes):
        raise DetailedReviewError("STEP 10 每个关注代码都必须有且只有一条逐候选计划。")
    return tuple(actions)


# 本地已知板块名只能用于其已绑定候选，避免模型把热门题材错写成个股归属事实。
def validate_candidate_sector_claims(
    action: DetailedCandidateAction,
    candidate: DetailedCandidate,
    known_sector_names: tuple[str, ...],
) -> None:
    owned_sector_names = split_candidate_sector_names(candidate.sector)
    action_prose = "\n".join(
        (
            action.rationale,
            action.trigger,
            action.position_guidance,
            action.abandon_condition,
        )
    )
    mismatched_sector_names = tuple(
        sector_name
        for sector_name in known_sector_names
        if sector_name in action_prose and sector_name not in owned_sector_names
    )
    if mismatched_sector_names:
        raise DetailedReviewError(
            f"STEP 10 候选 {candidate.code} 的板块归属不属于该候选："
            f"{'、'.join(mismatched_sector_names)}。"
        )


# 候选理由只能复述该对象已有角色，不能把连板候选改写成板块领涨候选。
def validate_candidate_role_claims(
    action: DetailedCandidateAction,
    candidate: DetailedCandidate,
) -> None:
    if "板块领涨" not in action.rationale or "板块领涨" in candidate.role:
        return
    if any(
        marker in action.rationale
        for marker in (
            "是否板块领涨",
            "板块领涨待确认",
            "板块领涨角色待确认",
            "不能确认板块领涨",
            "无法确认板块领涨",
        )
    ):
        return
    raise DetailedReviewError(
        f"STEP 10 候选 {candidate.code} 的板块领涨角色不属于该候选。"
    )


# 当前没有用户确认 AI shortlist 身份的结构化字段，因此模型计划始终保持条件观察边界。
def validate_unconfirmed_ai_shortlist_plan(
    participation_decision: str,
    position_guidance: str,
    summary: str,
    participation_condition: str,
    risk_boundary: str,
    candidate_actions: tuple[DetailedCandidateAction, ...],
) -> None:
    if participation_decision == "yes":
        raise DetailedReviewError(
            "核心票身份尚未验证；AI shortlist 未经用户确认，STEP 10 不得给出无条件参与结论。"
        )
    if any(action.action == "buy" for action in candidate_actions):
        raise DetailedReviewError(
            "核心票身份尚未验证；AI shortlist 未经用户确认，STEP 10 不得给出条件买入行动。"
        )
    plan_prose_values = (
        summary,
        participation_condition,
        risk_boundary,
        *(action.rationale for action in candidate_actions),
        *(action.trigger for action in candidate_actions),
        *(action.abandon_condition for action in candidate_actions),
    )
    for prose in plan_prose_values:
        residual = prose
        for marker in sorted(
            FOCUSLESS_NEGATED_EXECUTION_MARKERS + REPORT_ONLY_PARTICIPATION_MARKERS,
            key=len,
            reverse=True,
        ):
            residual = residual.replace(marker, "")
        if any(marker in residual for marker in FOCUSLESS_EXECUTION_MARKERS):
            raise DetailedReviewError(
                "核心票身份尚未验证；STEP 10 的条件观察文本不得夹带参与、执行、买入或建仓指令。"
            )
    position_values = (position_guidance,) + tuple(
        action.position_guidance for action in candidate_actions
    )
    uncapped_position_values = tuple(
        value
        for value in position_values
        if is_executable_position_guidance(value) and not has_explicit_position_cap(value)
    )
    if uncapped_position_values:
        raise DetailedReviewError(
            "核心票身份尚未验证；AI shortlist 未经用户确认，STEP 10 仓位方案必须明确写成上限。"
        )


def has_explicit_position_cap(value: str) -> bool:
    return POSITION_CAP_PATTERN.search(re.sub(r"\s+", "", value)) is not None


# 没有可复核行动对象时，总计划也必须保持不参与和不可执行，避免空对象仍给出仓位。
def validate_plan_without_focus_candidates(
    participation_decision: str,
    position_guidance: str,
    participation_condition: str,
    required_reassessment_markers: tuple[str, ...],
) -> None:
    if participation_decision != "no" or is_executable_position_guidance(position_guidance):
        raise DetailedReviewError(
            "STEP 10 没有行动对象时只能不参与且不设置可执行仓位。"
        )
    condition_without_negative_actions = participation_condition
    for marker in FOCUSLESS_NEGATED_EXECUTION_MARKERS:
        condition_without_negative_actions = condition_without_negative_actions.replace(
            marker, ""
        )
    if any(
        marker in condition_without_negative_actions
        for marker in FOCUSLESS_EXECUTION_MARKERS
    ):
        raise DetailedReviewError(
            "STEP 10 没有行动对象时，重新评估条件不得追加参与、执行、买入或仓位动作。"
        )
    valid_reassessment_clause = next(
        (
            clause
            for sentence in PROSE_SENTENCE_PATTERN.findall(participation_condition)
            for clause in PROSE_CLAUSE_PATTERN.findall(sentence)
            if is_positive_reassessment_clause(
                clause,
                required_reassessment_markers,
            )
        ),
        None,
    )
    if valid_reassessment_clause is None:
        raise DetailedReviewError(
            "STEP 10 重新评估条件必须在同一条件中写明补齐当前阻塞证据后重新评估。"
        )


def is_positive_reassessment_clause(
    clause: str,
    required_reassessment_markers: tuple[str, ...],
) -> bool:
    if NEGATED_REASSESSMENT_PATTERN.search(clause):
        return False
    if NEGATED_REASSESSMENT_ACTION_PATTERN.search(clause):
        return False
    if NEGATED_ACQUIRED_EVIDENCE_PATTERN.search(clause):
        return False
    if STILL_MISSING_EVIDENCE_PATTERN.search(clause):
        return False
    evidence_matches = tuple(
        (clause.find(marker), len(marker))
        for marker in REASSESSMENT_EVIDENCE_MARKERS
        if marker in clause
    )
    action_positions = tuple(
        clause.find(marker)
        for marker in REASSESSMENT_ACTION_MARKERS
        if marker in clause
    )
    required_matches = tuple(
        (clause.find(marker), len(marker)) for marker in required_reassessment_markers
    )
    if not evidence_matches or not action_positions:
        return False
    if any(position < 0 for position, _ in required_matches):
        return False
    action_position = min(action_positions)
    if required_matches:
        first_required_position = min(position for position, _ in required_matches)
        last_required_position, last_required_length = max(required_matches)
        last_required_end = last_required_position + last_required_length
        for evidence_position, evidence_length in evidence_matches:
            evidence_end = evidence_position + evidence_length
            prefix_gap = clause[evidence_end:first_required_position]
            postfix_gap = clause[last_required_end:evidence_position]
            # 证据动作必须紧邻全部阻塞标签，避免“市场证据已齐”误绑定到仍缺失的个股证据。
            evidence_before_labels = bool(
                evidence_position < first_required_position
                and re.fullmatch(r"(?:全部|以下|相关|上述)?(?:的)?", prefix_gap)
            )
            evidence_after_labels = bool(
                evidence_position >= last_required_end
                and re.fullmatch(r"(?:相关)?证据(?:均|已|全部|已经)?", postfix_gap)
            )
            if (
                (evidence_before_labels or evidence_after_labels)
                and action_position > max(evidence_position, last_required_end)
            ):
                return True
        return False
    return any(position < action_position for position, _ in evidence_matches)


def is_executable_position_guidance(value: str) -> bool:
    normalized_value = re.sub(r"\s+", "", value)
    # 非执行短语只保护自身，后续若仍混有仓位数字或动作，必须继续按可执行方案校验。
    for marker in sorted(NON_EXECUTABLE_POSITION_MARKERS, key=len, reverse=True):
        normalized_value = normalized_value.replace(marker, "")
    return bool(
        NUMBER_PATTERN.search(normalized_value)
        or CHINESE_POSITION_PATTERN.search(normalized_value)
        or any(marker in normalized_value for marker in EXECUTABLE_POSITION_MARKERS)
    )


def normalize_model_step_number(value: Any) -> int:
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise DetailedReviewError("详细复盘模型 STEP 编号必须是 1 至 7。")
    if number not in MODEL_STEP_NUMBERS:
        raise DetailedReviewError("详细复盘模型 STEP 编号必须是 1 至 7。")
    return number


def validate_prose_shape(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DetailedReviewError(f"详细复盘{label}不能为空。")
    prose = value.strip()
    if len(prose) > MAX_PROSE_LENGTH:
        raise DetailedReviewError(f"详细复盘{label}超过允许长度。")
    if "#" in prose:
        raise DetailedReviewError(f"详细复盘{label}不得创建 Markdown 标题。")
    return prose


def validate_analysis_prose(value: Any, label: str) -> str:
    prose = validate_prose_shape(value, label)
    sanitized_prose = remove_numeric_prose_sentences(prose)
    if not sanitized_prose:
        raise DetailedReviewError(
            f"详细复盘{label}清理后没有可用定性内容；已移除数字或 STEP 标签。"
        )
    return sanitized_prose


# STEP 8/10 是明确标记的条件计划，允许数字、买卖方向和仓位表达，但仍禁止 Markdown 注入。
def validate_plan_prose(
    value: Any,
    label: str,
    allowed_codes: set[str],
) -> str:
    prose = validate_prose_shape(value, label)
    unknown_codes = set(STOCK_CODE_PATTERN.findall(prose)) - allowed_codes
    if unknown_codes:
        raise DetailedReviewError(
            f"详细复盘{label}包含有限候选范围之外的股票代码："
            f"{'、'.join(sorted(unknown_codes))}。"
        )
    validate_unsupported_daily_kline_prose(prose, label)
    return prose


# 候选没有逐日 K 线时，成交和换手的同义量价表述也不能作为判断或触发条件。
def validate_unsupported_daily_kline_prose(prose: str, label: str) -> None:
    if UNSUPPORTED_DAILY_KLINE_PATTERN.search(prose):
        raise DetailedReviewError(
            f"详细复盘{label}引用了当前证据未提供的候选日 K、均线或量价形态。"
        )


# 两组榜单可能给出同名板块的不同净流入值，凡使用该字段都必须声明榜单口径。
def validate_sector_net_inflow_scope(prose_values: tuple[str, ...], label: str) -> None:
    for prose in prose_values:
        if "净流入" in prose and not any(
            marker in prose for marker in SECTOR_NET_INFLOW_SCOPE_MARKERS
        ):
            raise DetailedReviewError(
                f"详细复盘{label}使用净流入时必须明确热门题材榜、强势行业榜或强势概念榜口径。"
            )


# STEP 8/10 的执行条件必须可复核；模糊词只有与数字阈值或离散事件同句时才可保留。
def validate_plan_condition_prose(
    value: Any,
    label: str,
    allowed_codes: set[str],
    stock_identity_missing: bool,
) -> str:
    prose = validate_plan_prose(value, label, allowed_codes)
    for sentence in PROSE_SENTENCE_PATTERN.findall(prose):
        if stock_identity_missing and any(
            marker in sentence for marker in UNVERIFIED_STOCK_IDENTITY_MARKERS
        ):
            raise DetailedReviewError(
                f"详细复盘{label}在个股身份缺失时不得使用核心票、龙头或中军作为条件。"
            )
        undefined_markers = tuple(
            match.group(0)
            for pattern in UNDEFINED_PLAN_CONDITION_PATTERNS
            if (match := pattern.search(sentence))
        )
        if undefined_markers:
            raise DetailedReviewError(
                f"详细复盘{label}包含未量化的模糊条件：{'、'.join(undefined_markers)}。"
            )
        sentence_for_vague_check = sentence
        for scope_marker in SECTOR_NET_INFLOW_SCOPE_MARKERS:
            sentence_for_vague_check = sentence_for_vague_check.replace(scope_marker, "")
        vague_markers = tuple(
            marker
            for marker in VAGUE_PLAN_CONDITION_MARKERS
            if marker in sentence_for_vague_check
        )
        if vague_markers:
            sentence_without_codes = STOCK_CODE_PATTERN.sub("", sentence)
            has_numeric_threshold = NUMBER_PATTERN.search(sentence_without_codes) is not None
            strictly_quantified_markers = tuple(
                marker
                for marker in STRICTLY_QUANTIFIED_PLAN_CONDITION_MARKERS
                if marker in sentence_for_vague_check
            )
            if strictly_quantified_markers and not has_numeric_threshold:
                raise DetailedReviewError(
                    f"详细复盘{label}包含未量化的模糊条件：{'、'.join(strictly_quantified_markers)}。"
                )
            has_discrete_event = any(
                marker in sentence for marker in DISCRETE_PLAN_CONDITION_MARKERS
            )
            if not has_numeric_threshold and not has_discrete_event:
                raise DetailedReviewError(
                    f"详细复盘{label}包含未量化的模糊条件：{'、'.join(vague_markers)}。"
                )
        validate_group_event_scope(sentence, label)
        for clause in PROSE_CLAUSE_PATTERN.findall(sentence):
            unbounded_market_markers = tuple(
                marker for marker in UNBOUNDED_MARKET_PATH_MARKERS if marker in clause
            )
            if unbounded_market_markers and not MARKET_PATH_PRICE_THRESHOLD_PATTERN.search(
                clause
            ):
                raise DetailedReviewError(
                    f"详细复盘{label}包含未量化的市场路径条件："
                    f"{'、'.join(unbounded_market_markers)}。"
                )
    return prose


# “或”连接的是可独立触发的分支，必须逐支检查，不能让另一支的数字替模糊集合兜底。
def validate_group_event_scope(sentence: str, label: str) -> None:
    for branch in re.split(r"或者|或", sentence):
        for clause in PROSE_CLAUSE_PATTERN.findall(branch):
            if not any(marker in clause for marker in GROUP_EVENT_OBJECT_MARKERS):
                continue
            if not any(marker in clause for marker in GROUP_EVENT_MARKERS):
                continue
            if label.startswith("STEP 10候选") and "候选" in clause and not any(
                marker in clause for marker in ("个股", "盘面")
            ):
                continue
            if has_explicit_group_event_scope(clause):
                continue
            raise DetailedReviewError(
                f"详细复盘{label}的集合事件没有明确对象范围、数量、比例或家数比较。"
            )


def has_explicit_group_event_scope(value: str) -> bool:
    if UNSCOPED_GROUP_EVENT_PATTERN.search(value):
        return False
    value_without_codes = STOCK_CODE_PATTERN.sub("", value)
    return bool(
        NAMED_STOCK_EVENT_PATTERN.search(value)
        or GROUP_QUANTITY_PATTERN.search(value_without_codes)
        or GROUP_SELECTOR_PATTERN.search(value)
        or GROUP_AGGREGATE_COMPARISON_PATTERN.search(value)
    )


# 模型只负责定性解释；含阿拉伯数字、代码或 STEP 标签的整句统一丢弃，避免留下不完整残句。
def remove_numeric_prose_sentences(prose: str) -> str:
    retained_sentences = [
        sentence
        for sentence in PROSE_SENTENCE_PATTERN.findall(prose)
        if not NUMBER_PATTERN.search(sentence) and not STEP_LABEL_PATTERN.search(sentence)
    ]
    return "".join(retained_sentences).strip()


def normalize_unverified_identity_claims(prose: str) -> str:
    normalized = prose
    for unsupported_claim, candidate_claim in UNVERIFIED_IDENTITY_REPLACEMENTS.items():
        normalized = normalized.replace(unsupported_claim, candidate_claim)
    return normalized


# 渲染严格遍历框架 STEP；模型草案经过本地字段、对象和文字边界校验后才能进入待确认区域。
def render_detailed_review(review: DetailedReview) -> str:
    analyses = {item.step_number: item for item in review.analyses}
    steps = {step.number: step for step in review.framework.steps}
    records = {record.step_number: record for record in review.context.step_records}
    lines = [
        f"# {review.review_date} A 股详细复盘",
        "",
        f"- 复盘框架：{review.framework.source_path}",
        f"- Evidence Snapshot：{review.snapshot_reference}",
        f"- 样本日期：{review.context.snapshot.sample_date}",
        f"- 模型草案来源：{DETAILED_MODEL_SOURCE_LABELS[review.model_source]}",
        "- 内容边界：历史事实由本地证据固定渲染；模型判断与仓位方案是 AI 条件计划，不保证市场结果，也不会自动交易或写入业务数据。",
    ]
    for number in MODEL_STEP_NUMBERS:
        lines.extend(render_analysis_step(steps[number], analyses[number], review, records.get(number)))
    lines.extend(render_preview_step(steps[8], review))
    lines.extend(render_pattern_step(steps[9], review))
    lines.extend(render_final_step(steps[10], review))
    return "\n".join(lines).rstrip() + "\n"


def render_analysis_step(
    step: ReviewStep,
    analysis: DetailedStepAnalysis,
    review: DetailedReview,
    manual_record: Any,
) -> list[str]:
    evidence = review.step_evidence[step.number]
    lines = [
        "",
        f"## STEP {step.number}: {step.title}",
        "",
        "### 本步结论",
        "",
        f"- 状态：{evidence.status}。",
        f"- AI 置信度：{analysis.confidence}。",
        f"- 待核查推断：{analysis.conclusion}",
    ]
    if step.number in {6, 7}:
        lines.extend(render_candidate_scope(review, step.number))
    lines.extend(["", "### 证据链", ""])
    for field_name in analysis.evidence_field_keys:
        lines.append(f"- `{field_name}`：{review.evidence_facts[field_name]}")
        lines.extend(render_evidence_reference_lines(review, (field_name,), indent="  "))
    if manual_record:
        lines.append(
            f"- 人工判断对照（不参与模型推断）：{manual_record.judgment}"
        )
        if manual_record.hypothesis:
            lines.append(f"- 人工假设对照：{manual_record.hypothesis}")
        if manual_record.note:
            lines.append(f"- 人工备注：{manual_record.note}")
    lines.extend(
        [
            "",
            "### 证据解释",
            "",
            f"- {analysis.interpretation}",
            "",
            "### 反向证据与硬缺口",
            "",
            f"- 反向证据：{STEP_COUNTER_EVIDENCE[step.number]}",
            f"- 硬缺口：{render_hard_gap_labels(evidence)}",
            "",
            "### 次日验证点",
            "",
            f"- {analysis.next_day_validation}",
        ]
    )
    return lines


def render_evidence_reference_lines(
    review: DetailedReview,
    field_names: tuple[str, ...],
    indent: str = "",
) -> list[str]:
    references = tuple(
        dict.fromkeys(
            reference
            for field_name in field_names
            for reference in review.evidence_references.get(field_name, ())
        )
    )
    fields_without_reference = tuple(
        field_name
        for field_name in field_names
        if not review.evidence_references.get(field_name)
    )
    if fields_without_reference:
        raise DetailedReviewError(
            "详细复盘证据字段缺少实际引用："
            f"{'、'.join(fields_without_reference)}。"
        )
    return [f"{indent}- 证据引用：{reference}" for reference in references]


def render_candidate_scope(review: DetailedReview, step_number: int) -> list[str]:
    market_candidates = [candidate for candidate in review.candidates if "系统发现候选" in candidate.origin]
    real_pool_candidates = [candidate for candidate in review.candidates if "用户真实池" in candidate.origin]
    shortlist_title = (
        "AI 核心备选（证据排序，未确认身份）"
        if step_number == 6
        else "次日重点核验（未确认交易资格）"
    )
    lines = ["", f"### {shortlist_title}", ""]
    if review.ranked_candidates:
        lines.append(
            f"- 排序分母：{len(review.candidates)} 只有限候选；最多展示 {MAX_FOCUS_CANDIDATES} 只，排序不等于核心票身份或买点。"
        )
        for rank, candidate in enumerate(review.ranked_candidates, start=1):
            lines.append(
                f"  - 排名 {rank}：{render_candidate_fact(candidate)}"
            )
    else:
        lines.append("- 当前没有可确认代码的 AI 候选。")
    if step_number == 7:
        return lines
    lines.extend(["", "### 完整有限候选范围", ""])
    if market_candidates:
        lines.append("- 系统发现候选（逐只展示本地事实，不代表核心票）：")
        for candidate in market_candidates:
            lines.append(f"  - {render_candidate_fact(candidate)}")
    else:
        lines.append("- 系统发现候选：当前快照没有可确认代码的市场个股事实。")
    if real_pool_candidates:
        lines.append("- 用户真实池（不自动等同重点关注）：")
        for candidate in real_pool_candidates:
            lines.append(f"  - {render_candidate_fact(candidate)}")
    else:
        lines.append("- 用户真实池：无有效对象。")
    return lines


def render_candidate_fact(candidate: DetailedCandidate) -> str:
    board_fact = f"{candidate.board_count} 板" if candidate.board_count else "无可确认连板高度"
    daily_kline_fact = (
        f"完整 {candidate.daily_bar_count} 根"
        if candidate.daily_bars_complete
        else "不完整/未提供"
    )
    return (
        f"{candidate.code} {candidate.name}｜板块：{candidate.sector}｜角色事实：{candidate.role}｜"
        f"角色来源：{candidate.role_source}｜连板高度：{board_fact}｜"
        f"5 日：{render_optional_percent(candidate.return_5d_percent)}｜"
        f"20 日：{render_optional_percent(candidate.return_20d_percent)}｜"
        f"5 日均成交额：{render_optional_amount(candidate.average_5d_amount)}｜"
        f"流通市值：{render_optional_amount(candidate.circulating_market_value)}｜"
        f"当日换手率：{render_optional_ratio_percent(candidate.turnover)}｜"
        f"最近日 K：{daily_kline_fact}"
    )


def render_optional_percent(value: float | None) -> str:
    return f"{value}%" if value is not None else "待确认"


def render_optional_ratio_percent(value: float | None) -> str:
    return f"{round(value * 100, 4)}%" if value is not None else "待确认"


def render_optional_amount(value: float | None) -> str:
    return f"{value} 元" if value is not None else "待确认"


def render_preview_step(step: ReviewStep, review: DetailedReview) -> list[str]:
    lines = ["", f"## STEP {step.number}: {step.title}", "", "### 本步结论", ""]
    if not review.context.previews:
        lines.extend([
            "- 状态：ai_conditional_plan。",
            f"- 主情景推断：{review.preview_outlook.primary_judgment}",
            f"- 值博率方向：{review.preview_outlook.odds_direction}。该方向只表示相对变化，不表示修复已发生。",
            f"- 主情景证据字段：{'、'.join(review.preview_outlook.evidence_field_keys)}",
            "- 以下四档条件是相对主情景的观察分支和否决线；其中数字是计划阈值，不是已发生事实，也不会自动写入 SQLite。",
            "- 判定顺序：放弃条件 → 不及预期 → 超预期 → 符合预期；命中前一档后不再归入后一档。",
        ])
        lines.extend(
            render_evidence_reference_lines(
                review,
                review.preview_outlook.evidence_field_keys,
            )
        )
        for preview in review.preview_drafts:
            bands_by_scenario = {
                band.scenario: band.condition for band in preview.scenario_bands
            }
            lines.extend(
                [
                    "",
                    f"### {preview.target}",
                    "",
                    f"- 固定对象：{preview.scope.object_name}",
                    f"- 固定数据源：{preview.scope.source_name}",
                    f"- 固定主指标：{preview.scope.metric_name}",
                    f"- 候选集分母：{preview.scope.denominator} 只"
                    if preview.scope.denominator is not None
                    else "- 候选集分母：不适用",
                    f"- 可用字段：{'、'.join(preview.scope.available_fields) or '无'}",
                    f"- 符合预期（本地主分档）：{bands_by_scenario['expectation']}",
                    f"- 超预期（本地主分档）：{bands_by_scenario['over_expectation']}",
                    f"- 不及预期（本地主分档）：{bands_by_scenario['under_expectation']}",
                    f"- 放弃条件（本地主分档）：{bands_by_scenario['abandon_condition']}",
                    f"- 证据字段：{'、'.join(preview.evidence_field_keys)}",
                ]
            )
            lines.extend(
                render_evidence_reference_lines(
                    review,
                    preview.evidence_field_keys,
                )
            )
        return lines
    lines.append("- 状态：confirmed_by_user。以下内容来自已保存的用户确认记录。")
    for preview in review.context.previews:
        lines.extend(
            [
                "",
                f"### {preview.target}",
                "",
                f"- 预演 ID：{preview.preview_id}",
                f"- 符合预期：{preview.expectation}",
                f"- 超预期：{preview.over_expectation}",
                f"- 不及预期：{preview.under_expectation}",
                f"- 放弃条件：{preview.abandon_condition}",
                f"- 证据引用：{preview.evidence_reference}",
            ]
        )
    return lines


def render_pattern_step(step: ReviewStep, review: DetailedReview) -> list[str]:
    priority_numbers, selection_reason = select_priority_pattern_numbers(review)
    priority_number_set = set(priority_numbers)
    candidate_by_code = {candidate.code: candidate for candidate in review.candidates}
    lines = [
        "",
        f"## STEP {step.number}: {step.title}",
        "",
        "- 使用边界：完整逐项核验十二种模式；缺少竞价、分时或日线形态时不确认模式和买点。",
        f"- 当日优先级依据：{selection_reason}",
    ]
    for check in sorted(review.step9_checks, key=lambda item: item.pattern_number):
        available = "、".join(STEP9_EVIDENCE_LABELS[name] for name in check.available_evidence) or "无"
        missing = "、".join(STEP9_EVIDENCE_LABELS[name] for name in check.missing_evidence) or "无"
        bound_candidates = "、".join(
            f"{code} {candidate_by_code[code].name}"
            for code in check.candidate_codes
            if code in candidate_by_code
        ) or "无具体候选"
        lines.extend(
            [
                "",
                f"### 模式 {check.pattern_number}：{check.name}",
                "",
                f"- 当日优先级：{'优先核验' if check.pattern_number in priority_number_set else '常规核验'}",
                f"- 当前核验状态：{STEP9_STATUS_LABELS[check.status]}（{check.status}）",
                f"- 绑定候选：{bound_candidates}",
                f"- 已有证据：{available}",
                f"- 次日需要补看的触发事实：{missing}",
                f"- 失效边界：{check.invalidation_boundary}",
            ]
        )
    return lines


def select_priority_pattern_numbers(review: DetailedReview) -> tuple[tuple[int, ...], str]:
    eligible_checks = tuple(
        check
        for check in review.step9_checks
        if check.status in {"background_matched_trigger_pending", "confirmed"}
    )
    eligible_numbers = {check.pattern_number for check in eligible_checks}
    if not eligible_checks:
        return (), "当前没有背景已匹配的模式，不设置优先项。"
    sentiment = review.context.snapshot.sentiment
    limit_up_count = sentiment.get("limit_up_count")
    limit_down_count = sentiment.get("limit_down_count")
    if isinstance(limit_up_count, (int, float)) and isinstance(limit_down_count, (int, float)):
        if limit_down_count > limit_up_count:
            weak_sentiment_numbers = tuple(
                number for number in (2, 6, 12) if number in eligible_numbers
            )
            remaining_numbers = tuple(
                number
                for number in sorted(eligible_numbers)
                if number not in weak_sentiment_numbers
            )
            priority_numbers = (*weak_sentiment_numbers, *remaining_numbers)[:3]
            return priority_numbers, "跌停数高于涨停数，仅从当前可核验项中优先检查退潮转折、弱情绪抗跌和情绪指数共振模式。"
    ordered = sorted(eligible_checks, key=lambda check: check.pattern_number)
    return tuple(check.pattern_number for check in ordered[:3]), "仅从当前背景已匹配的模式中设置优先项。"


def render_final_step(step: ReviewStep, review: DetailedReview) -> list[str]:
    lines = ["", f"## STEP {step.number}: {step.title}", "", "### 最终应对", ""]
    if not review.final_responses:
        draft = review.final_plan_draft
        candidate_by_code = {candidate.code: candidate for candidate in review.candidates}
        focus_text = "、".join(
            f"{code} {candidate_by_code[code].name}" for code in draft.focus_codes
        ) or "无"
        condition_label = "执行条件" if draft.focus_codes else "重新评估条件"
        plan_intro = (
            "- 以下给出 AI 条件行动方案；风险由用户自行承担，但历史事实仍必须来自本地证据，且不会自动交易或写入计划、池子、Observation 或 SQLite。"
            if draft.focus_codes
            else "- 以下给出 AI 证据重评边界；当前没有可复核行动对象，不构成参与、仓位或逐候选行动方案。"
        )
        lines.extend([
            "- 状态：ai_conditional_plan。",
            plan_intro,
            f"- 次日能否参与：{PARTICIPATION_DECISION_LABELS[draft.participation_decision]}。",
            f"- 建议总仓位：{draft.position_guidance}",
            f"- 次日总纲：{draft.summary}",
            f"- 行动对象：{focus_text}",
            f"- {condition_label}：{draft.participation_condition}",
            f"- 风险与放弃边界：{draft.risk_boundary}",
            f"- 证据字段：{'、'.join(draft.evidence_field_keys)}",
            "- 执行边界：计划参数不等于市场事实，只有对应触发实际出现时才成立。",
        ])
        if not draft.focus_codes:
            lines.append("- 重评边界：该条件只触发重新核查，不代表恢复参与或设置仓位。")
        lines.extend(render_evidence_reference_lines(review, draft.evidence_field_keys))
        lines.extend(["", "### 逐候选行动", ""])
        if not draft.candidate_actions:
            lines.append("- 无可复核的逐候选行动。")
        for action in draft.candidate_actions:
            candidate = candidate_by_code[action.code]
            lines.extend(
                [
                    f"- {candidate.code} {candidate.name}｜{CANDIDATE_ACTION_LABELS[action.action]}｜仓位：{action.position_guidance}",
                    f"  - 理由：{action.rationale}",
                    f"  - 触发：{action.trigger}",
                    f"  - 放弃：{action.abandon_condition}",
                ]
            )
        return lines
    lines.append("- 状态：confirmed_by_user。以下内容来自已保存的用户确认记录。")
    for response in review.final_responses:
        lines.extend(
            [
                "",
                f"### {response.final_id}",
                "",
                f"- 最终应对：{response.response}",
                f"- 风险边界：{response.risk_boundary}",
                f"- 关联真实池：{'、'.join(response.pool_codes)}",
                f"- 关联预演：{'、'.join(response.preview_ids)}",
                f"- 关联 Observation：{'、'.join(response.observation_ids)}",
                f"- 计划项：{response.plan_item}",
                f"- 证据引用：{response.evidence_reference}",
            ]
        )
    return lines


def render_hard_gap_labels(evidence: StepEvidence) -> str:
    return "、".join(HARD_GAP_LABELS.get(name, name) for name in evidence.hard_missing_fields) or "无"


def write_detailed_review_log(
    review: DetailedReview,
    framework_path: Path,
    output_path: Path,
    log_path: Path,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.detailed_review")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=review create-detailed trade_date=%s framework=%s snapshot=%s output=%s step_count=%s model_source=%s status=created",
            review.review_date,
            framework_path,
            review.snapshot_reference,
            output_path,
            len(review.framework.steps),
            review.model_source,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
