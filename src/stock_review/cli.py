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
    collect_akshare_evidence_snapshot,
    import_evidence_snapshot,
)
from stock_review.learning.summarize_weekly_learning import create_weekly_learning
from stock_review.observations.manage_observation import (
    ObservationError,
    add_observation,
    list_observations,
    review_observation,
)
from stock_review.planning.build_trade_plan import TradePlanError, create_trade_plan
from stock_review.pools.manage_pool_item import PoolItemError, add_pool_item, list_pool_items
from stock_review.review_documents.create_daily_review import create_daily_review
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
    except (EvidenceSnapshotError, FrameworkParseError, ObservationError, PoolItemError, TradePlanError) as error:
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
    evidence_collect_parser.set_defaults(handler=handle_evidence_collect)

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
    pool_list_parser.set_defaults(handler=handle_pool_list)

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
    parser.add_argument("--sector", default="", help="所属板块，缺失时标记为待确认")
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
        sector=args.sector,
        note=args.note,
        database_path=args.database,
    )
    write_pool_log("pool add", item.pool_type, item.code, item.start_date, args.database)
    print(f"已加入{pool_label_for_output(item.pool_type)}：{item.code} {item.name}")
    print(f"状态：{item.status}")
    print(f"开始日期：{item.start_date}")
    return 0


def handle_pool_list(args: argparse.Namespace) -> int:
    items = list_pool_items(args.type, database_path=args.database)
    if not items:
        print("暂无池子记录。")
        return 0

    for item in items:
        print(
            f"{pool_label_for_output(item.pool_type)} | {item.code} | {item.name} | {item.exchange} | "
            f"{item.sector} | {item.status} | {item.start_date} | {item.reason or '无'}"
        )
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
