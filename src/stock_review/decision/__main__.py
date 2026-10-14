# 本文件是决策卡的 CLI 入口，只做参数解析与结果输出，判定逻辑全部在应用层。
# 用法：python -m stock_review.decision --date 2026-10-09
#       python -m stock_review.decision --from-snapshot data/decision/2026-10-09.json

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from stock_review.decision.collect_decision_inputs import DEFAULT_LOOKBACK_DAYS
from stock_review.decision.run_decision_card import (
    DEFAULT_DECISION_DATA_DIR,
    DEFAULT_DECISION_REPORT_DIR,
    run_decision_card,
    run_decision_card_from_snapshot,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m stock_review.decision",
        description="按 stock-review.md 框架生成盘后决策卡：市场情绪、参与倾向、候选对象与仓位区间。",
    )
    parser.add_argument("--date", help="已收盘交易日，格式 YYYY-MM-DD；用 --from-snapshot 时可省略。")
    parser.add_argument(
        "--from-snapshot",
        help="从既有 data/decision/<trade-date>.json 快照重渲染，不重新采集任何外部数据。",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"情绪序列回看交易日数，默认 {DEFAULT_LOOKBACK_DAYS}；仅采集模式生效。",
    )
    parser.add_argument("--report-dir", default=str(DEFAULT_DECISION_REPORT_DIR), help="决策卡输出目录。")
    parser.add_argument("--data-dir", default=str(DEFAULT_DECISION_DATA_DIR), help="JSON 快照输出目录。")
    parser.add_argument("--stdout", action="store_true", help="把决策卡同时打印到标准输出。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    snapshot_path = Path(args.from_snapshot) if args.from_snapshot else None

    # 交易日必须是合法日期；不在此处校验是否为交易日，采集结果会如实反映。
    if args.date:
        try:
            date.fromisoformat(args.date)
        except ValueError:
            print(f"交易日格式不合法：{args.date}，应为 YYYY-MM-DD。")
            return 2

    if snapshot_path is not None:
        if not snapshot_path.is_file():
            print(f"快照文件不存在：{snapshot_path}")
            return 2
        result = run_decision_card_from_snapshot(
            snapshot_path,
            report_dir=Path(args.report_dir),
            data_dir=Path(args.data_dir),
        )
        # 同时给了 --date 时必须与快照交易日一致，避免把重渲染结果错当成另一交易日的卡。
        if args.date and args.date != result["trade_date"]:
            print(f"--date（{args.date}）与快照交易日（{result['trade_date']}）不一致，已终止。")
            return 2
        print("模式：从既有快照重渲染（未重新采集外部数据）")
    else:
        if not args.date:
            print("需要 --date，或用 --from-snapshot 指定既有快照。")
            return 2
        result = run_decision_card(
            args.date,
            report_dir=Path(args.report_dir),
            data_dir=Path(args.data_dir),
            lookback_days=args.lookback_days,
        )

    answers = result["answers"]
    print(f"交易日：{result['trade_date']}")
    print(f"市场情绪：{answers['sentiment']}（情绪温度 {answers['sentiment_temperature']}）")
    print(f"分歧质量：{answers['divergence_quality']}")
    print(f"指数阶段：{answers['market_stage']}；量能档位：{answers['amount_band']}")
    print(f"止跌迹象：{answers['stabilization']}")
    print(f"是否参与：{answers['participation']}")
    print(f"仓位区间：{answers['position_range'][0]}%–{answers['position_range'][1]}%")
    print(f"主线方向：{answers['main_line']}；连板候选 {answers['candidate_count']} 只")
    if result["gaps"]:
        print("数据缺口：")
        for item in result["gaps"]:
            print(f"  - {item}")
    print(f"决策卡：{result['report_path']}")
    print(f"快照：{result['data_path']}")

    if args.stdout:
        print()
        print(result["markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
