# 本文件是核心异动票跟踪的 CLI 入口，只做参数解析与结果输出。
# 用法：
#   python -m stock_review.watchlist run --date 2026-10-14
#   python -m stock_review.watchlist candidates --date 2026-10-14 --top 30
#   python -m stock_review.watchlist pool-list
#   python -m stock_review.watchlist pool-add --code 301251 --name 威尔高 --reason "严重异常波动"
#   python -m stock_review.watchlist pool-status --code 301251 --status paused --reason "已破位"

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

from stock_review.watchlist.manage_watchlist_pool import (
    DEFAULT_POOL_PATH,
    VALID_STATUSES,
    WatchlistPoolError,
    add_item,
    pool_items,
    set_status,
)
from stock_review.watchlist.run_watchlist import (
    DEFAULT_DATA_DIR,
    DEFAULT_REPORT_DIR,
    collect_candidates_file,
    run_watchlist,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m stock_review.watchlist",
        description="核心异动票长期跟踪：位置与量价、异动认定、龙虎榜与资金流向事实。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="对跟踪池采集事实并出跟踪表。")
    run_parser.add_argument("--date", required=True, help="已收盘交易日，格式 YYYY-MM-DD。")
    run_parser.add_argument("--pool", default=str(DEFAULT_POOL_PATH), help="跟踪池 JSON 路径。")
    run_parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="跟踪表输出目录。")
    run_parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="快照输出目录。")
    run_parser.add_argument("--stdout", action="store_true", help="把跟踪表同时打印到标准输出。")

    candidate_parser = sub.add_parser("candidates", help="自动收集异动候选，供审核后入池。")
    candidate_parser.add_argument("--date", required=True, help="交易日，格式 YYYY-MM-DD。")
    candidate_parser.add_argument("--pool", default=str(DEFAULT_POOL_PATH), help="跟踪池 JSON 路径。")
    candidate_parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="候选输出目录。")
    candidate_parser.add_argument("--top", type=int, default=30, help="精算候选上限，默认 30。")

    sub.add_parser("pool-list", help="列出跟踪池成员。").add_argument(
        "--pool", default=str(DEFAULT_POOL_PATH)
    )

    add_parser = sub.add_parser("pool-add", help="把审核通过的候选加入跟踪池。")
    add_parser.add_argument("--code", required=True)
    add_parser.add_argument("--name", required=True)
    add_parser.add_argument("--reason", required=True, help="入池原因（异动档位或来源）。")
    add_parser.add_argument("--date", default=None, help="入池日期，缺省为今天。")
    add_parser.add_argument("--pool", default=str(DEFAULT_POOL_PATH))

    status_parser = sub.add_parser("pool-status", help="变更跟踪池成员状态。")
    status_parser.add_argument("--code", required=True)
    status_parser.add_argument("--status", required=True, choices=VALID_STATUSES)
    status_parser.add_argument("--reason", required=True)
    status_parser.add_argument("--pool", default=str(DEFAULT_POOL_PATH))

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pool_path = Path(args.pool)

    if args.command == "pool-list":
        items = pool_items(pool_path)
        if not items:
            print("跟踪池为空。")
            return 0
        for item in items:
            print(f"{item.code} {item.name}｜{item.board}｜{item.status}｜入池 {item.added_date}｜{item.reason}")
        return 0

    if args.command == "pool-add":
        added_date = args.date or datetime.now().date().isoformat()
        try:
            item = add_item(
                code=args.code,
                name=args.name,
                reason=args.reason,
                added_date=added_date,
                path=pool_path,
            )
        except WatchlistPoolError as error:
            print(f"入池失败：{error}")
            return 2
        print(f"已加入跟踪池：{item.code} {item.name}（{item.board}｜基准 {item.benchmark}）")
        return 0

    if args.command == "pool-status":
        try:
            item = set_status(args.code, args.status, args.reason, path=pool_path)
        except WatchlistPoolError as error:
            print(f"状态变更失败：{error}")
            return 2
        print(f"已更新：{item.code} {item.name} → {item.status}")
        return 0

    if args.date:
        try:
            date.fromisoformat(args.date)
        except ValueError:
            print(f"交易日格式不合法：{args.date}，应为 YYYY-MM-DD。")
            return 2

    if args.command == "candidates":
        payload = collect_candidates_file(
            args.date,
            pool_path=pool_path,
            data_dir=Path(args.data_dir),
            top_n=args.top,
        )
        print(f"交易日：{payload['trade_date']}")
        print(
            f"粗筛对象：{payload['seed_count']} 只；入围：{payload['gated_count']} 只；"
            f"精算：{payload['evaluated_count']} 只"
        )
        for record in payload["candidates"]:
            print(
                f"  {record['code']} {record['name']}｜{record['board']}"
                f"｜异动 {record['regulation_level']}｜最近活跃 {record.get('last_active_date') or '—'}"
                f"｜10 日偏离 {_num(record['deviation_10d'])}｜区间涨幅 {_num(record['interval_gain_percent'])}"
                f"｜龙虎榜 {record['longhubang_count']} 次{_pool_tag(record)}"
            )
        if payload["gaps"]:
            print("数据缺口：")
            for item in payload["gaps"]:
                print(f"  - {item}")
        print(f"候选清单：{payload['path']}")
        return 0

    result = run_watchlist(
        args.date,
        pool_path=pool_path,
        report_dir=Path(args.report_dir),
        data_dir=Path(args.data_dir),
    )
    print(f"交易日：{result['trade_date']}")
    print(f"跟踪对象：{result['item_count']} 只（出事实 {result['fact_count']} 只）")
    if result["reminders"]:
        print("提醒：")
        for item in result["reminders"]:
            print(f"  - {item}")
    else:
        print("提醒：本次无。")
    if result["gaps"]:
        print("数据缺口：")
        for item in result["gaps"]:
            print(f"  - {item}")
    print(f"跟踪表：{result['report_path']}")
    print(f"快照：{result['data_path']}")
    if args.stdout:
        print()
        print(result["markdown"])
    return 0


def _num(value: object) -> str:
    return "—" if value is None else f"{float(value):+.2f}%"


def _pool_tag(record: dict) -> str:
    return "｜已在池" if record.get("in_pool") else ""


if __name__ == "__main__":
    raise SystemExit(main())
