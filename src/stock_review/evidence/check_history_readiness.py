# 本文件负责检查本地 hhxg 快照的历史积累是否满足分析门槛，不联网、不写入快照或数据库。

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import EvidenceSnapshotError, read_json_mapping


MINIMUM_HHXG_HISTORY_DAYS = 5
HHXG_UNAVAILABLE_FIELDS = ("指数", "成交额", "可确认股票代码")


@dataclass(frozen=True)
class HhxgReadinessDay:
    trade_date: str
    missing_fields: tuple[str, ...]


@dataclass(frozen=True)
class HhxgHistoryReadiness:
    start_date: str
    end_date: str
    valid_days: tuple[HhxgReadinessDay, ...]
    snapshot_dates_without_valid_hhxg: tuple[str, ...]

    @property
    def remaining_days(self) -> int:
        return max(0, MINIMUM_HHXG_HISTORY_DAYS - len(self.valid_days))

    @property
    def is_ready(self) -> bool:
        return self.remaining_days == 0


# 有效 hhxg 日只认定返回日期已核验且事件来源明确的本地快照，禁止用合并快照或历史回填凑数。
def check_hhxg_history_readiness(
    start_date: str,
    end_date: str,
    snapshot_dir: Path,
) -> HhxgHistoryReadiness:
    normalized_start, normalized_end = parse_date_range(start_date, end_date)
    if not snapshot_dir.exists():
        raise EvidenceSnapshotError(f"证据快照目录不存在：{snapshot_dir}")

    valid_days: list[HhxgReadinessDay] = []
    snapshot_dates_without_valid_hhxg: list[str] = []
    for snapshot_path in sorted(snapshot_dir.glob("*_snapshot.json")):
        raw_data = read_json_mapping(snapshot_path)
        trade_date = str(raw_data.get("trade_date") or snapshot_path.name.removesuffix("_snapshot.json"))
        try:
            parsed_trade_date = date.fromisoformat(trade_date)
        except ValueError as error:
            raise EvidenceSnapshotError(f"证据快照交易日期格式无效：{snapshot_path}：{trade_date}") from error
        if not normalized_start <= parsed_trade_date <= normalized_end:
            continue

        snapshot = build_evidence_snapshot(trade_date, raw_data)
        if snapshot.sample_date == trade_date and has_hhxg_event(snapshot.events):
            valid_days.append(HhxgReadinessDay(trade_date, snapshot.missing_fields))
        else:
            snapshot_dates_without_valid_hhxg.append(trade_date)

    return HhxgHistoryReadiness(
        start_date=normalized_start.isoformat(),
        end_date=normalized_end.isoformat(),
        valid_days=tuple(valid_days),
        snapshot_dates_without_valid_hhxg=tuple(snapshot_dates_without_valid_hhxg),
    )


# 仅将来源字段为 hhxg 的事件视作已成功采集，顶层 source 会受同日合并顺序影响，不能单独作为依据。
def has_hhxg_event(events: list[dict[str, object]]) -> bool:
    return any(str(event.get("source") or "") == "hhxg" for event in events)


def render_hhxg_history_readiness(readiness: HhxgHistoryReadiness) -> str:
    lines = [
        f"# {readiness.start_date} 至 {readiness.end_date} hhxg 历史就绪检查",
        "",
        f"- 已验证有效交易日：{len(readiness.valid_days)} / {MINIMUM_HHXG_HISTORY_DAYS}",
        "- 有效条件：本地快照含 hhxg 来源事件，且交易日期与样本日期一致。",
        "- hhxg 固有缺口：" + "、".join(HHXG_UNAVAILABLE_FIELDS) + "；需要其它来源或人工确认补齐。",
        "",
        "## 有效 hhxg 快照与缺口",
        "",
    ]
    if readiness.valid_days:
        for day in readiness.valid_days:
            missing_text = "无" if not day.missing_fields else "、".join(day.missing_fields)
            lines.append(f"- {day.trade_date}：合并快照缺口：{missing_text}。")
    else:
        lines.append("- 当前范围内没有已验证的 hhxg 快照。")

    lines.extend(["", "## 本地快照但未计入 hhxg 历史", ""])
    if readiness.snapshot_dates_without_valid_hhxg:
        lines.extend(f"- {trade_date}：没有满足 hhxg 有效条件的快照。" for trade_date in readiness.snapshot_dates_without_valid_hhxg)
    else:
        lines.append("- 无。")

    lines.extend(["", "## 历史分析状态", ""])
    if readiness.is_ready:
        lines.append("- 已满足 5 个有效交易日要求；可按其它历史报告的证据边界进行分析。")
    else:
        lines.append(
            f"- 未启用：还需连续采集 {readiness.remaining_days} 个有效交易日；不得据此输出历史分析结论。"
        )
    return "\n".join(lines) + "\n"


def parse_date_range(start_date: str, end_date: str) -> tuple[date, date]:
    try:
        normalized_start = date.fromisoformat(start_date)
        normalized_end = date.fromisoformat(end_date)
    except ValueError as error:
        raise EvidenceSnapshotError("hhxg 历史日期格式必须为 YYYY-MM-DD。") from error
    if normalized_start > normalized_end:
        raise EvidenceSnapshotError("hhxg 历史开始日期不能晚于结束日期。")
    return normalized_start, normalized_end
