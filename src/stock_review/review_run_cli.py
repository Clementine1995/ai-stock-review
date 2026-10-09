# 本文件只负责新版 ReviewRun CLI 参数、输入解码和应用服务调用，不直接执行 SQL 或业务判断。

from __future__ import annotations

import argparse
import base64
from datetime import date, timedelta
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from stock_review.learning.create_weekly_review_run import create_weekly_review_run
from stock_review.observations.review_due_observations import (
    propose_observation_result,
    record_observation_result,
)
from stock_review.review_runs.action_set import FinalizationProposal
from stock_review.review_runs.canonical_hash import canonical_json
from stock_review.review_runs.complete_daily_review import complete_local_review
from stock_review.review_runs.complete_model_review import (
    complete_model_review,
    prepare_model_review,
)
from stock_review.review_runs.finalize_daily_review import (
    finalize_daily_review,
    propose_daily_actions,
)
from stock_review.review_runs.plan_daily_review import (
    DEFAULT_REVIEW_RUN_DATABASE_PATH,
    plan_daily_review,
)
from stock_review.review_runs.revise_daily_review import revise_daily_review
from stock_review.review_runs.run_daily_review import (
    attach_daily_evidence,
    seal_daily_evidence,
    start_daily_review,
)
from stock_review.review_runs.run_manifest import RunManifest
from stock_review.review_runs.verify_daily_review import (
    describe_daily_review,
    verify_daily_review,
)
from stock_review.storage.plan_review_run_migration import (
    MigrationPlan,
    migrate_review_run_database,
    plan_review_run_migration,
    require_review_run_schema,
)
from stock_review.storage.project_audit_log import project_pending_audit_log
from stock_review.storage.review_run_repository import ReviewRunRepository, SCHEMA_VERSION


DEFAULT_DATABASE = DEFAULT_REVIEW_RUN_DATABASE_PATH
DEFAULT_EVIDENCE_DIR = Path("data") / "evidence"
DEFAULT_DAILY_DIR = Path("reports") / "daily"
DEFAULT_WEEKLY_DIR = Path("reports") / "weekly"
DEFAULT_AUDIT_LOG = Path("logs") / "review_run.log"


def register_review_run_commands(
    subparsers: Any,
    observation_subparsers: Any,
    observation_list_parser: argparse.ArgumentParser,
    pool_list_parser: argparse.ArgumentParser,
) -> None:
    daily = subparsers.add_parser("daily", help="版本化 ReviewRun 每日复盘")
    daily_commands = daily.add_subparsers(dest="daily_command", required=True)
    _register_daily_plan(daily_commands)
    _register_daily_start(daily_commands)
    _register_daily_attach(daily_commands)
    _register_daily_seal(daily_commands)
    _register_daily_complete_local(daily_commands)
    _register_daily_prepare_model(daily_commands)
    _register_daily_complete_model(daily_commands)
    _register_daily_revise(daily_commands)
    _register_daily_propose(daily_commands)
    _register_daily_finalize(daily_commands)
    _register_daily_show(daily_commands)
    _register_daily_verify(daily_commands)

    weekly = subparsers.add_parser("weekly", help="版本化周报")
    weekly_commands = weekly.add_subparsers(dest="weekly_command", required=True)
    weekly_create = weekly_commands.add_parser("create", help="冻结当前正式日报生成周报")
    weekly_create.add_argument("--end-date", required=True, help="周期结束日期")
    weekly_create.add_argument("--start-date", help="周期开始日期；默认取结束日所在周周一")
    weekly_create.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    weekly_create.add_argument("--output-dir", type=Path, default=DEFAULT_WEEKLY_DIR)
    _add_audit_log(weekly_create)
    weekly_create.set_defaults(handler=handle_weekly_create)

    # observation list 的正式语义切换为新版不可变 Observation 投影。
    observation_list_parser.add_argument(
        "--due-on-or-before",
        help="只列出最晚回填日期不晚于该日的记录",
    )
    # 两个共用命令已经切换为新版只读投影，默认库也必须跟随新版主链。
    observation_list_parser.set_defaults(
        database=DEFAULT_DATABASE,
        handler=handle_review_run_observation_list,
    )
    pool_list_parser.set_defaults(
        database=DEFAULT_DATABASE,
        handler=handle_review_run_pool_list,
    )
    propose_result = observation_subparsers.add_parser(
        "propose-result", help="只读生成 Observation 结果动作"
    )
    propose_result.add_argument("--observation-id", required=True)
    propose_result.add_argument("--expected-result-revision", default="null")
    propose_result.add_argument("--status", required=True, choices=["hit", "miss", "invalid"])
    propose_result.add_argument("--actual-result", required=True)
    propose_result.add_argument("--reason", default="")
    propose_result.add_argument("--evidence-reference", action="append", default=[])
    propose_result.add_argument("--note", default="")
    propose_result.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    propose_result.set_defaults(handler=handle_observation_propose_result)
    record_result = observation_subparsers.add_parser(
        "record-result", help="提交已确认的 Observation 结果动作"
    )
    record_result.add_argument("--action-base64-stdin", action="store_true", required=True)
    record_result.add_argument("--action-hash", required=True)
    record_result.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    _add_audit_log(record_result)
    record_result.set_defaults(handler=handle_observation_record_result)

    database = subparsers.add_parser("database", help="ReviewRun 数据库迁移与审计")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    plan_migration = database_commands.add_parser(
        "plan-migration", help="只读生成数据库迁移计划"
    )
    plan_migration.add_argument("--database", required=True, type=Path)
    plan_migration.add_argument("--target-version", type=int, default=SCHEMA_VERSION)
    plan_migration.add_argument("--backup-path", required=True, type=Path)
    plan_migration.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    plan_migration.add_argument("--daily-report-dir", type=Path, default=DEFAULT_DAILY_DIR)
    plan_migration.add_argument("--weekly-report-dir", type=Path, default=DEFAULT_WEEKLY_DIR)
    plan_migration.set_defaults(handler=handle_database_plan_migration)
    migrate = database_commands.add_parser(
        "migrate", help="按已确认计划迁移并登记旧产物"
    )
    migrate.add_argument("--database", required=True, type=Path)
    migrate.add_argument("--plan-base64-stdin", action="store_true", required=True)
    migrate.add_argument("--plan-hash", required=True)
    migrate.add_argument("--confirmed", action="store_true", required=True)
    _add_audit_log(migrate)
    migrate.set_defaults(handler=handle_database_migrate)
    project_log = database_commands.add_parser(
        "project-audit-log", help="幂等恢复尚未投递的本地审计日志"
    )
    project_log.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    project_log.add_argument("--log", type=Path, default=DEFAULT_AUDIT_LOG)
    project_log.add_argument("--limit", type=int, default=100)
    project_log.set_defaults(handler=handle_project_audit_log)


def _register_daily_plan(commands: Any) -> None:
    parser = commands.add_parser("plan", help="只读生成待确认 RunManifest")
    parser.add_argument("--date", required=True)
    parser.add_argument("--capture", action="append", default=[])
    parser.add_argument("--conditional-capture", action="append", default=[])
    parser.add_argument(
        "--capture-profile-base64-stdin",
        action="store_true",
        help="从 stdin 读取绑定有限对象与记录上限的结构化采集 profile",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DAILY_DIR)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--model-required", action="store_true")
    parser.set_defaults(handler=handle_daily_plan)


def _register_daily_start(commands: Any) -> None:
    parser = commands.add_parser("start", help="按已确认 RunManifest 开始运行")
    parser.add_argument("--manifest-base64-stdin", action="store_true", required=True)
    parser.add_argument("--manifest-hash", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    _add_audit_log(parser)
    parser.set_defaults(handler=handle_daily_start)


def _register_daily_attach(commands: Any) -> None:
    parser = commands.add_parser("attach-evidence", help="提交 manifest 允许的外部证据信封")
    _add_run_identity(parser)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--input-base64-stdin", action="store_true", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    _add_audit_log(parser)
    parser.set_defaults(handler=handle_daily_attach)


def _register_daily_seal(commands: Any) -> None:
    parser = commands.add_parser("seal-evidence", help="封存不可变 EvidenceBundle")
    _add_run_identity(parser)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    _add_audit_log(parser)
    parser.set_defaults(handler=handle_daily_seal)


def _register_daily_complete_local(commands: Any) -> None:
    parser = commands.add_parser("complete-local", help="生成规则版 RevisionBundle")
    _add_run_identity(parser)
    parser.add_argument("--expected-current-revision-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--framework", type=Path, default=Path("stock-review.md"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    _add_audit_log(parser)
    parser.set_defaults(handler=handle_daily_complete_local)


def _register_daily_prepare_model(commands: Any) -> None:
    parser = commands.add_parser("prepare-model", help="冻结并保存模型请求")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--base-revision-id", required=True)
    parser.add_argument("--expected-current-revision-id", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    _add_audit_log(parser)
    parser.set_defaults(handler=handle_daily_prepare_model)


def _register_daily_complete_model(commands: Any) -> None:
    parser = commands.add_parser("complete-model", help="验证并提交受限模型响应")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--base-revision-id", required=True)
    parser.add_argument("--expected-current-revision-id", required=True)
    parser.add_argument("--model-response-base64-stdin", action="store_true", required=True)
    parser.add_argument("--manifest-hash", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--framework", type=Path, default=Path("stock-review.md"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    _add_audit_log(parser)
    parser.set_defaults(handler=handle_daily_complete_model)


def _register_daily_revise(commands: Any) -> None:
    parser = commands.add_parser("revise", help="追加全量人工联合修订")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--base-revision-id", required=True)
    parser.add_argument("--expected-current-revision-id", required=True)
    parser.add_argument("--edits-base64-stdin", action="store_true", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    _add_audit_log(parser)
    parser.set_defaults(handler=handle_daily_revise)


def _register_daily_propose(commands: Any) -> None:
    parser = commands.add_parser("propose-actions", help="只读生成最终修订与 ActionSet")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--review-revision-id", required=True)
    parser.add_argument("--selections-base64-stdin", action="store_true", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DAILY_DIR)
    parser.set_defaults(handler=handle_daily_propose)


def _register_daily_finalize(commands: Any) -> None:
    parser = commands.add_parser("finalize", help="原子提交已确认 FinalizationProposal")
    parser.add_argument("--proposal-base64-stdin", action="store_true", required=True)
    parser.add_argument("--proposal-hash", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    _add_audit_log(parser)
    parser.set_defaults(handler=handle_daily_finalize)


def _register_daily_show(commands: Any) -> None:
    parser = commands.add_parser("show", help="显示 ReviewRun 当前只读投影")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.set_defaults(handler=handle_daily_show)


def _register_daily_verify(commands: Any) -> None:
    parser = commands.add_parser("verify", help="校验 ReviewRun 完整依赖链")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.set_defaults(handler=handle_daily_verify)


def _add_run_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--manifest-hash", required=True)


def _add_audit_log(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)


def handle_daily_plan(args: argparse.Namespace) -> int:
    capture_profile = (
        _read_base64_mapping()
        if args.capture_profile_base64_stdin
        else None
    )
    manifest = plan_daily_review(
        args.date,
        tuple(args.capture),
        tuple(args.conditional_capture),
        capture_profile=capture_profile,
        output_dir=args.output_dir,
        evidence_dir=args.evidence_dir,
        database_path=args.database,
        model_ids=tuple(args.model),
        model_required=args.model_required,
    )
    _print_json(manifest.to_dict())
    return 0


def handle_daily_start(args: argparse.Namespace) -> int:
    manifest = RunManifest.from_dict(_read_base64_mapping())
    repository = _review_run_repository(args.database, allow_empty=True)
    run = start_daily_review(manifest, args.manifest_hash, repository)
    _project(repository, args.audit_log)
    _print_json(run.to_dict())
    return 0


def handle_daily_attach(args: argparse.Namespace) -> int:
    repository = _review_run_repository(args.database)
    run = attach_daily_evidence(
        args.run_id,
        args.date,
        args.manifest_hash,
        args.operation_id,
        _read_base64_mapping(),
        repository,
    )
    _project(repository, args.audit_log)
    _print_json(run.to_dict())
    return 0


def handle_daily_seal(args: argparse.Namespace) -> int:
    repository = _review_run_repository(args.database)
    run, bundle = seal_daily_evidence(
        args.run_id,
        args.date,
        args.manifest_hash,
        repository,
    )
    _project(repository, args.audit_log)
    _print_json({"run": run.to_dict(), "evidence_bundle": bundle.to_dict()})
    return 0


def handle_daily_complete_local(args: argparse.Namespace) -> int:
    repository = _review_run_repository(args.database)
    revision = complete_local_review(
        args.run_id,
        args.date,
        _nullable_id(args.expected_current_revision_id),
        args.manifest_hash,
        args.operation_id,
        repository,
        framework_path=args.framework,
    )
    _project(repository, args.audit_log)
    _print_json(revision.to_dict())
    return 0


def handle_daily_prepare_model(args: argparse.Namespace) -> int:
    repository = _review_run_repository(args.database)
    request = prepare_model_review(
        args.run_id,
        args.date,
        _nullable_id(args.base_revision_id),
        _nullable_id(args.expected_current_revision_id),
        args.model_id,
        args.operation_id,
        repository,
    )
    _project(repository, args.audit_log)
    _print_json(request)
    return 0


def handle_daily_complete_model(args: argparse.Namespace) -> int:
    repository = _review_run_repository(args.database)
    revision = complete_model_review(
        args.run_id,
        args.date,
        args.request_id,
        _nullable_id(args.base_revision_id),
        _nullable_id(args.expected_current_revision_id),
        _read_base64_mapping(),
        args.manifest_hash,
        args.operation_id,
        repository,
        framework_path=args.framework,
    )
    _project(repository, args.audit_log)
    _print_json(revision.to_dict())
    return 0


def handle_daily_revise(args: argparse.Namespace) -> int:
    repository = _review_run_repository(args.database)
    revision = revise_daily_review(
        args.run_id,
        args.base_revision_id,
        args.expected_current_revision_id,
        _read_base64_mapping(),
        args.operation_id,
        repository,
    )
    _project(repository, args.audit_log)
    _print_json(revision.to_dict())
    return 0


def handle_daily_propose(args: argparse.Namespace) -> int:
    proposal, preview = propose_daily_actions(
        args.run_id,
        args.review_revision_id,
        _read_base64_mapping(),
        _review_run_repository(args.database),
        output_dir=args.output_dir,
    )
    _print_json({"proposal": proposal.to_dict(), "report_preview": preview})
    return 0


def handle_daily_finalize(args: argparse.Namespace) -> int:
    value = _read_base64_mapping()
    proposal_value = value.get("proposal", value)
    if not isinstance(proposal_value, Mapping):
        raise ValueError("proposal 必须是 JSON 对象。")
    proposal = FinalizationProposal.from_dict(proposal_value)
    repository = _review_run_repository(args.database)
    result = finalize_daily_review(proposal, args.proposal_hash, repository)
    _project(repository, args.audit_log)
    _print_json(result)
    return 0


def handle_daily_show(args: argparse.Namespace) -> int:
    _print_json(describe_daily_review(args.run_id, _review_run_repository(args.database)))
    return 0


def handle_daily_verify(args: argparse.Namespace) -> int:
    result = verify_daily_review(args.run_id, _review_run_repository(args.database))
    _print_json(result.to_dict())
    return 0 if result.valid else 1


def handle_weekly_create(args: argparse.Namespace) -> int:
    end = date.fromisoformat(args.end_date)
    start = args.start_date or (end - timedelta(days=end.weekday())).isoformat()
    repository = _review_run_repository(args.database)
    result = create_weekly_review_run(
        start,
        end.isoformat(),
        repository,
        output_root=args.output_dir,
    )
    _project(repository, args.audit_log)
    _print_json(result)
    return 0


def handle_review_run_observation_list(args: argparse.Namespace) -> int:
    records = _review_run_repository(args.database).list_observations(
        review_date=args.date,
        due_on_or_before=args.due_on_or_before,
    )
    if args.status:
        records = [
            item for item in records if item["current_result"]["status"] == args.status
        ]
    _print_json({"observations": records})
    return 0


def handle_review_run_pool_list(args: argparse.Namespace) -> int:
    records = _review_run_repository(args.database).list_current_pool_statuses(args.type)
    if args.record_kind:
        records = [
            item
            for item in records
            if item["payload"].get("record_kind") == args.record_kind
        ]
    _print_json({"pool_items": records})
    return 0


def handle_observation_propose_result(args: argparse.Namespace) -> int:
    action = propose_observation_result(
        args.observation_id,
        _nullable_id(args.expected_result_revision),
        args.status,
        args.actual_result,
        args.reason,
        tuple(args.evidence_reference),
        _review_run_repository(args.database),
        note=args.note,
    )
    _print_json(action)
    return 0


def handle_observation_record_result(args: argparse.Namespace) -> int:
    repository = _review_run_repository(args.database)
    result = record_observation_result(
        _read_base64_mapping(),
        args.action_hash,
        repository,
    )
    _project(repository, args.audit_log)
    _print_json(result)
    return 0


def handle_database_plan_migration(args: argparse.Namespace) -> int:
    plan = plan_review_run_migration(
        args.database,
        args.target_version,
        args.backup_path,
        evidence_dir=args.evidence_dir,
        daily_report_dir=args.daily_report_dir,
        weekly_report_dir=args.weekly_report_dir,
    )
    _print_json(plan.to_dict())
    return 0


def handle_database_migrate(args: argparse.Namespace) -> int:
    plan = MigrationPlan.from_dict(_read_base64_mapping())
    if Path(plan.database_path).resolve() != args.database.resolve():
        raise ValueError("--database 与迁移计划绑定的目标库不一致。")
    result = migrate_review_run_database(
        plan,
        args.plan_hash,
        confirmed=args.confirmed,
    )
    repository = _review_run_repository(args.database)
    _project(repository, args.audit_log)
    _print_json(result)
    return 0


def handle_project_audit_log(args: argparse.Namespace) -> int:
    _print_json(
        project_pending_audit_log(
            _review_run_repository(args.database),
            args.log,
            limit=args.limit,
        )
    )
    return 0


def _project(repository: ReviewRunRepository, log_path: Path) -> None:
    project_pending_audit_log(repository, log_path)


# CLI 在进入应用服务前统一验证新版 schema，把旧库结构差异收敛为可读的领域错误。
def _review_run_repository(
    database_path: Path,
    *,
    allow_empty: bool = False,
) -> ReviewRunRepository:
    require_review_run_schema(database_path, allow_empty=allow_empty)
    return ReviewRunRepository(database_path)


def _read_base64_mapping() -> Mapping[str, Any]:
    encoded = sys.stdin.buffer.read().strip()
    if not encoded:
        raise ValueError("stdin 没有 base64 JSON 输入。")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        value = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"stdin 不是有效的 base64 UTF-8 JSON：{error}") from error
    if not isinstance(value, Mapping):
        raise ValueError("stdin JSON 顶层必须是对象。")
    return value


def _nullable_id(value: str) -> str | None:
    return None if value.strip().lower() in {"", "null", "none"} else value.strip()


def _print_json(value: Mapping[str, Any]) -> None:
    print(canonical_json(value))
