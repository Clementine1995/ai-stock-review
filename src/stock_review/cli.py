# 本文件负责 CLI 参数解析、错误展示，并调用当前已实现的应用服务。

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from stock_review.evidence.manage_evidence_snapshot import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_EVIDENCE_DIR,
    EvidenceSnapshotError,
    check_evidence_snapshot,
    collect_akshare_evidence_scopes,
    collect_akshare_evidence_snapshot,
    import_evidence_snapshot,
    collect_hhxg_evidence_snapshot,
)
from stock_review.evidence.summarize_sector_history import create_sector_history_report
from stock_review.evidence.summarize_market_history import create_market_history_report
from stock_review.evidence.check_history_readiness import (
    check_hhxg_history_readiness,
    render_hhxg_history_readiness,
)
from stock_review.evidence.summarize_hhxg_history import create_hhxg_history_report
from stock_review.evidence.collect_pool_stock_history import collect_real_pool_stock_history
from stock_review.evidence.summarize_review_facts import create_review_facts_report
from stock_review.learning.summarize_weekly_learning import create_weekly_learning
from stock_review.observations.manage_observation import (
    ObservationError,
    add_observation,
    list_observations,
    review_observation,
)
from stock_review.planning.build_trade_plan import TradePlanError, create_trade_plan
from stock_review.pools.manage_pool_item import (
    PoolItemError,
    add_pool_item,
    list_pool_item_status_history,
    list_pool_items,
    migrate_pool_item_sectors,
    update_pool_item_record_kind,
    update_pool_item_status,
)
from stock_review.pools.build_pool_candidates import build_pool_candidate_summary, render_pool_candidate_summary
from stock_review.review_documents.create_daily_review import create_daily_review
from stock_review.review_documents.manage_manual_review import (
    ManualReviewError,
    add_manual_preview,
    add_manual_step_record,
    list_manual_review_records,
    write_manual_review_log,
)
from stock_review.review_documents.build_review_context import build_review_context, render_review_context
from stock_review.review_framework.parse_framework import (
    FrameworkParseError,
    parse_framework_file,
)
from stock_review.scoring.score_market_state import create_scoring_report


# CLI 失败时需要给出明确原因，并用非 0 退出码提示调用方当前命令未完成。
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "handler"):
        parser.print_help()
        return 0

    try:
        return args.handler(args)
    except (EvidenceSnapshotError, FrameworkParseError, ManualReviewError, ObservationError, PoolItemError, TradePlanError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"错误：文件操作失败：{error}", file=sys.stderr)
        return 1


# 命令树只暴露当前已完成里程碑的正式入口，避免提前扩展未实现的业务命令。
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock_review",
        description="价值投机复盘助手 CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    framework_parser = subparsers.add_parser("framework", help="复盘框架相关命令")
    framework_subparsers = framework_parser.add_subparsers(dest="framework_command")
    framework_check_parser = framework_subparsers.add_parser(
        "check",
        help="检查 stock-review.md 的 STEP 识别结果",
    )
    framework_check_parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="复盘框架 Markdown 文件路径",
    )
    framework_check_parser.set_defaults(handler=handle_framework_check)

    review_parser = subparsers.add_parser("review", help="每日复盘文档相关命令")
    review_subparsers = review_parser.add_subparsers(dest="review_command")
    review_create_parser = review_subparsers.add_parser(
        "create",
        help="按复盘框架生成每日复盘 Markdown",
    )
    review_create_parser.add_argument(
        "--date",
        required=True,
        help="交易日期，格式 YYYY-MM-DD",
    )
    review_create_parser.add_argument(
        "--framework",
        required=True,
        type=Path,
        help="复盘框架 Markdown 文件路径",
    )
    review_create_parser.add_argument(
        "--evidence",
        type=Path,
        help="可选 Evidence Snapshot JSON 文件路径",
    )
    review_create_parser.set_defaults(handler=handle_review_create)

    review_record_step_parser = review_subparsers.add_parser(
        "record-step",
        help="保存用户的单个 STEP 人工判断和证据引用",
    )
    review_record_step_parser.add_argument("--date", required=True, help="复盘日期，格式 YYYY-MM-DD")
    review_record_step_parser.add_argument("--step", required=True, type=int, help="STEP 编号")
    review_record_step_parser.add_argument("--judgment", required=True, help="人工判断")
    review_record_step_parser.add_argument("--evidence-reference", required=True, help="Evidence Snapshot 或人工证据引用")
    review_record_step_parser.add_argument("--hypothesis", default="", help="可选待验证假设")
    review_record_step_parser.add_argument("--note", default="", help="可选人工备注")
    review_record_step_parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH, help="本地 SQLite 数据库路径"
    )
    review_record_step_parser.add_argument("--log-path", type=Path, default=Path("logs") / "stock_review.log", help="本地日志路径")
    review_record_step_parser.set_defaults(handler=handle_review_record_step)

    review_record_preview_parser = review_subparsers.add_parser(
        "record-preview",
        help="保存用户确认的 STEP 8 四类预演条件",
    )
    review_record_preview_parser.add_argument("--date", required=True, help="复盘日期，格式 YYYY-MM-DD")
    review_record_preview_parser.add_argument("--target", required=True, help="预演对象")
    review_record_preview_parser.add_argument("--expectation", required=True, help="符合预期条件")
    review_record_preview_parser.add_argument("--over-expectation", required=True, help="超预期条件")
    review_record_preview_parser.add_argument("--under-expectation", required=True, help="不及预期条件")
    review_record_preview_parser.add_argument("--abandon-condition", required=True, help="放弃条件")
    review_record_preview_parser.add_argument("--evidence-reference", required=True, help="Evidence Snapshot 或人工证据引用")
    review_record_preview_parser.add_argument("--confirmed", action="store_true", help="确认这是用户本人输入的预演")
    review_record_preview_parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH, help="本地 SQLite 数据库路径"
    )
    review_record_preview_parser.add_argument("--log-path", type=Path, default=Path("logs") / "stock_review.log", help="本地日志路径")
    review_record_preview_parser.set_defaults(handler=handle_review_record_preview)

    review_list_records_parser = review_subparsers.add_parser(
        "list-records",
        help="按复盘日期查看人工 STEP 判断和 STEP 8 预演",
    )
    review_list_records_parser.add_argument("--date", required=True, help="复盘日期，格式 YYYY-MM-DD")
    review_list_records_parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH, help="本地 SQLite 数据库路径"
    )
    review_list_records_parser.set_defaults(handler=handle_review_list_records)

    review_build_context_parser = review_subparsers.add_parser(
        "build-context",
        help="只读汇总复盘证据、人工记录和有效真实池对象",
    )
    review_build_context_parser.add_argument("--date", required=True, help="复盘日期，格式 YYYY-MM-DD")
    review_build_context_parser.add_argument(
        "--snapshot-dir", type=Path, default=DEFAULT_EVIDENCE_DIR, help="Evidence Snapshot 目录"
    )
    review_build_context_parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH, help="本地 SQLite 数据库路径"
    )
    review_build_context_parser.set_defaults(handler=handle_review_build_context)

    evidence_parser = subparsers.add_parser("evidence", help="证据快照相关命令")
    evidence_subparsers = evidence_parser.add_subparsers(dest="evidence_command")
    evidence_import_parser = evidence_subparsers.add_parser(
        "import",
        help="导入本地 JSON 样例证据并生成 Evidence Snapshot",
    )
    evidence_import_parser.add_argument(
        "--date",
        required=True,
        help="交易日期，格式 YYYY-MM-DD",
    )
    evidence_import_parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="本地 JSON 证据文件路径",
    )
    evidence_import_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="证据快照输出目录",
    )
    evidence_import_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )
    evidence_import_parser.set_defaults(handler=handle_evidence_import)

    evidence_collect_parser = evidence_subparsers.add_parser(
        "collect",
        help="调用真实数据源生成 Evidence Snapshot",
    )
    evidence_collect_parser.add_argument("--date", required=True, help="交易日期，格式 YYYY-MM-DD")
    evidence_collect_parser.add_argument(
        "--source",
        required=True,
        choices=["akshare"],
        help="数据源名称；当前仅支持 akshare",
    )
    evidence_collect_parser.add_argument(
        "--scope",
        required=True,
        choices=["market", "sentiment", "sectors", "stocks"],
        help="采集范围；当前支持 market、sentiment、sectors 或 stocks，禁止默认全市场扫描",
    )
    evidence_collect_parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="证据快照输出目录",
    )
    evidence_collect_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )
    evidence_collect_parser.add_argument(
        "--refresh",
        action="store_true",
        help="忽略同交易日已有 AKShare 事实并重新联网采集",
    )
    evidence_collect_parser.set_defaults(handler=handle_evidence_collect)

    evidence_collect_daily_parser = evidence_subparsers.add_parser(
        "collect-daily",
        help="按显式 scope 采集单日市场、情绪、板块和个股证据",
    )
    evidence_collect_daily_parser.add_argument("--date", required=True, help="交易日期，格式 YYYY-MM-DD")
    evidence_collect_daily_parser.add_argument(
        "--source",
        required=True,
        choices=["akshare"],
        help="数据源名称；当前仅支持 akshare",
    )
    evidence_collect_daily_parser.add_argument(
        "--scope",
        required=True,
        action="append",
        choices=["market", "sentiment", "sectors", "stocks"],
        help="采集范围；可重复传入，每个 scope 独立执行",
    )
    evidence_collect_daily_parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="证据快照输出目录",
    )
    evidence_collect_daily_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )
    evidence_collect_daily_parser.add_argument(
        "--refresh",
        action="store_true",
        help="忽略已有有效 scope 并重新联网采集",
    )
    evidence_collect_daily_parser.set_defaults(handler=handle_evidence_collect_daily)

    evidence_check_parser = evidence_subparsers.add_parser(
        "check",
        help="检查指定交易日 Evidence Snapshot 的缺口",
    )
    evidence_check_parser.add_argument(
        "--date",
        required=True,
        help="交易日期，格式 YYYY-MM-DD",
    )
    evidence_check_parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="证据快照目录",
    )
    evidence_check_parser.set_defaults(handler=handle_evidence_check)

    evidence_sector_history_parser = evidence_subparsers.add_parser(
        "sector-history",
        help="汇总本地快照中的当日最强板块事实和近期反复活跃候选",
    )
    evidence_sector_history_parser.add_argument("--start", required=True, help="开始日期，格式 YYYY-MM-DD")
    evidence_sector_history_parser.add_argument("--end", required=True, help="结束日期，格式 YYYY-MM-DD")
    evidence_sector_history_parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="证据快照目录",
    )
    evidence_sector_history_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports") / "daily",
        help="板块历史报告输出目录",
    )
    evidence_sector_history_parser.set_defaults(handler=handle_evidence_sector_history)

    evidence_collect_hhxg_parser = evidence_subparsers.add_parser(
        "collect-hhxg",
        help="采集 hhxg 最近交易日快照；返回日期必须与指定日期一致",
    )
    evidence_collect_hhxg_parser.add_argument("--date", required=True, help="交易日期，格式 YYYY-MM-DD")
    evidence_collect_hhxg_parser.add_argument("--output-dir", required=True, type=Path, help="证据快照输出目录")
    evidence_collect_hhxg_parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH, help="本地 SQLite 数据库路径"
    )
    evidence_collect_hhxg_parser.set_defaults(handler=handle_evidence_collect_hhxg)

    evidence_collect_pool_history_parser = evidence_subparsers.add_parser(
        "collect-pool-history",
        help="采集真实池个股的 5/20 日历史事实，禁止全市场扫描",
    )
    evidence_collect_pool_history_parser.add_argument("--date", required=True, help="交易日期，格式 YYYY-MM-DD")
    evidence_collect_pool_history_parser.add_argument(
        "--source", required=True, choices=["akshare"], help="数据源名称；当前仅支持 akshare"
    )
    evidence_collect_pool_history_parser.add_argument("--output-dir", required=True, type=Path, help="事实输出目录")
    evidence_collect_pool_history_parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH, help="本地 SQLite 数据库路径"
    )
    evidence_collect_pool_history_parser.set_defaults(handler=handle_evidence_collect_pool_history)

    evidence_history_readiness_parser = evidence_subparsers.add_parser(
        "history-readiness",
        help="检查 hhxg 本地历史是否已积累满 5 个有效交易日",
    )
    evidence_history_readiness_parser.add_argument(
        "--source", required=True, choices=["hhxg"], help="数据源名称；当前仅支持 hhxg"
    )
    evidence_history_readiness_parser.add_argument("--start", required=True, help="开始日期，格式 YYYY-MM-DD")
    evidence_history_readiness_parser.add_argument("--end", required=True, help="结束日期，格式 YYYY-MM-DD")
    evidence_history_readiness_parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="证据快照目录",
    )
    evidence_history_readiness_parser.set_defaults(handler=handle_evidence_history_readiness)

    evidence_hhxg_history_parser = evidence_subparsers.add_parser(
        "hhxg-history",
        help="汇总本地 hhxg 情绪、题材和行业排行历史事实",
    )
    evidence_hhxg_history_parser.add_argument("--start", required=True, help="开始日期，格式 YYYY-MM-DD")
    evidence_hhxg_history_parser.add_argument("--end", required=True, help="结束日期，格式 YYYY-MM-DD")
    evidence_hhxg_history_parser.add_argument(
        "--snapshot-dir", type=Path, default=DEFAULT_EVIDENCE_DIR, help="证据快照目录"
    )
    evidence_hhxg_history_parser.add_argument(
        "--output-dir", type=Path, default=Path("reports") / "daily", help="报告输出目录"
    )
    evidence_hhxg_history_parser.set_defaults(handler=handle_evidence_hhxg_history)

    evidence_market_history_parser = evidence_subparsers.add_parser(
        "market-history",
        help="汇总本地快照中的市场指数和成交额历史事实",
    )
    evidence_market_history_parser.add_argument("--start", required=True, help="开始日期，格式 YYYY-MM-DD")
    evidence_market_history_parser.add_argument("--end", required=True, help="结束日期，格式 YYYY-MM-DD")
    evidence_market_history_parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="证据快照目录",
    )
    evidence_market_history_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports") / "daily",
        help="市场历史报告输出目录",
    )
    evidence_market_history_parser.set_defaults(handler=handle_evidence_market_history)

    evidence_summarize_review_parser = evidence_subparsers.add_parser(
        "summarize-review",
        help="按 STEP 1-7 汇总本地证据事实，不生成交易结论",
    )
    evidence_summarize_review_parser.add_argument("--date", required=True, help="交易日期，格式 YYYY-MM-DD")
    evidence_summarize_review_parser.add_argument(
        "--snapshot-dir", type=Path, default=DEFAULT_EVIDENCE_DIR, help="Evidence Snapshot 目录"
    )
    evidence_summarize_review_parser.add_argument(
        "--pool-history-dir", type=Path, default=DEFAULT_EVIDENCE_DIR, help="真实池历史事实目录"
    )
    evidence_summarize_review_parser.add_argument(
        "--output-dir", type=Path, default=Path("reports") / "daily", help="事实归纳报告输出目录"
    )
    evidence_summarize_review_parser.set_defaults(handler=handle_evidence_summarize_review)

    pool_parser = subparsers.add_parser("pool", help="关注池和热点池相关命令")
    pool_subparsers = pool_parser.add_subparsers(dest="pool_command")

    watch_add_parser = pool_subparsers.add_parser("add-watch", help="手工加入关注池")
    add_pool_arguments(watch_add_parser, require_reason=False)
    watch_add_parser.set_defaults(handler=handle_pool_add_watch)

    hot_add_parser = pool_subparsers.add_parser("add-hot", help="手工加入热点池")
    add_pool_arguments(hot_add_parser, require_reason=True)
    hot_add_parser.set_defaults(handler=handle_pool_add_hot)

    pool_list_parser = pool_subparsers.add_parser("list", help="查看关注池或热点池")
    pool_list_parser.add_argument(
        "--type",
        choices=["watch", "hot"],
        help="池子类型；不传则查看全部",
    )
    pool_list_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )
    pool_list_parser.add_argument("--record-kind", choices=["real", "sample"], help="记录类型筛选")
    pool_list_parser.set_defaults(handler=handle_pool_list)

    pool_migrate_sectors_parser = pool_subparsers.add_parser(
        "migrate-sectors",
        help="将旧单板块池子结构显式迁移为多板块关联结构",
    )
    pool_migrate_sectors_parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH, help="本地 SQLite 数据库路径"
    )
    pool_migrate_sectors_parser.set_defaults(handler=handle_pool_migrate_sectors)

    pool_update_record_kind_parser = pool_subparsers.add_parser(
        "update-record-kind",
        help="标记池子记录为真实或样例，真实计划默认只使用真实记录",
    )
    pool_update_record_kind_parser.add_argument("--type", required=True, choices=["watch", "hot"], help="池子类型")
    pool_update_record_kind_parser.add_argument("--code", required=True, help="股票代码")
    pool_update_record_kind_parser.add_argument("--record-kind", required=True, choices=["real", "sample"], help="记录类型")
    pool_update_record_kind_parser.add_argument("--reason", required=True, help="类型变更原因")
    pool_update_record_kind_parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH, help="本地 SQLite 数据库路径"
    )
    pool_update_record_kind_parser.set_defaults(handler=handle_pool_update_record_kind)

    pool_candidates_parser = pool_subparsers.add_parser(
        "candidates",
        help="从本地 Evidence Snapshot 整理待人工确认的池子候选事实",
    )
    pool_candidates_parser.add_argument("--date", required=True, help="交易日期，格式 YYYY-MM-DD")
    pool_candidates_parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="证据快照目录",
    )
    pool_candidates_parser.set_defaults(handler=handle_pool_candidates)

    pool_update_status_parser = pool_subparsers.add_parser(
        "update-status",
        help="更新池子对象状态并保留维护原因",
    )
    pool_update_status_parser.add_argument("--type", required=True, choices=["watch", "hot"], help="池子类型")
    pool_update_status_parser.add_argument("--code", required=True, help="股票代码")
    pool_update_status_parser.add_argument(
        "--status",
        required=True,
        choices=["active", "paused", "removed"],
        help="新状态",
    )
    pool_update_status_parser.add_argument("--reason", required=True, help="状态变更原因")
    pool_update_status_parser.add_argument("--note", default="", help="人工备注")
    pool_update_status_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )
    pool_update_status_parser.set_defaults(handler=handle_pool_update_status)

    pool_history_parser = pool_subparsers.add_parser("history", help="查看池子对象状态历史")
    pool_history_parser.add_argument("--type", required=True, choices=["watch", "hot"], help="池子类型")
    pool_history_parser.add_argument("--code", required=True, help="股票代码")
    pool_history_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )
    pool_history_parser.set_defaults(handler=handle_pool_history)

    plan_parser = subparsers.add_parser("plan", help="次日观察计划相关命令")
    plan_subparsers = plan_parser.add_subparsers(dest="plan_command")
    plan_create_parser = plan_subparsers.add_parser(
        "create",
        help="根据复盘、证据快照和池子记录生成次日观察计划",
    )
    plan_create_parser.add_argument("--date", required=True, help="交易日期，格式 YYYY-MM-DD")
    plan_create_parser.add_argument(
        "--review",
        type=Path,
        help="每日复盘 Markdown 路径；不传则使用 reports/daily/YYYY-MM-DD_review.md",
    )
    plan_create_parser.add_argument(
        "--evidence",
        type=Path,
        help="可选 Evidence Snapshot JSON 文件路径",
    )
    plan_create_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )
    plan_create_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports") / "daily",
        help="计划 Markdown 输出目录",
    )
    plan_create_parser.set_defaults(handler=handle_plan_create)

    observation_parser = subparsers.add_parser("observation", help="Observation 相关命令")
    observation_subparsers = observation_parser.add_subparsers(dest="observation_command")

    observation_add_parser = observation_subparsers.add_parser("add", help="手工创建 Observation")
    observation_add_parser.add_argument("--date", required=True, help="复盘日期，格式 YYYY-MM-DD")
    observation_add_parser.add_argument("--topic", required=True, help="判断主题")
    observation_add_parser.add_argument("--target", default="", help="相关板块或个股，缺失时标记为待确认")
    observation_add_parser.add_argument("--hypothesis", required=True, help="可验证假设")
    observation_add_parser.add_argument("--confirmation", required=True, help="成立条件")
    observation_add_parser.add_argument("--invalidation", required=True, help="失效条件")
    observation_add_parser.add_argument("--evidence-source", required=True, help="证据来源")
    observation_add_parser.add_argument("--plan-item", default="", help="可选关联计划项")
    observation_add_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )
    observation_add_parser.set_defaults(handler=handle_observation_add)

    observation_list_parser = observation_subparsers.add_parser("list", help="查询 Observation")
    observation_list_parser.add_argument("--date", help="可选复盘日期，格式 YYYY-MM-DD")
    observation_list_parser.add_argument(
        "--status",
        choices=["pending", "hit", "miss", "invalid"],
        help="可选状态筛选",
    )
    observation_list_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )
    observation_list_parser.set_defaults(handler=handle_observation_list)

    observation_review_parser = observation_subparsers.add_parser("review", help="回填 Observation 结果")
    observation_review_parser.add_argument("--id", required=True, help="Observation ID")
    observation_review_parser.add_argument(
        "--status",
        required=True,
        choices=["pending", "hit", "miss", "invalid"],
        help="回填状态",
    )
    observation_review_parser.add_argument("--result", required=True, help="实际结果")
    observation_review_parser.add_argument("--note", default="", help="复盘备注")
    observation_review_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )
    observation_review_parser.set_defaults(handler=handle_observation_review)

    learning_parser = subparsers.add_parser("learning", help="学习总结相关命令")
    learning_subparsers = learning_parser.add_subparsers(dest="learning_command")
    learning_weekly_parser = learning_subparsers.add_parser("weekly", help="生成周度学习总结")
    learning_weekly_parser.add_argument("--start", required=True, help="开始日期，格式 YYYY-MM-DD")
    learning_weekly_parser.add_argument("--end", required=True, help="结束日期，格式 YYYY-MM-DD")
    learning_weekly_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )
    learning_weekly_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports") / "weekly",
        help="周度学习报告输出目录",
    )
    learning_weekly_parser.set_defaults(handler=handle_learning_weekly)

    scoring_parser = subparsers.add_parser("scoring", help="可解释规则评分相关命令")
    scoring_subparsers = scoring_parser.add_subparsers(dest="scoring_command")
    scoring_create_parser = scoring_subparsers.add_parser("create", help="生成市场与板块评分报告")
    scoring_create_parser.add_argument("--date", required=True, help="交易日期，格式 YYYY-MM-DD")
    scoring_create_parser.add_argument("--evidence", required=True, type=Path, help="Evidence Snapshot 路径")
    scoring_create_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports") / "daily",
        help="评分报告输出目录",
    )
    scoring_create_parser.set_defaults(handler=handle_scoring_create)

    return parser


def add_pool_arguments(parser: argparse.ArgumentParser, require_reason: bool) -> None:
    parser.add_argument("--code", required=True, help="股票代码")
    parser.add_argument("--name", required=True, help="股票名称")
    parser.add_argument("--date", required=True, help="进入池子的日期，格式 YYYY-MM-DD")
    parser.add_argument("--reason", required=require_reason, default="", help="进入池子的原因")
    parser.add_argument("--exchange", default="", help="交易所，缺失时标记为待确认")
    parser.add_argument("--sector", action="append", default=[], help="所属板块；可重复传入，最多 3 个")
    parser.add_argument("--note", default="", help="人工备注")
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="本地 SQLite 数据库路径",
    )


# 框架检查只读取文件并输出识别数量与标题，不产生本地业务状态。
def handle_framework_check(args: argparse.Namespace) -> int:
    framework = parse_framework_file(args.file)
    print(f"识别到 STEP 数量：{len(framework.steps)}")
    for step in framework.steps:
        print(f"STEP {step.number}: {step.title}")
    return 0


# 日报创建只展示 Evidence Snapshot 中已有事实，不生成推断性行情结论。
def handle_review_create(args: argparse.Namespace) -> int:
    output_path = create_daily_review(args.date, args.framework, evidence_path=args.evidence)
    print(f"已生成每日复盘：{output_path}")
    return 0


# 单个 STEP 判断只保存用户填写的原文和证据引用，不根据快照自动补写结论。
def handle_review_record_step(args: argparse.Namespace) -> int:
    record = add_manual_step_record(
        args.date,
        args.step,
        args.judgment,
        args.evidence_reference,
        hypothesis=args.hypothesis,
        note=args.note,
        database_path=args.database,
    )
    write_manual_review_log(
        "review record-step", record.review_date, record.record_id, record.evidence_reference, args.database, args.log_path
    )
    print(f"已保存人工 STEP 判断：{record.record_id}")
    return 0


# STEP 8 必须显式确认后才保存，四类条件只供人工观察和后续回查，不生成买卖指令。
def handle_review_record_preview(args: argparse.Namespace) -> int:
    preview = add_manual_preview(
        args.date,
        args.target,
        args.expectation,
        args.over_expectation,
        args.under_expectation,
        args.abandon_condition,
        args.evidence_reference,
        user_confirmed=args.confirmed,
        database_path=args.database,
    )
    write_manual_review_log(
        "review record-preview", preview.review_date, preview.preview_id, preview.evidence_reference, args.database, args.log_path
    )
    print(f"已保存 STEP 8 人工预演：{preview.preview_id}")
    return 0


# 查询只回显已保存的人工记录，不将其自动关联至计划或 Observation。
def handle_review_list_records(args: argparse.Namespace) -> int:
    records, previews = list_manual_review_records(args.date, database_path=args.database)
    if not records and not previews:
        print("暂无人工复盘记录。")
        return 0
    for record in records:
        print(f"{record.record_id} | STEP {record.step_number} | {record.judgment} | {record.evidence_reference}")
    for preview in previews:
        print(f"{preview.preview_id} | STEP 8 | {preview.target} | {preview.evidence_reference}")
    return 0


# 上下文构建只读取本地事实和人工记录，空池或缺少判断时明确返回待补状态。
def handle_review_build_context(args: argparse.Namespace) -> int:
    context = build_review_context(args.date, snapshot_dir=args.snapshot_dir, database_path=args.database)
    print(render_review_context(context), end="")
    return 0


# 样例证据导入只处理显式传入的本地文件，禁止默认扫描全市场或调用真实接口。
def handle_evidence_import(args: argparse.Namespace) -> int:
    output_path = import_evidence_snapshot(
        args.date,
        args.file,
        output_dir=args.output_dir,
        database_path=args.database,
    )
    snapshot = check_evidence_snapshot(args.date, snapshot_dir=args.output_dir)
    write_evidence_log(
        command="evidence import",
        trade_date=args.date,
        source_file=args.file,
        output_path=output_path,
        missing_fields=snapshot.missing_fields,
    )
    print(f"已生成证据快照：{output_path}")
    print(f"缺口数量：{len(snapshot.missing_fields)}")
    for missing_field in snapshot.missing_fields:
        print(f"- {missing_field}")
    return 0


# 真实数据采集必须显式声明数据源、日期、范围和输出目录。
def handle_evidence_collect(args: argparse.Namespace) -> int:
    if args.source != "akshare" or args.scope not in ("market", "sentiment", "sectors", "stocks"):
        raise EvidenceSnapshotError(
            "当前仅支持 source=akshare 且 scope=market、sentiment、sectors 或 stocks 的最小采集。"
        )

    output_path = collect_akshare_evidence_snapshot(
        args.date,
        scope=args.scope,
        output_dir=args.output_dir,
        database_path=args.database,
        refresh=args.refresh,
    )
    snapshot = check_evidence_snapshot(args.date, snapshot_dir=args.output_dir)
    write_evidence_log(
        command="evidence collect",
        trade_date=args.date,
        source_file=Path(f"akshare:{args.scope}"),
        output_path=output_path,
        missing_fields=snapshot.missing_fields,
    )
    print(f"已生成 AKShare 证据快照：{output_path}")
    print(f"缺口数量：{len(snapshot.missing_fields)}")
    for missing_field in snapshot.missing_fields:
        print(f"- {missing_field}")
    return 0


# 单日采集必须由用户明确列出 scope；部分失败时继续其余 scope 并用非零状态提示未完成。
def handle_evidence_collect_daily(args: argparse.Namespace) -> int:
    results = collect_akshare_evidence_scopes(
        args.date,
        scopes=args.scope,
        output_dir=args.output_dir,
        database_path=args.database,
        refresh=args.refresh,
    )
    successful_results = [result for result in results if result.is_successful]
    failed_results = [result for result in results if not result.is_successful]
    for result in results:
        if result.is_successful:
            print(f"scope={result.scope}：成功，快照={result.output_path}")
        else:
            print(f"scope={result.scope}：失败，原因={result.error_message}", file=sys.stderr)
    write_daily_collection_log(args.date, args.scope, successful_results, failed_results, args.output_dir, args.database)
    print(f"每日采集汇总：成功 {len(successful_results)} 个，失败 {len(failed_results)} 个")
    return 1 if failed_results else 0


# 检查命令展示标准缺口名称，后续日报可以直接复用这些缺口。
def handle_evidence_check(args: argparse.Namespace) -> int:
    snapshot = check_evidence_snapshot(args.date, snapshot_dir=args.snapshot_dir)
    print(f"证据快照：{args.snapshot_dir / f'{args.date}_snapshot.json'}")
    print(f"数据来源：{snapshot.source}")
    print(f"样本日期：{snapshot.sample_date}")
    print(f"缺口数量：{len(snapshot.missing_fields)}")
    for missing_field in snapshot.missing_fields:
        print(f"- {missing_field}")
    return 0


def write_daily_collection_log(
    trade_date: str,
    requested_scopes: list[str],
    successful_results: list[object],
    failed_results: list[object],
    output_dir: Path,
    database_path: Path,
) -> None:
    log_path = Path("logs") / "stock_review.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.evidence")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=evidence collect-daily trade_date=%s requested_scopes=%s successful_scopes=%s "
            "failed_scopes=%s output_dir=%s database=%s status=%s",
            trade_date,
            ",".join(requested_scopes),
            ",".join(result.scope for result in successful_results),
            ",".join(result.scope for result in failed_results),
            output_dir,
            database_path,
            "partial_failed" if failed_results else "created",
        )
    finally:
        logger.removeHandler(handler)
        handler.close()


def write_evidence_log(
    command: str,
    trade_date: str,
    source_file: Path,
    output_path: Path,
    missing_fields: tuple[str, ...],
    log_path: Path = Path("logs") / "stock_review.log",
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.evidence")
    logger.setLevel(logging.INFO)

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=%s trade_date=%s source_file=%s output=%s missing_fields=%s status=created",
            command,
            trade_date,
            source_file,
            output_path,
            ",".join(missing_fields),
        )
    finally:
        logger.removeHandler(handler)
        handler.close()


def handle_pool_add_watch(args: argparse.Namespace) -> int:
    return handle_pool_add(args, "watch")


def handle_pool_add_hot(args: argparse.Namespace) -> int:
    return handle_pool_add(args, "hot")


# 池子写入只记录用户手工维护对象，不生成任何核心票或走坏判断。
def handle_pool_add(args: argparse.Namespace, pool_type: str) -> int:
    item = add_pool_item(
        pool_type=pool_type,
        code=args.code,
        name=args.name,
        start_date=args.date,
        reason=args.reason,
        exchange=args.exchange,
        sectors=tuple(args.sector),
        note=args.note,
        database_path=args.database,
    )
    write_pool_log("pool add", item.pool_type, item.code, item.start_date, args.database)
    print(f"已加入{pool_label_for_output(item.pool_type)}：{item.code} {item.name}")
    print(f"状态：{item.status}")
    print(f"开始日期：{item.start_date}")
    return 0


def handle_pool_list(args: argparse.Namespace) -> int:
    items = list_pool_items(args.type, record_kind=args.record_kind, database_path=args.database)
    if not items:
        print("暂无池子记录。")
        return 0

    for item in items:
        print(
            f"{pool_label_for_output(item.pool_type)} | {item.code} | {item.name} | {item.exchange} | "
            f"{'、'.join(item.sectors)} | {item.status} | {item.record_kind} | {item.start_date} | {item.reason or '无'}"
        )
    return 0


# 迁移只保留已有对象及其原板块为第一条人工确认关联，不新增股票、不修改状态或记录类型。
def handle_pool_migrate_sectors(args: argparse.Namespace) -> int:
    migrated_count = migrate_pool_item_sectors(database_path=args.database)
    write_pool_migration_log(migrated_count, args.database)
    print(f"已完成池子多板块结构迁移：{migrated_count} 条对象记录。")
    return 0


# 类型变更必须留下本地审计日志，避免样例和真实记录在后续计划中再次混用。
def handle_pool_update_record_kind(args: argparse.Namespace) -> int:
    item = update_pool_item_record_kind(
        args.type,
        args.code,
        args.record_kind,
        args.reason,
        database_path=args.database,
    )
    write_pool_record_kind_log(item.pool_type, item.code, item.record_kind, args.reason, args.database)
    print(f"已更新{pool_label_for_output(item.pool_type)}记录类型：{item.code} {item.name} -> {item.record_kind}")
    return 0


def write_pool_log(command: str, pool_type: str, code: str, start_date: str, database_path: Path) -> None:
    log_path = Path("logs") / "stock_review.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.pool")
    logger.setLevel(logging.INFO)

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=%s pool_type=%s code=%s start_date=%s database=%s status=created",
            command,
            pool_type,
            code,
            start_date,
            database_path,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()


def write_pool_status_log(pool_type: str, code: str, status: str, reason: str, database_path: Path) -> None:
    log_path = Path("logs") / "stock_review.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.pool")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=pool update-status pool_type=%s code=%s status=%s reason=%s database=%s",
            pool_type,
            code,
            status,
            reason,
            database_path,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()


def write_pool_migration_log(migrated_count: int, database_path: Path) -> None:
    log_path = Path("logs") / "stock_review.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.pool")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info("command=pool migrate-sectors database=%s migrated_count=%s status=completed", database_path, migrated_count)
    finally:
        logger.removeHandler(handler)
        handler.close()


def write_pool_record_kind_log(
    pool_type: str, code: str, record_kind: str, reason: str, database_path: Path
) -> None:
    log_path = Path("logs") / "stock_review.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.pool")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=pool update-record-kind pool_type=%s code=%s record_kind=%s reason=%s database=%s",
            pool_type,
            code,
            record_kind,
            reason,
            database_path,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()


def pool_label_for_output(pool_type: str) -> str:
    return "关注池" if pool_type == "watch" else "热点池"


# 计划生成只输出观察条件模板，不输出买卖指令或自动交易建议。
def handle_plan_create(args: argparse.Namespace) -> int:
    output_path = create_trade_plan(
        args.date,
        review_path=args.review,
        evidence_path=args.evidence,
        database_path=args.database,
        output_dir=args.output_dir,
    )
    print(f"已生成次日观察计划：{output_path}")
    return 0


# 候选命令只读取本地证据事实，最终是否入池仍由用户通过 add-watch 或 add-hot 显式确认。
def handle_pool_candidates(args: argparse.Namespace) -> int:
    summary = build_pool_candidate_summary(args.date, args.snapshot_dir)
    print(render_pool_candidate_summary(summary), end="")
    return 0


# 池子状态只能通过显式 CLI 变更，移出记录仍保留，避免丢失后续复盘证据。
def handle_pool_update_status(args: argparse.Namespace) -> int:
    item = update_pool_item_status(
        pool_type=args.type,
        code=args.code,
        status=args.status,
        reason=args.reason,
        note=args.note,
        database_path=args.database,
    )
    write_pool_status_log(item.pool_type, item.code, item.status, args.reason, args.database)
    print(f"已更新{pool_label_for_output(item.pool_type)}：{item.code} {item.name}")
    print(f"状态：{item.status}")
    return 0


def handle_pool_history(args: argparse.Namespace) -> int:
    events = list_pool_item_status_history(args.type, args.code, database_path=args.database)
    if not events:
        print("暂无状态历史记录。")
        return 0
    for event in events:
        print(f"{event.changed_at} | {event.status} | {event.reason} | {event.note or '无'}")
    return 0


# 板块历史命令只汇总本地快照，不联网、不修改池子，也不认定核心板块。
def handle_evidence_sector_history(args: argparse.Namespace) -> int:
    output_path, summary = create_sector_history_report(
        args.start,
        args.end,
        snapshot_dir=args.snapshot_dir,
        output_dir=args.output_dir,
    )
    print(f"已生成板块历史事实报告：{output_path}")
    print(f"快照日期数：{len(summary.snapshot_dates)}")
    print(f"有效板块证据日期数：{len(summary.sector_evidence_dates)}")
    if not summary.has_enough_history:
        print("近期候选状态：数据不足，不认定核心板块")
    else:
        print(f"近期反复活跃候选数：{len(summary.repeated_candidates)}")
    return 0


# hhxg 写入前由应用服务核验返回日期，CLI 不自行解释或修正交易日期。
def handle_evidence_collect_hhxg(args: argparse.Namespace) -> int:
    output_path = collect_hhxg_evidence_snapshot(args.date, args.output_dir, args.database)
    snapshot = check_evidence_snapshot(args.date, snapshot_dir=args.output_dir)
    write_evidence_log(
        command="evidence collect-hhxg",
        trade_date=args.date,
        source_file=Path("hhxg:latest_snapshot"),
        output_path=output_path,
        missing_fields=snapshot.missing_fields,
    )
    print(f"已生成 hhxg Evidence Snapshot：{output_path}")
    return 0


# 真实池历史采集由用户显式指定日期和来源，输出独立事实文件，不修改池子或生成交易结论。
def handle_evidence_collect_pool_history(args: argparse.Namespace) -> int:
    output_path = collect_real_pool_stock_history(args.date, args.output_dir, args.database)
    print(f"已生成真实池个股历史事实：{output_path}")
    return 0


# 就绪检查只读取本地快照，明确阻止不足五个有效交易日时提前进行历史分析。
def handle_evidence_history_readiness(args: argparse.Namespace) -> int:
    readiness = check_hhxg_history_readiness(args.start, args.end, args.snapshot_dir)
    print(render_hhxg_history_readiness(readiness), end="")
    return 0


# hhxg 历史报告只输出可追溯事实；五日门槛不足时必须停在数据不足状态。
def handle_evidence_hhxg_history(args: argparse.Namespace) -> int:
    output_path, summary = create_hhxg_history_report(
        args.start,
        args.end,
        snapshot_dir=args.snapshot_dir,
        output_dir=args.output_dir,
    )
    print(f"已生成 hhxg 历史事实报告：{output_path}")
    print(f"已验证有效交易日：{len(summary.daily_facts)}")
    if not summary.has_enough_history:
        print("历史分析状态：数据不足，不输出多日历史分析结论")
    else:
        print(f"多日重复排行事实数：{len(summary.repeated_rank_facts)}")
    return 0


# 市场历史命令只读取本地快照，不联网、不写 SQLite，也不产生市场阶段结论。
def handle_evidence_market_history(args: argparse.Namespace) -> int:
    output_path, summary = create_market_history_report(
        args.start,
        args.end,
        snapshot_dir=args.snapshot_dir,
        output_dir=args.output_dir,
    )
    print(f"已生成市场历史事实报告：{output_path}")
    print(f"快照日期数：{len(summary.snapshot_dates)}")
    print(f"有效市场证据日期数：{len(summary.market_facts)}")
    if not summary.has_enough_history:
        print("市场阶段状态：数据不足，不判断市场阶段")
    else:
        print(f"指数区间事实数：{len(summary.index_range_facts)}")
    return 0


# STEP 1-7 归纳只读取本地文件；报告固定保留待确认和不可判定，不触发任何业务状态变更。
def handle_evidence_summarize_review(args: argparse.Namespace) -> int:
    output_path, summary = create_review_facts_report(
        args.date,
        snapshot_dir=args.snapshot_dir,
        pool_history_dir=args.pool_history_dir,
        output_dir=args.output_dir,
    )
    print(f"已生成 STEP 1-7 证据事实归纳报告：{output_path}")
    print(f"Evidence Snapshot 标准缺口数：{len(summary.missing_fields)}")
    print("归纳状态：仅事实与待确认，不判断阶段、核心票或买点")
    return 0


# Observation 创建只保存用户显式输入的可验证判断，不从证据或计划自动生成。
def handle_observation_add(args: argparse.Namespace) -> int:
    observation = add_observation(
        review_date=args.date,
        topic=args.topic,
        related_target=args.target,
        hypothesis=args.hypothesis,
        confirmation_condition=args.confirmation,
        invalidation_condition=args.invalidation,
        evidence_source=args.evidence_source,
        plan_item=args.plan_item,
        database_path=args.database,
    )
    write_observation_log("observation add", observation.observation_id, observation.status, args.database)
    print(f"已创建 Observation：{observation.observation_id}")
    print(f"状态：{observation.status}")
    return 0


def handle_observation_list(args: argparse.Namespace) -> int:
    observations = list_observations(args.date, args.status, database_path=args.database)
    if not observations:
        print("暂无 Observation 记录。")
        return 0
    for observation in observations:
        print(
            f"{observation.observation_id} | {observation.review_date} | {observation.status} | "
            f"{observation.topic} | {observation.related_target} | {observation.hypothesis}"
        )
    return 0


# 回填命令更新唯一结果记录，invalid 状态由业务模型明确排除在经验候选之外。
def handle_observation_review(args: argparse.Namespace) -> int:
    observation = review_observation(
        observation_id=args.id,
        status=args.status,
        actual_result=args.result,
        review_note=args.note,
        database_path=args.database,
    )
    write_observation_log("observation review", observation.observation_id, observation.status, args.database)
    print(f"已回填 Observation：{observation.observation_id}")
    print(f"状态：{observation.status}")
    return 0


def write_observation_log(command: str, observation_id: str, status: str, database_path: Path) -> None:
    log_path = Path("logs") / "stock_review.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.observation")
    logger.setLevel(logging.INFO)

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=%s observation_id=%s status=%s database=%s",
            command,
            observation_id,
            status,
            database_path,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()


# 周度学习只读取已保存 Observation，生成经验候选但不修改复盘规则。
def handle_learning_weekly(args: argparse.Namespace) -> int:
    output_path = create_weekly_learning(
        args.start,
        args.end,
        database_path=args.database,
        output_dir=args.output_dir,
    )
    print(f"已生成周度学习总结：{output_path}")
    return 0


# 评分命令只消费标准证据并输出可解释规则，不改变池子、计划或 Observation。
def handle_scoring_create(args: argparse.Namespace) -> int:
    output_path = create_scoring_report(args.date, args.evidence, args.output_dir)
    write_scoring_log(args.date, args.evidence, output_path)
    print(f"已生成可解释规则评分：{output_path}")
    return 0


def write_scoring_log(trade_date: str, evidence_path: Path, output_path: Path) -> None:
    log_path = Path("logs") / "stock_review.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.scoring")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=scoring create trade_date=%s evidence=%s output=%s status=created",
            trade_date,
            evidence_path,
            output_path,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
