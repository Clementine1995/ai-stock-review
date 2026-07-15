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


@dataclass(frozen=True)
class ManualFinalResponse:
    final_id: str
    review_date: str
    response: str
    risk_boundary: str
    evidence_reference: str
    plan_reference: str
    plan_item: str
    pool_codes: tuple[str, ...]
    preview_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
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


# STEP 10 只能由用户确认后保存，并显式关联真实池、已确认预演、计划项和同日 Observation。
def add_manual_final_response(
    review_date: str,
    response: str,
    risk_boundary: str,
    evidence_reference: str,
    plan_reference: Path,
    plan_item: str,
    pool_codes: tuple[str, ...],
    preview_ids: tuple[str, ...],
    observation_ids: tuple[str, ...],
    user_confirmed: bool,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> ManualFinalResponse:
    normalized_date = normalize_review_date(review_date)
    validate_required_fields(
        {
            "最终应对": response,
            "风险边界": risk_boundary,
            "证据引用": evidence_reference,
            "计划项": plan_item,
        }
    )
    if not user_confirmed:
        raise ManualReviewError("STEP 10 最终应对必须由用户显式确认。")
    if not plan_reference.exists():
        raise ManualReviewError(f"计划 Markdown 不存在：{plan_reference}")
    if f"### {plan_item.strip()}" not in plan_reference.read_text(encoding="utf-8"):
        raise ManualReviewError(f"计划 Markdown 中未找到计划项：{plan_item.strip()}")

    normalized_pool_codes = normalize_identifiers(pool_codes, "真实池代码")
    normalized_preview_ids = normalize_identifiers(preview_ids, "STEP 8 预演 ID")
    normalized_observation_ids = normalize_identifiers(observation_ids, "Observation ID")
    from stock_review.observations.manage_observation import list_observations
    from stock_review.pools.manage_pool_item import list_pool_items

    real_pool_codes = {item.code for item in list_pool_items(record_kind="real", database_path=database_path) if item.status == "active"}
    if not set(normalized_pool_codes).issubset(real_pool_codes):
        raise ManualReviewError("STEP 10 关联的真实池代码不存在或未激活。")
    _, previews = list_manual_review_records(normalized_date, database_path=database_path)
    if not set(normalized_preview_ids).issubset({preview.preview_id for preview in previews}):
        raise ManualReviewError("STEP 10 关联的 STEP 8 预演 ID 不属于该复盘日期。")
    observations = list_observations(review_date=normalized_date, database_path=database_path)
    selected_observations = [item for item in observations if item.observation_id in normalized_observation_ids]
    if len(selected_observations) != len(normalized_observation_ids):
        raise ManualReviewError("STEP 10 关联的 Observation ID 不属于该复盘日期。")
    if any(item.plan_item != plan_item.strip() for item in selected_observations):
        raise ManualReviewError("STEP 10 关联的 Observation 计划项与指定计划项不一致。")

    from stock_review.storage.sqlite_repository import ManualReviewRepository

    repository = ManualReviewRepository(database_path)
    final_response = ManualFinalResponse(
        final_id=repository.next_final_id(normalized_date),
        review_date=normalized_date,
        response=response.strip(),
        risk_boundary=risk_boundary.strip(),
        evidence_reference=evidence_reference.strip(),
        plan_reference=str(plan_reference),
        plan_item=plan_item.strip(),
        pool_codes=normalized_pool_codes,
        preview_ids=normalized_preview_ids,
        observation_ids=normalized_observation_ids,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    repository.add_final_response(final_response)
    return final_response


# 查询只返回用户已保存的记录，供后续计划与 Observation 显式关联；本函数不创建任何业务记录。
def list_manual_review_records(
    review_date: str,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> tuple[list[ManualStepRecord], list[ManualPreview]]:
    normalized_date = normalize_review_date(review_date)
    from stock_review.storage.sqlite_repository import ManualReviewRepository

    repository = ManualReviewRepository(database_path)
    return repository.list_step_records(normalized_date), repository.list_previews(normalized_date)


# 最终应对只回显已保存的显式关联，供用户回查而不生成新的计划或 Observation。
def list_manual_final_responses(review_date: str, database_path: Path = DEFAULT_DATABASE_PATH) -> list[ManualFinalResponse]:
    normalized_date = normalize_review_date(review_date)
    from stock_review.storage.sqlite_repository import ManualReviewRepository

    return ManualReviewRepository(database_path).list_final_responses(normalized_date)


def normalize_review_date(review_date: str) -> str:
    try:
        return date.fromisoformat(review_date).isoformat()
    except ValueError as error:
        raise ManualReviewError("复盘日期格式必须为 YYYY-MM-DD。") from error


def validate_required_fields(values: dict[str, str]) -> None:
    for label, value in values.items():
        if not value.strip():
            raise ManualReviewError(f"{label}不能为空。")


def normalize_identifiers(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not normalized:
        raise ManualReviewError(f"至少关联一个{label}。")
    return normalized


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
