# 本文件负责生成次日观察计划 Markdown，只输出观察和应对条件，不生成买卖指令。

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
from pathlib import Path

from stock_review.evidence.evidence_snapshot import EvidenceSnapshot, build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import read_json_mapping
from stock_review.pools.manage_pool_item import DEFAULT_DATABASE_PATH, PoolItem, list_pool_items


DEFAULT_DAILY_REPORT_DIR = Path("reports") / "daily"
DEFAULT_LOG_PATH = Path("logs") / "stock_review.log"


class TradePlanError(ValueError):
    pass


@dataclass(frozen=True)
class TradePlanInput:
    trade_date: str
    review_path: Path
    evidence_snapshot: EvidenceSnapshot | None
    pool_items: tuple[PoolItem, ...]


# 计划生成必须基于已存在的复盘文档，避免脱离当日复盘凭空生成观察对象。
def create_trade_plan(
    trade_date: str,
    review_path: Path | None = None,
    evidence_path: Path | None = None,
    database_path: Path = DEFAULT_DATABASE_PATH,
    output_dir: Path = DEFAULT_DAILY_REPORT_DIR,
    log_path: Path = DEFAULT_LOG_PATH,
) -> Path:
    parsed_date = date.fromisoformat(trade_date)
    normalized_date = parsed_date.isoformat()
    resolved_review_path = review_path or output_dir / f"{normalized_date}_review.md"
    if not resolved_review_path.exists():
        raise TradePlanError(f"未找到每日复盘文档：{resolved_review_path}")

    plan_input = TradePlanInput(
        trade_date=normalized_date,
        review_path=resolved_review_path,
        evidence_snapshot=load_plan_evidence(normalized_date, evidence_path),
        pool_items=tuple(list_pool_items(record_kind="real", database_path=database_path)),
    )
    content = render_trade_plan(plan_input)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{normalized_date}_plan.md"
    output_path.write_text(content, encoding="utf-8")
    write_plan_log(normalized_date, resolved_review_path, evidence_path, output_path, len(plan_input.pool_items), log_path)
    return output_path


def load_plan_evidence(trade_date: str, evidence_path: Path | None) -> EvidenceSnapshot | None:
    if evidence_path is None:
        return None
    snapshot_data = read_json_mapping(evidence_path)
    return build_evidence_snapshot(trade_date, snapshot_data)


# 输出固定强调观察计划属性，防止被误读为自动交易或买卖建议。
def render_trade_plan(plan_input: TradePlanInput) -> str:
    evidence_status = (
        f"已接入 Evidence Snapshot，来源：{plan_input.evidence_snapshot.source}，样本日期：{plan_input.evidence_snapshot.sample_date}。"
        if plan_input.evidence_snapshot
        else "未接入 Evidence Snapshot，计划项证据来源标记为待确认。"
    )
    lines = [
        f"# {plan_input.trade_date} 次日观察计划",
        "",
        f"- 交易日期：{plan_input.trade_date}",
        f"- 关联复盘：{plan_input.review_path}",
        f"- 证据状态：{evidence_status}",
        "- 计划边界：本文件只记录观察条件和应对框架，不是买卖指令。",
        "",
        "## 观察对象",
        "",
    ]

    if not plan_input.pool_items:
        lines.extend(
            [
                "- 暂无池子记录：请先使用 `pool add-watch` 或 `pool add-hot` 手工加入观察对象。",
                "",
            ]
        )
    else:
        for index, item in enumerate(plan_input.pool_items, start=1):
            lines.extend(render_plan_item(index, item, plan_input.evidence_snapshot))

    return "\n".join(lines).rstrip() + "\n"


def render_plan_item(index: int, item: PoolItem, evidence_snapshot: EvidenceSnapshot | None) -> list[str]:
    evidence_source = (
        f"{evidence_snapshot.source} / {evidence_snapshot.sample_date}"
        if evidence_snapshot
        else "待确认"
    )
    return [
        f"### 计划项 {index}: {item.code} {item.name}",
        "",
        f"- 池子类型：{pool_label(item.pool_type)}",
        f"- 记录类型：{item.record_kind}",
        f"- 交易所：{item.exchange}",
        f"- 板块：{'、'.join(item.sectors)}",
        f"- 进入原因：{item.reason or '无'}",
        f"- 证据来源：{evidence_source}",
        "",
        "#### 符合预期",
        "",
        "- 观察条件待人工填写：需与当日复盘结论、板块反馈和个股表现一致。",
        "",
        "#### 超预期",
        "",
        "- 观察条件待人工填写：需出现强于当日复盘设定的板块或个股反馈，并补充对应证据。",
        "",
        "#### 不及预期",
        "",
        "- 观察条件待人工填写：若板块或个股反馈弱于复盘设定，需要记录具体证据。",
        "",
        "#### 放弃条件",
        "",
        "- 放弃条件待人工填写：若证据缺失、股票身份/板块归属无法确认，或触发人工设定的失效条件，则放弃观察。",
        "",
    ]


def pool_label(pool_type: str) -> str:
    return "关注池" if pool_type == "watch" else "热点池"


def write_plan_log(
    trade_date: str,
    review_path: Path,
    evidence_path: Path | None,
    output_path: Path,
    item_count: int,
    log_path: Path,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.plan")
    logger.setLevel(logging.INFO)

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=plan create trade_date=%s review=%s evidence=%s output=%s item_count=%s status=created",
            trade_date,
            review_path,
            evidence_path or "",
            output_path,
            item_count,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
