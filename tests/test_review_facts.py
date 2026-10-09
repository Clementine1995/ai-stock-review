from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.evidence.manage_evidence_snapshot import EvidenceSnapshotError
from stock_review.evidence.summarize_review_facts import (
    StepEvidence,
    build_step_evidence,
    create_review_facts_report,
    render_review_facts,
    summarize_review_facts,
)


class ReviewFactsTest(unittest.TestCase):
    def test_report_renders_sources_facts_and_non_deterministic_boundaries(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-13")
            self.write_pool_history(root, "2026-07-13")

            output_path, summary = create_review_facts_report(
                "2026-07-13", snapshot_dir=root, pool_history_dir=root, output_dir=root / "reports"
            )
            content = output_path.read_text(encoding="utf-8")

            self.assertEqual(summary.field_sources["market"], "akshare")
            self.assertEqual(len(summary.pool_history_records), 1)
            self.assertIn("市场字段来源：akshare", content)
            self.assertIn("002829 星网宇达：5 日 4.2%，20 日 12.5%", content)
            self.assertIn("炸板率比例 0.3", content)
            self.assertIn("本地允许事实字段：指数事实、成交额事实", content)
            self.assertIn("真实池 5 日涨跌幅", content)
            self.assertIn("归纳结果：不可判定", content)
            self.assertIn("不能认定核心票", content)
            self.assertIn("不自动加入、暂停、移出或修改任何池子", content)

    def test_summary_keeps_missing_pool_history_as_gap(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-13")

            summary = summarize_review_facts("2026-07-13", root, root)

            self.assertTrue(summary.pool_history_missing)
            self.assertEqual(summary.pool_history_records, ())

    def test_step_mapping_limits_pool_history_to_step_six_and_step_seven(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-13")
            self.write_pool_history(root, "2026-07-13")

            step_evidence = build_step_evidence(summarize_review_facts("2026-07-13", root, root))

            self.assertNotIn("pool_history.return_5d_percent", step_evidence[5].allowed_field_keys)
            self.assertIn("pool_history.return_5d_percent", step_evidence[6].allowed_field_keys)
            self.assertIn("pool_history.return_20d_percent", step_evidence[7].allowed_field_keys)
            self.assertIn("stocks.relative_comparison", step_evidence[6].allowed_field_keys)
            self.assertIn("stocks.relative_comparison", step_evidence[7].allowed_field_keys)
            self.assertIn("missing_stock_identity", step_evidence[6].hard_missing_fields)
            self.assertIn("missing_market_history", step_evidence[1].hard_missing_fields)
            self.assertIn("missing_sentiment_history", step_evidence[3].hard_missing_fields)
            self.assertIn("missing_sector_history", step_evidence[4].hard_missing_fields)
            self.assertIn("missing_stock_identity", step_evidence[7].hard_missing_fields)
            self.assertIn("missing_relative_strength", step_evidence[7].hard_missing_fields)
            self.assertIn("missing_continuous_comparison", step_evidence[7].hard_missing_fields)
            self.assertIn("missing_auction", step_evidence[7].hard_missing_fields)

    def test_relative_comparison_gap_requires_complete_coverage(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            complete_fact = {
                "completeness": "complete",
                "direction": "相对偏强",
            }
            incomplete_fact = {
                "completeness": "incomplete",
                "reason": "候选历史缺该个股",
            }
            self.write_snapshot(
                root,
                "2026-07-13",
                stocks=[
                    {
                        "code": "002829",
                        "name": "星网宇达",
                        "identity_verified": True,
                        "relative_strength": complete_fact,
                        "continuous_comparison": complete_fact,
                    },
                    {
                        "code": "603580",
                        "name": "艾艾精工",
                        "identity_verified": True,
                        "relative_strength": incomplete_fact,
                        "continuous_comparison": incomplete_fact,
                    },
                ],
            )

            partial_evidence = build_step_evidence(
                summarize_review_facts("2026-07-13", root, root)
            )

            self.assertIn(
                "missing_relative_strength",
                partial_evidence[6].hard_missing_fields,
            )
            self.assertIn(
                "missing_continuous_comparison",
                partial_evidence[7].hard_missing_fields,
            )

            self.write_snapshot(
                root,
                "2026-07-13",
                stocks=[
                    {
                        "code": "002829",
                        "name": "星网宇达",
                        "identity_verified": True,
                        "relative_strength": complete_fact,
                        "continuous_comparison": complete_fact,
                    },
                    {
                        "code": "603580",
                        "name": "艾艾精工",
                        "identity_verified": True,
                        "relative_strength": complete_fact,
                        "continuous_comparison": complete_fact,
                    },
                ],
            )

            complete_evidence = build_step_evidence(
                summarize_review_facts("2026-07-13", root, root)
            )

            self.assertNotIn(
                "missing_relative_strength",
                complete_evidence[6].hard_missing_fields,
            )
            self.assertNotIn(
                "missing_continuous_comparison",
                complete_evidence[7].hard_missing_fields,
            )

    def test_five_snapshot_history_keeps_continuity_gap(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            dates = ("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10")
            for offset, trade_date in enumerate(dates):
                self.write_snapshot(root, trade_date, event_source="hhxg", index_close=3900 + offset)
            self.write_pool_history(root, "2026-07-10")

            summary = summarize_review_facts("2026-07-10", root, root)
            step_evidence = build_step_evidence(summary)
            output_path, _ = create_review_facts_report(
                "2026-07-10", snapshot_dir=root, pool_history_dir=root, output_dir=root / "reports"
            )
            content = output_path.read_text(encoding="utf-8")

            self.assertIn("market.history_range", step_evidence[1].allowed_field_keys)
            self.assertNotIn("missing_market_history", step_evidence[1].hard_missing_fields)
            self.assertIn("missing_continuous_market_history", step_evidence[1].hard_missing_fields)
            self.assertIn("sentiment.history_style", step_evidence[2].allowed_field_keys)
            self.assertIn("sentiment.history_cycle", step_evidence[3].allowed_field_keys)
            self.assertIn("missing_sentiment_history", step_evidence[3].hard_missing_fields)
            self.assertIn("sectors.history_repeated_rankings", step_evidence[4].allowed_field_keys)
            self.assertNotIn("missing_sector_history", step_evidence[4].hard_missing_fields)
            self.assertNotIn("market.history_range", step_evidence[5].allowed_field_keys)
            self.assertIn("最近 5 个本地有效快照样本", content)
            self.assertIn("连续性未验证", content)
            self.assertIn(
                "上证指数样本收盘路径 2026-07-06 收盘 3900 → 2026-07-07 收盘 3901 → "
                "2026-07-08 收盘 3902 → 2026-07-09 收盘 3903 → 2026-07-10 收盘 3904",
                content,
            )
            self.assertNotIn("连续市场事实：上证指数 5 日区间", content)
            self.assertIn("多日短线情绪事实：2026-07-06", content)
            self.assertIn("多日重复排行事实：军民融合", content)

    def test_tongdaxin_daily_bar_history_replaces_sparse_snapshot_range(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            for offset, trade_date in enumerate(
                ("2026-07-21", "2026-07-22", "2026-07-23", "2026-07-28", "2026-08-07")
            ):
                self.write_snapshot(root, trade_date, index_close=3900 + offset)
            records = []
            for offset, sample_date in enumerate(
                ("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07")
            ):
                records.append(
                    {
                        "sample_date": sample_date,
                        "indices": [
                            {"symbol": "sh000001", "name": "上证指数", "close": 3900 + offset},
                            {"symbol": "sz399001", "name": "深证成指", "close": 14000 + offset},
                            {"symbol": "sz399006", "name": "创业板指", "close": 3500 + offset},
                        ],
                    }
                )
            history_path = root / "2026-08-07_tdx_index_history.json"
            history_path.write_text(
                json.dumps(
                    {
                        "trade_date": "2026-08-07",
                        "sample_date": "2026-08-07",
                        "source": "workbuddy:tongdaxin.tdx_kline",
                        "quality": "connector_daily_kline_sequence",
                        "raw_reference": str(root / "raw.json"),
                        "sample_dates": [record["sample_date"] for record in records],
                        "records": records,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            summary = summarize_review_facts("2026-08-07", root, root)
            step_evidence = build_step_evidence(summary)
            content = render_review_facts(summary)

            self.assertTrue(summary.market_history.has_verified_daily_sequence)
            self.assertNotIn(
                "missing_continuous_market_history",
                step_evidence[1].hard_missing_fields,
            )
            self.assertIn("missing_sentiment_history", step_evidence[3].hard_missing_fields)
            self.assertEqual(
                summary.market_history.evidence_references,
                (str(history_path), str(root / "raw.json")),
            )
            self.assertIn(
                "上证指数完整收盘路径 2026-08-03 收盘 3900 → 2026-08-04 收盘 3901 → "
                "2026-08-05 收盘 3902 → 2026-08-06 收盘 3903 → 2026-08-07 收盘 3904",
                content,
            )
            self.assertIn(
                "深证成指完整收盘路径 2026-08-03 收盘 14000 → 2026-08-04 收盘 14001 → "
                "2026-08-05 收盘 14002 → 2026-08-06 收盘 14003 → 2026-08-07 收盘 14004",
                content,
            )

    def test_sentiment_history_requires_same_dates_as_verified_index_sequence(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            dates = ("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07")
            records = []
            for offset, trade_date in enumerate(dates):
                self.write_snapshot(
                    root,
                    trade_date,
                    event_source="hhxg",
                    index_close=3900 + offset,
                )
                records.append(
                    {
                        "sample_date": trade_date,
                        "indices": [
                            {"symbol": "sh000001", "name": "上证指数", "close": 3900 + offset},
                            {"symbol": "sz399001", "name": "深证成指", "close": 14000 + offset},
                            {"symbol": "sz399006", "name": "创业板指", "close": 3500 + offset},
                        ],
                    }
                )
            (root / "2026-08-07_tdx_index_history.json").write_text(
                json.dumps(
                    {
                        "trade_date": "2026-08-07",
                        "sample_date": "2026-08-07",
                        "source": "workbuddy:tongdaxin.tdx_kline",
                        "quality": "connector_daily_kline_sequence",
                        "raw_reference": str(root / "raw.json"),
                        "sample_dates": list(dates),
                        "records": records,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            step_evidence = build_step_evidence(
                summarize_review_facts("2026-08-07", root, root)
            )

            self.assertNotIn(
                "missing_sentiment_history",
                step_evidence[3].hard_missing_fields,
            )

    def test_verified_board_roles_build_complete_ladder_and_clear_gap(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            stocks = [
                {
                    "code": "300001",
                    "name": "五板股",
                    "role": "连板事实候选",
                    "role_source": "涨停池连板股（5板）",
                    "source": "akshare:stock_zt_pool_em",
                },
                {
                    "code": "300002",
                    "name": "三板股",
                    "role": "连板事实候选",
                    "role_source": "涨停池连板股（3板）",
                    "source": "akshare:stock_zt_pool_em",
                },
                {
                    "code": "300003",
                    "name": "二板股",
                    "role": "连板事实候选",
                    "role_source": "涨停池连板股（2板）",
                    "source": "akshare:stock_zt_pool_em",
                },
            ]
            sentiment = {
                "limit_up_count": 63,
                "limit_down_count": 9,
                "broken_board_rate": 0.3077,
                "highest_board": 5,
            }
            self.write_snapshot(root, "2026-08-14", stocks=stocks, sentiment=sentiment)

            summary = summarize_review_facts("2026-08-14", root, root)
            step_evidence = build_step_evidence(summary)

            self.assertEqual(summary.board_ladder["candidate_count"], 3)
            self.assertEqual(summary.board_ladder["completeness"], "complete")
            self.assertEqual(
                [tier["board_count"] for tier in summary.board_ladder["tiers"]],
                [5, 4, 3, 2],
            )
            self.assertEqual(
                [tier["candidate_count"] for tier in summary.board_ladder["tiers"]],
                [1, 0, 1, 1],
            )
            self.assertEqual(
                summary.board_ladder["source"], "akshare:stock_zt_pool_em"
            )
            self.assertIn(
                "sentiment.board_ladder", step_evidence[2].allowed_field_keys
            )
            self.assertNotIn(
                "missing_board_ladder", step_evidence[2].hard_missing_fields
            )
            self.assertIn(
                "missing_large_cap_trend", step_evidence[2].hard_missing_fields
            )

    def test_board_ladder_keeps_gap_when_highest_board_does_not_match(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            stocks = [
                {
                    "code": "300002",
                    "name": "三板股",
                    "role": "连板事实候选",
                    "role_source": "涨停池连板股（3板）",
                    "source": "akshare:stock_zt_pool_em",
                }
            ]
            sentiment = {
                "limit_up_count": 63,
                "limit_down_count": 9,
                "broken_board_rate": 0.3077,
                "highest_board": 5,
            }
            self.write_snapshot(root, "2026-08-14", stocks=stocks, sentiment=sentiment)

            summary = summarize_review_facts("2026-08-14", root, root)
            step_evidence = build_step_evidence(summary)

            self.assertEqual(summary.board_ladder["completeness"], "partial")
            self.assertIn(
                "missing_board_ladder", step_evidence[2].hard_missing_fields
            )

    def test_finite_candidate_history_builds_capacity_and_return_rankings(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            records = [
                {
                    "code": "300001",
                    "name": "容量甲",
                    "average_5d_amount": 1_500_000_000,
                    "circulating_market_value": 20_000_000_000,
                    "return_5d_percent": 5.0,
                    "return_20d_percent": 20.0,
                },
                {
                    "code": "300002",
                    "name": "容量乙",
                    "average_5d_amount": 900_000_000,
                    "circulating_market_value": 12_000_000_000,
                    "return_5d_percent": 10.0,
                    "return_20d_percent": 5.0,
                },
                {
                    "code": "300003",
                    "name": "弹性丙",
                    "average_5d_amount": 300_000_000,
                    "circulating_market_value": 5_000_000_000,
                    "return_5d_percent": -1.0,
                    "return_20d_percent": 30.0,
                },
            ]
            self.write_snapshot(root, "2026-08-14")
            self.write_review_candidate_history(root, "2026-08-14", records)

            summary = summarize_review_facts("2026-08-14", root, root)
            step_evidence = build_step_evidence(summary)
            ranking = summary.candidate_capacity_ranking
            content = render_review_facts(summary)

            self.assertEqual(ranking["scope"], "finite_candidate_history")
            self.assertEqual(ranking["candidate_count"], 3)
            self.assertEqual(ranking["capacity_candidate_count"], 2)
            self.assertEqual(
                [item["code"] for item in ranking["capacity_candidates"]],
                ["300001", "300002"],
            )
            self.assertEqual(
                [item["code"] for item in ranking["return_5d_ranking"]],
                ["300002", "300001", "300003"],
            )
            self.assertEqual(
                [item["code"] for item in ranking["return_20d_ranking"]],
                ["300003", "300001", "300002"],
            )
            self.assertEqual(ranking["completeness"], "complete")
            self.assertEqual(ranking["source"], "akshare:review_candidate_history")
            for step_number in (2, 6, 7):
                self.assertIn(
                    "candidate_history.capacity_ranking",
                    step_evidence[step_number].allowed_field_keys,
                )
                self.assertNotIn(
                    "missing_candidate_capacity",
                    step_evidence[step_number].hard_missing_fields,
                )
            self.assertIn(
                "missing_large_cap_trend", step_evidence[2].hard_missing_fields
            )
            self.assertIn("有限候选历史去重 3 只（非全市场）", content)

            history_path = root / "2026-08-14_review_candidate_history.json"
            invalid_payload = json.loads(history_path.read_text(encoding="utf-8"))
            invalid_payload["records"][0]["code"] = "候选甲"
            history_path.write_text(
                json.dumps(invalid_payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(EvidenceSnapshotError, "候选身份无效"):
                summarize_review_facts("2026-08-14", root, root)

    def test_incomplete_candidate_metrics_keep_capacity_gap(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            records = [
                {
                    "code": "300001",
                    "name": "容量甲",
                    "average_5d_amount": 1_500_000_000,
                    "circulating_market_value": 20_000_000_000,
                    "return_5d_percent": 5.0,
                    "return_20d_percent": None,
                }
            ]
            self.write_snapshot(root, "2026-08-14")
            self.write_review_candidate_history(root, "2026-08-14", records)

            summary = summarize_review_facts("2026-08-14", root, root)
            step_evidence = build_step_evidence(summary)

            self.assertEqual(
                summary.candidate_capacity_ranking["completeness"], "partial"
            )
            for step_number in (2, 6, 7):
                self.assertIn(
                    "missing_candidate_capacity",
                    step_evidence[step_number].hard_missing_fields,
                )

    def test_structured_market_and_sector_fields_clear_only_matching_gaps(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            market = {
                "indices": [{"name": "上证指数", "close": 3900}],
                "total_amount": 10,
                "up_count": 3200,
                "down_count": 1800,
            }
            sectors = [
                {
                    "name": "通信设备",
                    "source_type": "hhxg_strong_sector",
                    "rank": 1,
                    "net_inflow_yi": 117,
                    "up_count": 80,
                    "down_count": 5,
                    "daily_kline": [
                        {
                            "sample_date": f"2026-08-{day:02d}",
                            "close": 100 + day,
                            "amount": 1_000_000_000 + day,
                        }
                        for day in range(10, 15)
                    ],
                }
            ]
            self.write_snapshot(root, "2026-08-14", market=market, sectors=sectors)

            summary = summarize_review_facts("2026-08-14", root, root)
            step_evidence = build_step_evidence(summary)

            self.assertNotIn(
                "missing_advance_decline", step_evidence[1].hard_missing_fields
            )
            self.assertIn(
                "market.advance_decline", step_evidence[1].allowed_field_keys
            )
            self.assertEqual(
                summary.evidence_references["market.advance_decline"],
                (str(root / "2026-08-14_snapshot.json"),),
            )
            self.assertIn(
                "missing_overseas_market", step_evidence[1].hard_missing_fields
            )
            market["up_count"] = 3200.5
            self.write_snapshot(root, "2026-08-14", market=market, sectors=sectors)
            fractional_step_evidence = build_step_evidence(
                summarize_review_facts("2026-08-14", root, root)
            )
            self.assertIn(
                "missing_advance_decline",
                fractional_step_evidence[1].hard_missing_fields,
            )
            self.assertNotIn(
                "missing_capital_flow", step_evidence[2].hard_missing_fields
            )
            for step_number in (4, 5):
                self.assertNotIn(
                    "missing_sector_kline",
                    step_evidence[step_number].hard_missing_fields,
                )
                self.assertNotIn(
                    "missing_sector_diffusion",
                    step_evidence[step_number].hard_missing_fields,
                )

    def test_supplemental_files_route_valid_us_session_and_longhubang_facts(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-08-14")
            self.write_review_candidate_history(root, "2026-08-14")
            (root / "2026-08-14_longhubang.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "review_date": "2026-08-14",
                        "sample_date": "2026-08-14",
                        "source": "akshare:stock_lhb_detail_em+stock_lhb_stock_detail_em",
                        "scope": "daily_list_limited_candidates",
                        "candidate_history_file": "2026-08-14_review_candidate_history.json",
                        "candidate_count": 1,
                        "public_listed_count": 30,
                        "consumed_listed_count": 1,
                        "records": [
                            {
                                "code": "002829",
                                "name": "星网宇达",
                                "listed": True,
                                "status": "listed",
                                "reason": "日涨幅偏离值达7%",
                                "buy_amount": 120_000_000,
                                "sell_amount": 80_000_000,
                                "net_amount": 40_000_000,
                                "institutional_net_amount": 10_000_000,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (root / "2026-08-14_us_market.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "review_date": "2026-08-14",
                        "generated_at": "2026-08-15T07:00:00+08:00",
                        "as_of": "2026-08-15T07:00:00+08:00",
                        "source": "akshare:index_us_stock_sina:.IXIC+.INX",
                        "window": {
                            "a_share_close_at": "2026-08-14T15:00:00+08:00",
                            "next_a_share_open_at": "2026-08-17T09:30:00+08:00",
                            "boundary": "open_interval",
                        },
                        "us_session_date": "2026-08-14",
                        "status": "available",
                        "session_close_at": "2026-08-15T04:00:00+08:00",
                        "indices": [
                            {
                                "symbol": ".IXIC",
                                "name": "纳斯达克综合指数",
                                "close": 22000,
                                "change_percent": 1.2,
                            },
                            {
                                "symbol": ".INX",
                                "name": "标普500指数",
                                "close": 6500,
                                "change_percent": 0.8,
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            summary = summarize_review_facts("2026-08-14", root, root)
            step_evidence = build_step_evidence(summary)
            content = render_review_facts(summary)

            self.assertNotIn(
                "missing_overseas_market", step_evidence[1].hard_missing_fields
            )
            self.assertIn("market.us_session", step_evidence[1].allowed_field_keys)
            self.assertNotIn(
                "missing_capital_flow", step_evidence[2].hard_missing_fields
            )
            for step_number in (2, 6, 7):
                self.assertIn(
                    "stocks.longhubang", step_evidence[step_number].allowed_field_keys
                )
            self.assertEqual(
                summary.evidence_references["market.us_session"],
                (str(root / "2026-08-14_us_market.json"),),
            )
            self.assertIn("纳斯达克综合指数", content)
            self.assertIn("002829 星网宇达", content)
            self.assertIn("不构成负面证据", content)

            longhubang_path = root / "2026-08-14_longhubang.json"
            longhubang_payload = json.loads(longhubang_path.read_text(encoding="utf-8"))
            longhubang_payload["records"][0]["code"] = "000001"
            longhubang_path.write_text(
                json.dumps(longhubang_payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(EvidenceSnapshotError, "候选集合不一致"):
                summarize_review_facts("2026-08-14", root, root)

            longhubang_payload["records"][0]["code"] = "002829"
            longhubang_path.write_text(
                json.dumps(longhubang_payload, ensure_ascii=False), encoding="utf-8"
            )
            us_market_path = root / "2026-08-14_us_market.json"
            us_market_payload = json.loads(us_market_path.read_text(encoding="utf-8"))
            us_market_payload["session_close_at"] = "2026-08-14T14:00:00+08:00"
            us_market_path.write_text(
                json.dumps(us_market_payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(EvidenceSnapshotError, "时点边界无效"):
                summarize_review_facts("2026-08-14", root, root)

            us_market_payload["session_close_at"] = "2026-08-15T04:00:00+08:00"
            us_market_payload["window"]["a_share_close_at"] = "2026-08-13T15:00:00+08:00"
            us_market_path.write_text(
                json.dumps(us_market_payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(EvidenceSnapshotError, "时点边界无效"):
                summarize_review_facts("2026-08-14", root, root)

    def test_pending_us_session_keeps_overseas_gap_without_stale_fallback(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-08-14")
            (root / "2026-08-14_us_market.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "review_date": "2026-08-14",
                        "generated_at": "2026-08-14T20:00:00+08:00",
                        "as_of": "2026-08-14T20:00:00+08:00",
                        "source": "akshare:index_us_stock_sina:.IXIC+.INX",
                        "window": {
                            "a_share_close_at": "2026-08-14T15:00:00+08:00",
                            "next_a_share_open_at": "2026-08-17T09:30:00+08:00",
                            "boundary": "open_interval",
                        },
                        "us_session_date": None,
                        "status": "pending",
                        "session_close_at": None,
                        "indices": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            summary = summarize_review_facts("2026-08-14", root, root)
            step_evidence = build_step_evidence(summary)
            content = render_review_facts(summary)

            self.assertIn(
                "missing_overseas_market", step_evidence[1].hard_missing_fields
            )
            self.assertIn("不回退使用 A 股收盘前的旧会话", content)

    def test_exact_sector_kline_file_routes_steps_and_clears_only_kline_gap(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(
                root,
                "2026-08-14",
                sectors=[
                    {
                        "name": "通信设备",
                        "source_type": "hhxg_strong_sector",
                        "rank": 1,
                    },
                    {
                        "name": "通信技术",
                        "source_type": "hhxg_hot_theme",
                        "rank": 1,
                    },
                ],
            )
            bars = [
                {
                    "date": f"2026-08-{day:02d}",
                    "open": 100 + day,
                    "high": 102 + day,
                    "low": 99 + day,
                    "close": 101 + day,
                    "volume": 10_000 + day,
                    "amount": 100_000_000 + day,
                }
                for day in range(10, 15)
            ]
            (root / "2026-08-14_sector_kline.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "review_date": "2026-08-14",
                        "sample_date": "2026-08-14",
                        "source": "akshare:ranked_sector_daily_kline",
                        "scope": "ranked_sector_kline_limited",
                        "lookback_bars": 5,
                        "max_objects": 3,
                        "selection_basis": "positive_source_type_then_rank_then_snapshot_order_then_exact_unique_name",
                        "eligible_source_types": [
                            "concept",
                            "hhxg_hot_theme",
                            "hhxg_strong_concept",
                            "hhxg_strong_industry",
                            "hhxg_strong_sector",
                            "industry",
                            "industry_ths",
                        ],
                        "snapshot_file": "2026-08-14_snapshot.json",
                        "selected_object_count": 2,
                        "available_count": 1,
                        "unavailable_count": 1,
                        "ambiguous_count": 0,
                        "records": [
                            {
                                "name": "通信设备",
                                "source_type": "hhxg_strong_sector",
                                "ranking_group": None,
                                "rank": 1,
                                "snapshot_position": 0,
                                "mapping_basis": "exact_unique_name",
                                "status": "available",
                                "resolved_board_type": "industry",
                                "resolved_board_code": "BK1036",
                                "history_source": "akshare:stock_board_industry_hist_em",
                                "bar_count": 5,
                                "bars": bars,
                            },
                            {
                                "name": "通信技术",
                                "source_type": "hhxg_hot_theme",
                                "ranking_group": None,
                                "rank": 1,
                                "snapshot_position": 1,
                                "mapping_basis": "exact_unique_name",
                                "status": "unavailable",
                                "resolved_board_type": None,
                                "resolved_board_code": None,
                                "history_source": None,
                                "bar_count": 0,
                                "bars": [],
                                "note": "名称未精确命中。",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            summary = summarize_review_facts("2026-08-14", root, root)
            step_evidence = build_step_evidence(summary)
            content = render_review_facts(summary)

            for step_number in (4, 5):
                self.assertIn(
                    "sectors.daily_kline", step_evidence[step_number].allowed_field_keys
                )
                self.assertNotIn(
                    "missing_sector_kline", step_evidence[step_number].hard_missing_fields
                )
                self.assertIn(
                    "missing_sector_diffusion",
                    step_evidence[step_number].hard_missing_fields,
                )
            self.assertEqual(
                summary.evidence_references["sectors.daily_kline"],
                (str(root / "2026-08-14_sector_kline.json"),),
            )
            self.assertIn("通信设备", content)
            self.assertIn("唯一映射为 industry BK1036", content)
            self.assertIn("2026-08-14 开 114", content)

            sector_kline_path = root / "2026-08-14_sector_kline.json"
            sector_kline_payload = json.loads(
                sector_kline_path.read_text(encoding="utf-8")
            )
            sector_kline_payload["records"][0]["source_type"] = "other"
            sector_kline_path.write_text(
                json.dumps(sector_kline_payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(EvidenceSnapshotError, "记录无效"):
                summarize_review_facts("2026-08-14", root, root)

    def test_summary_rejects_snapshot_with_different_sample_date(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-13", sample_date="2026-07-10")

            with self.assertRaisesRegex(EvidenceSnapshotError, "样本日期与交易日期不一致"):
                summarize_review_facts("2026-07-13", root, root)

    def test_step_status_distinguishes_partial_from_unavailable(self):
        self.assertEqual(
            StepEvidence(1, (), ("missing_total_amount",)).status,
            "partial",
        )
        self.assertEqual(
            StepEvidence(1, (), ("missing_indices",)).status,
            "unavailable",
        )
        self.assertEqual(StepEvidence(1, (), ()).status, "pending_confirmation")

    def test_review_candidate_history_covers_real_pool_history_gap(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-13")
            self.write_review_candidate_history(root, "2026-07-13")

            step_evidence = build_step_evidence(
                summarize_review_facts("2026-07-13", root, root)
            )

            self.assertNotIn(
                "missing_real_pool_history",
                step_evidence[6].hard_missing_fields,
            )
            self.assertNotIn(
                "missing_real_pool_history",
                step_evidence[7].hard_missing_fields,
            )

    @staticmethod
    def write_snapshot(
        root: Path,
        trade_date: str,
        sample_date: str | None = None,
        event_source: str | None = None,
        index_close: float = 3900,
        market: dict | None = None,
        sentiment: dict | None = None,
        sectors: list[dict] | None = None,
        stocks: list[dict] | None = None,
    ) -> None:
        payload = {
            "trade_date": trade_date,
            "sample_date": sample_date or trade_date,
            "source": "akshare",
            "market": market if market is not None else {"indices": [{"name": "上证指数", "close": index_close, "change_percent": -1.2}], "total_amount": 10},
            "sentiment": sentiment if sentiment is not None else {"limit_up_count": 90, "limit_down_count": 4, "broken_board_rate": 0.3, "highest_board": 2},
            "sectors": sectors if sectors is not None else [{"name": "军民融合", "rank": 1, "limit_up_count": 15, "source_type": "hhxg_hot_theme"}],
            "stocks": stocks if stocks is not None else [{"code": "002829", "name": "星网宇达", "role": "个股事实候选", "role_source": "测试来源"}],
            "field_sources": {"market": "akshare", "sentiment": "hhxg", "sectors": "hhxg", "stocks": "akshare"},
            "events": [{"source": event_source, "title": "历史快照"}] if event_source else [],
        }
        (root / f"{trade_date}_snapshot.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def write_pool_history(root: Path, trade_date: str) -> None:
        payload = {
            "trade_date": trade_date,
            "sample_date": trade_date,
            "records": [{"code": "002829", "name": "星网宇达", "return_5d_percent": 4.2, "return_20d_percent": 12.5, "missing_fields": []}],
        }
        (root / f"{trade_date}_real_pool_stock_history.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def write_review_candidate_history(
        root: Path, trade_date: str, records: list[dict] | None = None
    ) -> None:
        payload = {
            "trade_date": trade_date,
            "sample_date": trade_date,
            "source": "akshare:review_candidate_history",
            "records": records if records is not None else [
                {
                    "code": "002829",
                    "name": "星网宇达",
                    "origin": "real_pool",
                    "close": 20.0,
                    "return_5d_percent": 4.2,
                    "return_20d_percent": 12.5,
                    "average_5d_amount": 500000000,
                    "circulating_market_value": 3000000000,
                    "missing_fields": [],
                }
            ],
        }
        (root / f"{trade_date}_review_candidate_history.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
