# 本文件负责生成只读 RunManifest，明确一次复盘获准使用的来源、范围与副作用。

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.run_manifest import (
    DAILY_REVIEW_DATABASE_TABLES,
    CaptureSlot,
    ModelUse,
    RunManifest,
)


DEFAULT_MANIFEST_LIFETIME = timedelta(minutes=30)
# 新版运行使用独立数据库，避免首次使用被旧 schema 迁移阻塞。
DEFAULT_REVIEW_RUN_DATABASE_PATH = Path("data") / "review_run.sqlite"
DEFAULT_REVIEW_RUN_EVIDENCE_DIR = Path("data") / "evidence"
DEFAULT_REVIEW_RUN_REPORT_DIR = Path("reports") / "daily"
EXTERNAL_ENVELOPE_SOURCES = {"manual", "tongdaxin", "workbuddy"}
SCOPE_FIELDS = {
    "market": ("market",),
    "sentiment": ("sentiment",),
    "sectors": ("sectors",),
    "stocks": ("stocks",),
    "snapshot": ("market", "sentiment", "sectors", "stocks", "events"),
    "index-history": ("market.index_history",),
    "sector-history": ("sector_history",),
    "sector_history": ("sector_history",),
    "candidate-history": ("candidate_history",),
    "market-breadth": ("market.up_count", "market.down_count"),
    "stock-identity": ("stocks.code", "stocks.name", "stocks.exchange"),
}
CAPTURE_PROFILE_FIELDS = {"captures"}
CAPTURE_PROFILE_ITEM_FIELDS = {
    "source",
    "scope",
    "predicate",
    "allowed_objects",
    "max_records",
}


# 清单生成只做规范化和授权展开，不创建目录、数据库或日志。
def plan_daily_review(
    trade_date: str,
    capture_specs: tuple[str, ...] = (),
    conditional_capture_specs: tuple[str, ...] = (),
    *,
    capture_profile: Mapping[str, Any] | None = None,
    output_dir: Path = DEFAULT_REVIEW_RUN_REPORT_DIR,
    evidence_dir: Path = DEFAULT_REVIEW_RUN_EVIDENCE_DIR,
    database_path: Path = DEFAULT_REVIEW_RUN_DATABASE_PATH,
    model_ids: tuple[str, ...] = (),
    model_required: bool = False,
    now: datetime | None = None,
    manifest_lifetime: timedelta = DEFAULT_MANIFEST_LIFETIME,
    id_factory=uuid4,
) -> RunManifest:
    if manifest_lifetime <= timedelta(0):
        raise ReviewRunDomainError("RunManifest 有效期必须大于 0。")
    created_at = _normalize_timestamp(now or datetime.now(timezone.utc))
    run_id = f"RUN-{trade_date.replace('-', '')}-{id_factory().hex}"
    manifest_id = f"MANIFEST-{id_factory().hex}"
    requests = _capture_requests(
        capture_specs,
        conditional_capture_specs,
        capture_profile,
    )
    captures: list[CaptureSlot] = []
    for position, (source, scope, predicate, allowed_objects, max_records) in enumerate(
        requests,
        start=1,
    ):
        captures.append(
            _build_capture_slot(
                run_id,
                trade_date,
                source,
                scope,
                predicate,
                position,
                evidence_dir,
                id_factory,
                allowed_objects=allowed_objects,
                max_records=max_records,
            )
        )
    if not captures:
        raise ReviewRunDomainError("daily plan 至少需要一个 --capture 或 --conditional-capture。")

    model_use = ModelUse(
        mode="required" if model_required else ("optional" if model_ids else "none"),
        model_ids=tuple(model_ids),
        max_requests=len(model_ids) if model_ids else 0,
    )
    run_report_dir = Path(output_dir) / trade_date / run_id
    return RunManifest.create(
        manifest_id=manifest_id,
        run_id=run_id,
        trade_date=trade_date,
        created_at=created_at.isoformat(),
        expires_at=(created_at + manifest_lifetime).isoformat(),
        captures=tuple(captures),
        model_use=model_use,
        file_effects=(
            str(run_report_dir / "revisions"),
            str(run_report_dir / "blobs"),
        ),
        database_effects=tuple(
            f"{Path(database_path).resolve()}:{table_name}"
            for table_name in DAILY_REVIEW_DATABASE_TABLES
        ),
    )


# 条件采集使用最后一个问号分隔谓词，普通 scope 中不得偷偷携带条件。
def parse_capture_specification(specification: str, *, conditional: bool) -> tuple[str, str, str]:
    value = specification.strip()
    if not value or ":" not in value:
        raise ReviewRunDomainError("采集参数必须使用 source:scope 格式。")
    source, remainder = value.split(":", 1)
    predicate = ""
    if "?" in remainder:
        scope, predicate = remainder.rsplit("?", 1)
    else:
        scope = remainder
    source = source.strip()
    scope = scope.strip()
    predicate = predicate.strip()
    if not source or not scope:
        raise ReviewRunDomainError("采集参数的 source 和 scope 均不能为空。")
    if conditional and not predicate:
        raise ReviewRunDomainError("条件采集必须使用 source:scope?predicate 并填写谓词。")
    if not conditional and predicate:
        raise ReviewRunDomainError("普通采集不能携带条件谓词，请改用 --conditional-capture。")
    if _requires_bounded_objects(source, scope):
        raise ReviewRunDomainError(
            f"{source}:{scope} 必须通过结构化 capture profile 绑定 allowed_objects。"
        )
    return source, scope, predicate


# 结构化 profile 只允许声明采集意图；槽位 ID、字段白名单、路径和副作用仍由 plan 生成。
def _capture_requests(
    capture_specs: tuple[str, ...],
    conditional_capture_specs: tuple[str, ...],
    capture_profile: Mapping[str, Any] | None,
) -> tuple[tuple[str, str, str, tuple[str, ...], int | None], ...]:
    if capture_profile is not None:
        if capture_specs or conditional_capture_specs:
            raise ReviewRunDomainError(
                "结构化 capture profile 不能与 --capture 或 --conditional-capture 同时使用。"
            )
        return _parse_capture_profile(capture_profile)

    requests: list[tuple[str, str, str, tuple[str, ...], int | None]] = []
    for specification in capture_specs:
        source, scope, predicate = parse_capture_specification(
            specification,
            conditional=False,
        )
        requests.append((source, scope, predicate, (), None))
    for specification in conditional_capture_specs:
        source, scope, predicate = parse_capture_specification(
            specification,
            conditional=True,
        )
        requests.append((source, scope, predicate, (), None))
    return tuple(requests)


def _parse_capture_profile(
    profile: Mapping[str, Any],
) -> tuple[tuple[str, str, str, tuple[str, ...], int | None], ...]:
    unexpected = set(profile) - CAPTURE_PROFILE_FIELDS
    if unexpected:
        raise ReviewRunDomainError(
            "capture profile 包含未授权字段：" + ", ".join(sorted(unexpected))
        )
    values = profile.get("captures")
    if not isinstance(values, list) or not values:
        raise ReviewRunDomainError("capture profile 必须包含非空 captures 数组。")

    requests: list[tuple[str, str, str, tuple[str, ...], int | None]] = []
    for position, value in enumerate(values, start=1):
        if not isinstance(value, Mapping):
            raise ReviewRunDomainError(f"capture profile 第 {position} 项必须是 JSON 对象。")
        extra_fields = set(value) - CAPTURE_PROFILE_ITEM_FIELDS
        if extra_fields:
            raise ReviewRunDomainError(
                f"capture profile 第 {position} 项包含未授权字段："
                + ", ".join(sorted(extra_fields))
            )
        source = value.get("source")
        scope = value.get("scope")
        predicate = value.get("predicate", "")
        if not isinstance(source, str) or not source.strip():
            raise ReviewRunDomainError(f"capture profile 第 {position} 项 source 不能为空。")
        if not isinstance(scope, str) or not scope.strip():
            raise ReviewRunDomainError(f"capture profile 第 {position} 项 scope 不能为空。")
        if not isinstance(predicate, str):
            raise ReviewRunDomainError(f"capture profile 第 {position} 项 predicate 必须是文本。")
        source = source.strip()
        scope = scope.strip()
        predicate = predicate.strip()

        object_values = value.get("allowed_objects", [])
        if not isinstance(object_values, list) or any(
            not isinstance(item, str) or not item.strip() for item in object_values
        ):
            raise ReviewRunDomainError(
                f"capture profile 第 {position} 项 allowed_objects 必须是非空文本数组或空数组。"
            )
        allowed_objects = tuple(item.strip() for item in object_values)
        if len(allowed_objects) != len(set(allowed_objects)):
            raise ReviewRunDomainError(
                f"capture profile 第 {position} 项 allowed_objects 不能重复。"
            )

        max_records = value.get("max_records")
        if max_records is not None and (
            type(max_records) is not int or max_records < 1
        ):
            raise ReviewRunDomainError(
                f"capture profile 第 {position} 项 max_records 必须是大于 0 的 JSON 整数。"
            )
        if _requires_bounded_objects(source, scope) and not allowed_objects:
            raise ReviewRunDomainError(
                f"{source}:{scope} 必须绑定非空 allowed_objects。"
            )
        if allowed_objects and max_records is None:
            max_records = len(allowed_objects)
        requests.append((source, scope, predicate, allowed_objects, max_records))
    return tuple(requests)


def _requires_bounded_objects(source: str, scope: str) -> bool:
    return source == "workbuddy" and scope.replace("-", "_") in {
        "stocks",
        "sector_history",
    }


def _build_capture_slot(
    run_id: str,
    trade_date: str,
    source: str,
    scope: str,
    predicate: str,
    position: int,
    evidence_dir: Path,
    id_factory,
    *,
    allowed_objects: tuple[str, ...] = (),
    max_records: int | None = None,
) -> CaptureSlot:
    slot_id = f"capture-{position:02d}-{source}-{scope}".replace("/", "-").replace("\\", "-")
    output_path = Path(evidence_dir) / trade_date / run_id / "captures" / f"{slot_id}.json"
    return CaptureSlot(
        slot_id=slot_id,
        operation_id=f"OP-{id_factory().hex}",
        source=source,
        scope=scope,
        input_location=("base64-stdin" if source in EXTERNAL_ENVELOPE_SOURCES else f"{source}:direct"),
        output_location=str(output_path),
        network_required=source != "manual",
        write_effects=(str(output_path), "SQLite:evidence_captures"),
        predicate=predicate,
        allowed_objects=allowed_objects,
        allowed_dates=(trade_date,),
        allowed_fields=SCOPE_FIELDS.get(scope, (scope,)),
        max_records=max_records,
    )


def _normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReviewRunDomainError("RunManifest 创建时间必须包含时区。")
    return value.astimezone(timezone.utc).replace(microsecond=0)
