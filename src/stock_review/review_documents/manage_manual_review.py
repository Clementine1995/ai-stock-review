# 本文件负责保存人工 STEP 判断和 STEP 8 预演条件，不自动生成计划、Observation 或交易结论。

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import logging
from pathlib import Path
import sqlite3


DEFAULT_DATABASE_PATH = Path("data") / "stock_review.sqlite"
DEFAULT_LOG_PATH = Path("logs") / "stock_review.log"


class ManualReviewError(ValueError):
    pass


@dataclass(frozen=True)
class ManualStepRecord:
    record_id: str
    review_date: str
    step_number: int
    judgment: str
    hypothesis: str
    evidence_reference: str
    note: str
    created_at: str


@dataclass(frozen=True)
class ManualPreview:
    preview_id: str
    review_date: str
    target: str
    expectation: str
    over_expectation: str
    under_expectation: str
    abandon_condition: str
    evidence_reference: str
    created_at: str


# STEP 判断由用户显式输入；每个交易日每个 STEP 只保存一条当前人工判断，避免静默合并不同结论。
def add_manual_step_record(
    review_date: str,
    step_number: int,
    judgment: str,
    evidence_reference: str,
    hypothesis: str = "",
    note: str = "",
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> ManualStepRecord:
    normalized_date = normalize_review_date(review_date)
    if step_number < 1:
        raise ManualReviewError("STEP 编号必须大于等于 1。")
    validate_required_fields({"人工判断": judgment, "证据引用": evidence_reference})

    from stock_review.storage.sqlite_repository import ManualReviewRepository

    repository = ManualReviewRepository(database_path)
    record = ManualStepRecord(
        record_id=repository.next_step_record_id(normalized_date, step_number),
        review_date=normalized_date,
        step_number=step_number,
        judgment=judgment.strip(),
        hypothesis=hypothesis.strip(),
        evidence_reference=evidence_reference.strip(),
        note=note.strip(),
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    try:
        repository.add_step_record(record)
    except sqlite3.IntegrityError as error:
        raise ManualReviewError(f"{normalized_date} 的 STEP {step_number} 已有人工判断。") from error
    return record


# STEP 8 预演必须由用户确认，并同时保存四类条件和证据引用，避免模板内容被当作真实计划。
def add_manual_preview(
    review_date: str,
    target: str,
    expectation: str,
    over_expectation: str,
    under_expectation: str,
    abandon_condition: str,
    evidence_reference: str,
    user_confirmed: bool,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> ManualPreview:
    normalized_date = normalize_review_date(review_date)
    validate_required_fields(
        {
            "预演对象": target,
            "符合预期条件": expectation,
            "超预期条件": over_expectation,
            "不及预期条件": under_expectation,
            "放弃条件": abandon_condition,
            "证据引用": evidence_reference,
        }
    )
    if not user_confirmed:
        raise ManualReviewError("STEP 8 预演必须由用户显式确认。")

    from stock_review.storage.sqlite_repository import ManualReviewRepository

    repository = ManualReviewRepository(database_path)
    preview = ManualPreview(
        preview_id=repository.next_preview_id(normalized_date),
        review_date=normalized_date,
        target=target.strip(),
        expectation=expectation.strip(),
        over_expectation=over_expectation.strip(),
        under_expectation=under_expectation.strip(),
        abandon_condition=abandon_condition.strip(),
        evidence_reference=evidence_reference.strip(),
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    try:
        repository.add_preview(preview)
    except sqlite3.IntegrityError as error:
        raise ManualReviewError(f"{normalized_date} 的预演对象已存在：{target.strip()}。") from error
    return preview


# 查询只返回用户已保存的记录，供后续计划与 Observation 显式关联；本函数不创建任何业务记录。
def list_manual_review_records(
    review_date: str,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> tuple[list[ManualStepRecord], list[ManualPreview]]:
    normalized_date = normalize_review_date(review_date)
    from stock_review.storage.sqlite_repository import ManualReviewRepository

    repository = ManualReviewRepository(database_path)
    return repository.list_step_records(normalized_date), repository.list_previews(normalized_date)


def normalize_review_date(review_date: str) -> str:
    try:
        return date.fromisoformat(review_date).isoformat()
    except ValueError as error:
        raise ManualReviewError("复盘日期格式必须为 YYYY-MM-DD。") from error


def validate_required_fields(values: dict[str, str]) -> None:
    for label, value in values.items():
        if not value.strip():
            raise ManualReviewError(f"{label}不能为空。")


# 正式 CLI 写操作记录必要审计字段，供用户按复盘日期、证据引用和记录 ID 回查。
def write_manual_review_log(
    command: str,
    review_date: str,
    record_id: str,
    evidence_reference: str,
    database_path: Path,
    log_path: Path = DEFAULT_LOG_PATH,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.manual_review")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=%s review_date=%s record_id=%s evidence_reference=%s database=%s status=created",
            command,
            review_date,
            record_id,
            evidence_reference,
            database_path,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
