# 本文件负责按交易日期汇总复盘上下文，为后续人工确认或 LLM 草案提供只读、可追溯输入。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stock_review.evidence.evidence_snapshot import EvidenceSnapshot
from stock_review.evidence.manage_evidence_snapshot import DEFAULT_EVIDENCE_DIR, EvidenceSnapshotError, check_evidence_snapshot
from stock_review.pools.manage_pool_item import DEFAULT_DATABASE_PATH, PoolItem, list_pool_items
from stock_review.review_documents.manage_manual_review import (
    ManualPreview,
    ManualStepRecord,
    list_manual_review_records,
)


@dataclass(frozen=True)
class ReviewContext:
    review_date: str
    snapshot: EvidenceSnapshot
    step_records: tuple[ManualStepRecord, ...]
    previews: tuple[ManualPreview, ...]
    active_real_pool_items: tuple[PoolItem, ...]
    missing_step_numbers: tuple[int, ...]

    @property
    def focus_input_status(self) -> str:
        if not self.active_real_pool_items:
            return "暂无有效真实池对象；不形成重点关注候选。"
        if not self.previews:
            return "缺少 STEP 8 用户确认预演；等待补充条件。"
        return "输入已齐备，仍需用户确认最终重点关注。"


# 上下文只读取指定日期的本地快照、人工记录和真实池，不从样例池或外部数据源补充对象。
def build_review_context(
    review_date: str,
    snapshot_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> ReviewContext:
    snapshot = check_evidence_snapshot(review_date, snapshot_dir=snapshot_dir)
    if snapshot.sample_date != review_date:
        raise EvidenceSnapshotError(
            f"Evidence Snapshot 样本日期与复盘日期不一致：复盘日期 {review_date}，样本日期 {snapshot.sample_date}。"
        )
    step_records, previews = list_manual_review_records(review_date, database_path=database_path)
    active_real_pool_items = tuple(
        item
        for item in list_pool_items(record_kind="real", database_path=database_path)
        if item.status == "active"
    )
    recorded_steps = {record.step_number for record in step_records}
    return ReviewContext(
        review_date=review_date,
        snapshot=snapshot,
        step_records=tuple(step_records),
        previews=tuple(previews),
        active_real_pool_items=active_real_pool_items,
        missing_step_numbers=tuple(step for step in range(1, 8) if step not in recorded_steps),
    )


# 输出只呈现已有输入和待补内容，不能将空池、缺口或单日事实转化为市场或交易结论。
def render_review_context(context: ReviewContext) -> str:
    lines = [
        f"# {context.review_date} 复盘上下文",
        "",
        f"- Evidence Snapshot：{context.snapshot.source}｜样本日期：{context.snapshot.sample_date}",
        f"- Evidence Snapshot 标准缺口：{', '.join(context.snapshot.missing_fields) or '无'}",
        "- 使用边界：本上下文只汇总本地事实与人工记录，不生成市场判断、重点关注、交易计划或 Observation。",
        "",
        "## STEP 1-7 人工判断",
        "",
    ]
    if context.step_records:
        for record in context.step_records:
            lines.append(
                f"- STEP {record.step_number}｜{record.record_id}｜证据引用：{record.evidence_reference}｜判断：{record.judgment}"
            )
    else:
        lines.append("- 暂无人工 STEP 判断。")
    lines.append(f"- 缺少人工判断的 STEP：{', '.join(str(step) for step in context.missing_step_numbers) or '无'}。")

    lines.extend(["", "## STEP 8 用户确认预演", ""])
    if context.previews:
        for preview in context.previews:
            lines.append(
                f"- {preview.preview_id}｜对象：{preview.target}｜证据引用：{preview.evidence_reference}｜"
                f"符合预期：{preview.expectation}｜超预期：{preview.over_expectation}｜"
                f"不及预期：{preview.under_expectation}｜放弃条件：{preview.abandon_condition}"
            )
    else:
        lines.append("- 暂无用户确认的 STEP 8 预演。")

    lines.extend(["", "## 有效真实池对象", ""])
    if context.active_real_pool_items:
        for item in context.active_real_pool_items:
            lines.append(
                f"- {item.pool_type}｜{item.code} {item.name}｜板块：{'、'.join(item.sectors)}｜进入原因：{item.reason or '待确认'}"
            )
    else:
        lines.append("- 对象数：0。暂无有效真实池对象；不从样例池或全市场补充候选。")

    lines.extend(["", "## 重点关注输入状态", "", f"- {context.focus_input_status}"])
    return "\n".join(lines).rstrip() + "\n"
