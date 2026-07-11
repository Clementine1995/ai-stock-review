# 本文件负责生成周度学习 Markdown，只归纳已回填 Observation，不自动修改复盘规则。

from __future__ import annotations

from collections import Counter
from datetime import date
import logging
from pathlib import Path

from stock_review.observations.manage_observation import (
    DEFAULT_DATABASE_PATH,
    Observation,
    list_observations_between,
)


DEFAULT_WEEKLY_REPORT_DIR = Path("reports") / "weekly"
DEFAULT_LOG_PATH = Path("logs") / "stock_review.log"


# 周度总结按显式日期范围读取 Observation，输出可追溯样本和候选，不使用 LLM 推断。
def create_weekly_learning(
    start_date: str,
    end_date: str,
    database_path: Path = DEFAULT_DATABASE_PATH,
    output_dir: Path = DEFAULT_WEEKLY_REPORT_DIR,
    log_path: Path = DEFAULT_LOG_PATH,
) -> Path:
    normalized_start = date.fromisoformat(start_date).isoformat()
    normalized_end = date.fromisoformat(end_date).isoformat()
    observations = list_observations_between(normalized_start, normalized_end, database_path)
    content = render_weekly_learning(normalized_start, normalized_end, observations)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{normalized_start}_to_{normalized_end}_learning.md"
    output_path.write_text(content, encoding="utf-8")
    write_learning_log(normalized_start, normalized_end, output_path, len(observations), log_path)
    return output_path


# 报告只把 hit/miss 作为经验候选，invalid 和 pending 分区展示但不参与候选。
def render_weekly_learning(
    start_date: str,
    end_date: str,
    observations: list[Observation],
) -> str:
    hit_items = [item for item in observations if item.status == "hit"]
    miss_items = [item for item in observations if item.status == "miss"]
    invalid_items = [item for item in observations if item.status == "invalid"]
    pending_items = [item for item in observations if item.status == "pending"]
    reviewed_items = [*hit_items, *miss_items, *invalid_items]

    lines = [
        f"# {start_date} 至 {end_date} 周度学习总结",
        "",
        f"- 开始日期：{start_date}",
        f"- 结束日期：{end_date}",
        f"- Observation 总数：{len(observations)}",
        f"- 已回填样本：{len(reviewed_items)}",
        f"- 命中：{len(hit_items)}；失败：{len(miss_items)}；无效：{len(invalid_items)}；待观察：{len(pending_items)}。",
        "- 总结边界：仅基于已保存 Observation 和回填事实，不自动修改 stock-review.md。",
        "",
    ]

    if not hit_items and not miss_items:
        lines.extend(
            [
                "## 样本状态",
                "",
                "- 有效回填样本不足：当前没有可进入经验候选的 hit 或 miss。",
                "",
            ]
        )

    lines.extend(render_sample_section("命中样本", hit_items))
    lines.extend(render_sample_section("失败样本", miss_items))
    lines.extend(render_sample_section("无效样本", invalid_items))
    lines.extend(render_sample_section("待观察样本", pending_items))
    lines.extend(render_pattern_section("有效判断模式", hit_items))
    lines.extend(render_pattern_section("反复误判主题", miss_items))
    lines.extend(render_candidate_section(hit_items, miss_items))
    return "\n".join(lines).rstrip() + "\n"


def render_sample_section(title: str, observations: list[Observation]) -> list[str]:
    lines = [f"## {title}", ""]
    if not observations:
        return [*lines, "- 无。", ""]
    for observation in observations:
        result = observation.actual_result or "未填写"
        note = observation.review_note or "无"
        lines.append(
            f"- {observation.observation_id}｜{observation.topic}｜假设：{observation.hypothesis}｜"
            f"实际结果：{result}｜复盘备注：{note}｜证据：{observation.evidence_source}"
        )
    return [*lines, ""]


# 模式统计只计算同状态主题出现次数，不把单个样本包装成因果规律。
def render_pattern_section(title: str, observations: list[Observation]) -> list[str]:
    lines = [f"## {title}", ""]
    if not observations:
        return [*lines, "- 样本不足，暂不归纳。", ""]
    topic_counts = Counter(observation.topic for observation in observations)
    for topic, count in sorted(topic_counts.items()):
        lines.append(f"- {topic}：{count} 个样本。")
    return [*lines, ""]


def render_candidate_section(
    hit_items: list[Observation],
    miss_items: list[Observation],
) -> list[str]:
    lines = ["## 经验候选", ""]
    candidates = [*hit_items, *miss_items]
    if not candidates:
        return [*lines, "- 无有效候选。", ""]
    for observation in candidates:
        lines.append(
            f"- [{observation.status}] {observation.topic}：{observation.hypothesis}；"
            f"成立条件：{observation.confirmation_condition}；失效条件：{observation.invalidation_condition}；"
            f"证据：{observation.evidence_source}；样本：{observation.observation_id}。"
        )
    return [*lines, ""]


def write_learning_log(
    start_date: str,
    end_date: str,
    output_path: Path,
    observation_count: int,
    log_path: Path,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.learning")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=learning weekly start_date=%s end_date=%s output=%s observation_count=%s status=created",
            start_date,
            end_date,
            output_path,
            observation_count,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
