# 本文件负责 Observation 的人工创建、查询和结果回填，不自动生成投资判断。

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import sqlite3


DEFAULT_DATABASE_PATH = Path("data") / "stock_review.sqlite"
VALID_OBSERVATION_STATUSES = {"pending", "hit", "miss", "invalid"}


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class Observation:
    observation_id: str
    review_date: str
    topic: str
    related_target: str
    hypothesis: str
    confirmation_condition: str
    invalidation_condition: str
    evidence_source: str
    plan_item: str
    status: str
    actual_result: str
    review_note: str
    created_at: str
    updated_at: str

    @property
    def eligible_for_learning(self) -> bool:
        return self.status in {"hit", "miss"}


# Observation 只能由用户显式填写可验证假设和证据，不从日报或计划中自动推导。
def add_observation(
    review_date: str,
    topic: str,
    hypothesis: str,
    confirmation_condition: str,
    invalidation_condition: str,
    evidence_source: str,
    related_target: str = "",
    plan_item: str = "",
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> Observation:
    normalized_date = date.fromisoformat(review_date).isoformat()
    required_fields = {
        "判断主题": topic,
        "假设": hypothesis,
        "成立条件": confirmation_condition,
        "失效条件": invalidation_condition,
        "证据来源": evidence_source,
    }
    for label, value in required_fields.items():
        if not value.strip():
            raise ObservationError(f"{label}不能为空。")

    from stock_review.storage.sqlite_repository import ObservationRepository

    repository = ObservationRepository(database_path)
    now = datetime.now().isoformat(timespec="seconds")
    observation = Observation(
        observation_id=repository.next_observation_id(normalized_date),
        review_date=normalized_date,
        topic=topic.strip(),
        related_target=related_target.strip() or "待确认",
        hypothesis=hypothesis.strip(),
        confirmation_condition=confirmation_condition.strip(),
        invalidation_condition=invalidation_condition.strip(),
        evidence_source=evidence_source.strip(),
        plan_item=plan_item.strip(),
        status="pending",
        actual_result="",
        review_note="",
        created_at=now,
        updated_at=now,
    )
    try:
        repository.add_observation(observation)
    except sqlite3.IntegrityError as error:
        raise ObservationError("同一复盘日期已存在相同主题和假设的 Observation。") from error
    return observation


# 查询只按日期和状态过滤，不在此处生成经验统计或判断结论。
def list_observations(
    review_date: str | None = None,
    status: str | None = None,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> list[Observation]:
    if review_date is not None:
        review_date = date.fromisoformat(review_date).isoformat()
    if status is not None:
        validate_observation_status(status)

    from stock_review.storage.sqlite_repository import ObservationRepository

    return ObservationRepository(database_path).list_observations(review_date, status)


# 周度学习只读取给定日期范围内的 Observation，不修改原记录。
def list_observations_between(
    start_date: str,
    end_date: str,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> list[Observation]:
    normalized_start = date.fromisoformat(start_date).isoformat()
    normalized_end = date.fromisoformat(end_date).isoformat()
    if normalized_start > normalized_end:
        raise ObservationError("开始日期不能晚于结束日期。")

    from stock_review.storage.sqlite_repository import ObservationRepository

    return ObservationRepository(database_path).list_observations_between(normalized_start, normalized_end)


# 回填更新原 Observation 和唯一回填记录，重复执行不会新增第二条回填。
def review_observation(
    observation_id: str,
    status: str,
    actual_result: str,
    review_note: str = "",
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> Observation:
    validate_observation_status(status)
    if not observation_id.strip():
        raise ObservationError("Observation ID 不能为空。")
    if not actual_result.strip():
        raise ObservationError("实际结果不能为空。")

    from stock_review.storage.sqlite_repository import ObservationRepository

    repository = ObservationRepository(database_path)
    observation = repository.review_observation(
        observation_id.strip(),
        status,
        actual_result.strip(),
        review_note.strip(),
        datetime.now().isoformat(timespec="seconds"),
    )
    if observation is None:
        raise ObservationError(f"未找到 Observation：{observation_id.strip()}")
    return observation


def validate_observation_status(status: str) -> None:
    if status not in VALID_OBSERVATION_STATUSES:
        raise ObservationError("Observation 状态必须是 pending、hit、miss 或 invalid。")
