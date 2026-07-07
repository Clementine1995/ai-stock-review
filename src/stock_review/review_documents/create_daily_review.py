# 本文件负责生成每日复盘 Markdown 文件，并记录本地写操作日志。

from __future__ import annotations

from datetime import date
import logging
from pathlib import Path

from stock_review.evidence.evidence_snapshot import EvidenceSnapshot, build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import read_json_mapping
from stock_review.reports.render_markdown import render_daily_review
from stock_review.review_framework.parse_framework import parse_framework_file


DEFAULT_DAILY_REPORT_DIR = Path("reports") / "daily"
DEFAULT_LOG_PATH = Path("logs") / "stock_review.log"


# 日期必须明确到交易日，避免把复盘文件写到模糊或不可追踪的路径。
def create_daily_review(
    trade_date: str,
    framework_path: Path,
    evidence_path: Path | None = None,
    output_dir: Path = DEFAULT_DAILY_REPORT_DIR,
    log_path: Path = DEFAULT_LOG_PATH,
) -> Path:
    parsed_date = date.fromisoformat(trade_date)
    framework = parse_framework_file(framework_path)
    evidence_snapshot = load_evidence_snapshot(parsed_date.isoformat(), evidence_path)
    content = render_daily_review(parsed_date.isoformat(), framework, evidence_snapshot)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{parsed_date.isoformat()}_review.md"
    output_path.write_text(content, encoding="utf-8")

    write_review_log(
        parsed_date.isoformat(),
        framework_path,
        output_path,
        len(framework.steps),
        log_path,
        evidence_path=evidence_path,
    )
    return output_path


# 日报生成只读取显式传入的快照文件，不隐式扫描数据目录，避免使用错误日期的证据。
def load_evidence_snapshot(trade_date: str, evidence_path: Path | None) -> EvidenceSnapshot | None:
    if evidence_path is None:
        return None
    snapshot_data = read_json_mapping(evidence_path)
    return build_evidence_snapshot(trade_date, snapshot_data)


# 正式 CLI 写操作需要留下本地证据，记录命令核心对象和输出结果。
def write_review_log(
    trade_date: str,
    framework_path: Path,
    output_path: Path,
    step_count: int,
    log_path: Path = DEFAULT_LOG_PATH,
    evidence_path: Path | None = None,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.review")
    logger.setLevel(logging.INFO)

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=review create trade_date=%s framework=%s evidence=%s output=%s step_count=%s status=created",
            trade_date,
            framework_path,
            evidence_path or "",
            output_path,
            step_count,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
