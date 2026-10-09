import base64
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_review.cli import main
from stock_review.llm.openai_compatible_client import OpenAICompatibleSettings
from stock_review.pools.manage_pool_item import add_pool_item
from stock_review.review_documents.create_detailed_review import (
    DetailedReviewError,
    build_detailed_review,
    create_detailed_review_report,
    decode_workbuddy_response_base64,
    prepare_detailed_review_request,
    render_detailed_review_request,
    render_detailed_review,
    resolve_candidate_turnover_ratio,
    select_priority_pattern_numbers,
)
from stock_review.review_documents.manage_manual_review import (
    ManualFinalResponse,
    add_manual_preview,
    add_manual_step_record,
)
from stock_review.review_framework.parse_framework import parse_framework_file
from stock_review.storage.sqlite_repository import ManualReviewRepository


class DetailedReviewTest(unittest.TestCase):
    def test_report_follows_framework_order_and_reuses_history_pool_and_patterns(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            self.write_history(root)
            self.write_pool_history(root, "2026-07-10")
            self.write_review_candidate_history(root, "2026-07-10")
            add_pool_item(
                "hot",
                "002829",
                "星网宇达",
                "2026-07-10",
                "人工确认",
                sectors=("军工",),
                database_path=database_path,
            )

            output_path = create_detailed_review_report(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                database_path,
                output_dir=root / "reports",
                log_path=root / "review.log",
                request_completion=self.valid_response,
                settings=self.settings(),
            )
            content = output_path.read_text(encoding="utf-8")
            framework = parse_framework_file(Path("stock-review.md"))
            positions = [
                content.index(f"## STEP {step.number}: {step.title}")
                for step in framework.steps
            ]

            self.assertEqual(positions, sorted(positions))
            self.assertIn("上证指数样本收盘路径", content)
            self.assertIn("5 个样本首尾变化", content)
            self.assertIn("多日板块重复排行事实：创新药", content)
            self.assertIn("002829 星网宇达 5 日涨跌幅 4.2", content)
            self.assertIn("值博率方向：rising", content)
            self.assertIn("base_evidence_available_trigger_pending", content)
            self.assertIn("603580 艾艾精工｜板块：轻工制造｜角色事实：连板事实候选", content)
            self.assertNotIn("### 次日逐候选行动计划", content)
            self.assertEqual(content.count("603580 艾艾精工｜观察｜仓位"), 0)
            self.assertIn("无可复核的逐候选行动", content)
            self.assertIn("### 模式 1：题材上升初期和发酵阶段的弹性标", content)
            self.assertIn("### 模式 12：情绪指数双低迷尾盘炸板", content)
            self.assertIn("### 大盘", content)
            self.assertIn("符合预期（本地主分档）：0% ≤ 涨跌幅 < 1%", content)
            self.assertIn("次日能否参与：不参与", content)
            self.assertIn("建议总仓位：不设置可执行仓位", content)
            self.assertIn("次日总纲：保持条件观察", content)
            self.assertIn("有限候选排序不等于全市场风格已确认", content)
            self.assertNotIn("缺少大市值趋势、容量中军、完整连板梯队", content)
            self.assertNotIn("## 指数全貌", content)
            self.assertTrue((root / "review.log").exists())

    def test_step_eight_renders_only_local_scenario_bands(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def conflicting_threshold_response(*args):
                result = self.valid_response(*args)
                result["market_preview"]["over_expectation"] = (
                    "上证指数收盘涨幅不低于 2% 且成交额高于复盘日"
                )
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=conflicting_threshold_response,
                settings=self.settings(),
            )
            content = render_detailed_review(review)

            self.assertIn("超预期（本地主分档）：涨跌幅 ≥ 1%", content)
            self.assertNotIn("模型补充说明", content)
            self.assertNotIn("上证指数收盘涨幅不低于 2%", content)

    def test_report_rejects_box_position_without_verified_boundaries(self):
        for position in ("箱体中部", "箱体上沿", "箱体下沿"):
            with self.subTest(position=position), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def unsupported_box_response(*args, position=position):
                    result = self.valid_response(*args)
                    result["step_analyses"][0]["conclusion"] = (
                        f"上证指数处于区间{position}偏强位置。"
                    )
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "箱体边界"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=unsupported_box_response,
                        settings=self.settings(),
                    )

    def test_missing_sentiment_history_rejects_directional_comparisons(self):
        for comparison in ("边际改善", "边际恶化"):
            with self.subTest(comparison=comparison), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def unsupported_comparison_response(*args, comparison=comparison):
                    result = self.valid_response(*args)
                    result["step_analyses"][2]["conclusion"] = (
                        f"短线情绪出现{comparison}迹象。"
                    )
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "连续情绪历史.*比较"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=unsupported_comparison_response,
                        settings=self.settings(),
                    )

    def test_return_evidence_cannot_be_described_as_average_price(self):
        mutations = (
            lambda result: result["step_analyses"][5].__setitem__(
                "conclusion", "未确认候选的五日均价与二十日均价表现居前。"
            ),
            lambda result: result["final_plan_draft"]["candidate_actions"][0].__setitem__(
                "rationale", "候选五日均价表现居前，但身份仍待确认。"
            ),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def unsupported_average_price_response(*args, mutation=mutation, index=index):
                    result = (
                        self.response_with_candidate_action(*args)
                        if index == 1
                        else self.valid_response(*args)
                    )
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "涨跌幅.*均价"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=unsupported_average_price_response,
                        settings=self.settings(),
                    )

    def test_report_preserves_market_quote_fallback_wording(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            snapshot_path = root / "2026-07-10_snapshot.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["market"].update(
                {
                    "total_amount_quality": "market_quote_fallback",
                    "total_amount_source": "workbuddy:tongdaxin.tdx_kline:000002.Amount+399106.Amount",
                }
            )
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.market_amount_response,
                settings=self.settings(),
            )
            content = render_detailed_review(review)

            self.assertIn("沪深成交额（通达信行情口径，非交易所官方汇总）", content)
            self.assertIn("来源：workbuddy:tongdaxin.tdx_kline:000002.Amount+399106.Amount", content)

    def test_verified_tongdaxin_close_path_allows_market_path_inference(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            records = []
            for offset, sample_date in enumerate(
                ("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10")
            ):
                records.append(
                    {
                        "sample_date": sample_date,
                        "indices": [
                            {
                                "symbol": "sh000001",
                                "name": "上证指数",
                                "close": 3900 + offset,
                            },
                            {
                                "symbol": "sz399001",
                                "name": "深证成指",
                                "close": 14000 + offset,
                            },
                            {
                                "symbol": "sz399006",
                                "name": "创业板指",
                                "close": 3500 + offset,
                            },
                        ],
                    }
                )
            (root / "2026-07-10_tdx_index_history.json").write_text(
                json.dumps(
                    {
                        "trade_date": "2026-07-10",
                        "sample_date": "2026-07-10",
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

            def path_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][0]["confidence"] = "medium"
                result["step_analyses"][0]["conclusion"] = (
                    "五根完整收盘路径呈现震荡格局，完整箱体边界仍待确认。"
                )
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=path_response,
                settings=self.settings(),
            )

            self.assertIn("完整收盘路径", review.evidence_facts["market.history_range"])
            self.assertIn("呈现震荡格局", review.analyses[0].conclusion)

    def test_report_renders_actual_references_for_composite_evidence(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            self.write_history(root)
            self.write_review_candidate_history(root, "2026-07-10")
            add_pool_item(
                "hot",
                "002829",
                "星网宇达",
                "2026-07-10",
                "人工确认",
                sectors=("军工",),
                database_path=database_path,
            )

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                database_path,
                request_completion=self.valid_response,
                settings=self.settings(),
            )
            content = render_detailed_review(review)

            self.assertEqual(
                review.evidence_references["market.indices"],
                (str(root / "2026-07-10_snapshot.json"),),
            )
            self.assertEqual(len(review.evidence_references["market.history_range"]), 5)
            self.assertEqual(
                review.evidence_references["candidate_history.return_5d_percent"],
                (str(root / "2026-07-10_review_candidate_history.json"),),
            )
            self.assertEqual(
                review.evidence_references["pool.active_real"],
                (f"SQLite:{database_path}#active_real_pool",),
            )
            self.assertIn(
                f"- 证据引用：{root / '2026-07-10_review_candidate_history.json'}",
                content,
            )

    def test_workbuddy_request_exports_current_guarded_prompt(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            self.write_review_candidate_history(root, "2026-07-10")

            request = prepare_detailed_review_request(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
            )
            payload = json.loads(render_detailed_review_request(request))

            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["review_date"], "2026-07-10")
            self.assertEqual(len(payload["request_id"]), 64)
            self.assertIn("只能返回用户要求的 JSON 对象", payload["system_prompt"])
            self.assertEqual(payload["user_prompt"]["review_date"], "2026-07-10")
            self.assertIn("required_output", payload["user_prompt"])
            final_output = payload["user_prompt"]["required_output"]["final_plan_draft"]
            self.assertIn("conditional", final_output["participation_decision"])
            stock_preview_output = payload["user_prompt"]["required_output"]["stock_preview"]
            self.assertIn("candidate_universe", stock_preview_output["expectation"])
            self.assertNotIn("ai_candidate_shortlist", stock_preview_output["expectation"])
            self.assertIn("仓位上限", final_output["position_guidance"])
            self.assertIn("ai_candidate_shortlist", final_output["focus_codes"][0])
            self.assertTrue(final_output["candidate_actions"])
            self.assertIn("ai_candidate_shortlist", payload["user_prompt"]["local_claim_boundaries"]["relative_comparison"])
            self.assertEqual(
                [item["code"] for item in payload["user_prompt"]["ai_candidate_shortlist"]],
                ["002829", "603580"],
            )
            self.assertEqual(payload["user_prompt"]["preview_scopes"]["个股"]["denominator"], 2)
            self.assertTrue(
                all("manual_record" not in step for step in payload["user_prompt"]["steps"])
            )
            self.assertIn("资金仍然活跃", payload["system_prompt"])
            self.assertIn("连续性未验证的本地样本路径", payload["system_prompt"])
            self.assertIn("公开公式待确认", payload["system_prompt"])
            self.assertIn("热门题材榜", payload["system_prompt"])
            self.assertNotIn("LLM_API_KEY", render_detailed_review_request(request))

    def test_workbuddy_response_creates_report_without_external_llm_settings(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            self.write_review_candidate_history(root, "2026-07-10")
            request = prepare_detailed_review_request(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
            )
            response = self.valid_response(None, request.system_prompt, request.user_prompt)
            envelope = self.workbuddy_response_envelope(request, response)

            output_path = create_detailed_review_report(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                output_dir=root / "reports",
                log_path=root / "review.log",
                workbuddy_response_text=json.dumps(envelope, ensure_ascii=False),
            )
            content = output_path.read_text(encoding="utf-8")

            self.assertIn("模型草案来源：WorkBuddy 内置模型（经本地校验）", content)
            self.assertIn("## STEP 10: 最终输出复盘文档", content)
            self.assertIn("model_source=workbuddy_builtin_llm", (root / "review.log").read_text(encoding="utf-8"))

    def test_manual_judgment_is_rendered_only_after_independent_model_inference(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            self.write_history(root)
            add_manual_step_record(
                "2026-07-10",
                3,
                "人工判断为弱反弹",
                "manual:test",
                database_path=database_path,
            )
            request = prepare_detailed_review_request(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                database_path,
            )

            self.assertNotIn("人工判断为弱反弹", request.user_prompt)
            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                database_path,
                request_completion=self.valid_response,
                settings=self.settings(),
            )
            self.assertIn(
                "人工判断对照（不参与模型推断）：人工判断为弱反弹",
                render_detailed_review(review),
            )

    def test_candidate_ranking_merges_unique_same_name_leader_fact_without_confirming_identity(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            self.write_review_candidate_history(root, "2026-07-10")
            snapshot_path = root / "2026-07-10_snapshot.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["stocks"][0]["role_source"] = "涨停池连板股（2板）"
            snapshot["stocks"][0]["turnover_rate"] = 12.5
            snapshot["stocks"][1]["role_source"] = "涨停池连板股（3板）"
            snapshot["stocks"].append(
                {
                    "code": "待确认",
                    "name": "星网宇达",
                    "sector": "创新药",
                    "role": "板块领涨事实候选",
                    "role_source": "concept 板块领涨股",
                }
            )
            snapshot_path.write_text(
                json.dumps(snapshot, ensure_ascii=False),
                encoding="utf-8",
            )

            request = prepare_detailed_review_request(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
            )

            self.assertEqual(request.ranked_candidates[0].code, "002829")
            self.assertIn("创新药", request.ranked_candidates[0].sector)
            self.assertIn("板块领涨事实候选", request.ranked_candidates[0].role)
            self.assertEqual(request.ranked_candidates[0].board_count, 2)
            self.assertEqual(request.ranked_candidates[0].turnover, 0.125)
            self.assertLessEqual(len(request.ranked_candidates), 3)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.valid_response,
                settings=self.settings(),
            )
            self.assertIn("当日换手率：12.5%", render_detailed_review(review))

    def test_candidate_history_turnover_ratio_is_rendered_as_percentage(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            self.write_review_candidate_history(root, "2026-07-10")
            history_path = root / "2026-07-10_review_candidate_history.json"
            history = json.loads(history_path.read_text(encoding="utf-8"))
            history["records"][0]["turnover"] = 0.5153
            history["records"][0]["turnover_unit"] = "ratio"
            history_path.write_text(
                json.dumps(history, ensure_ascii=False),
                encoding="utf-8",
            )

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.valid_response,
                settings=self.settings(),
            )

            self.assertIn("当日换手率：51.53%", render_detailed_review(review))

    def test_legacy_candidate_turnover_uses_exact_source_unit(self):
        cases = (
            (
                {"turnover": 0.5153, "source": "akshare:stock_zh_a_daily"},
                {},
                0.5153,
            ),
            (
                {"turnover": 51.53, "source": "akshare:stock_zh_a_hist"},
                {},
                0.5153,
            ),
            (
                {"turnover": 51.53, "source": "unknown"},
                {},
                None,
            ),
            (
                {"turnover": 51.53, "source": "unknown"},
                {"turnover_rate": 12.5},
                0.125,
            ),
            (
                {"turnover": 51.53, "source": "unknown"},
                {"turnover": 12.5},
                None,
            ),
        )

        for history, stock, expected in cases:
            with self.subTest(history=history, stock=stock):
                self.assertEqual(
                    resolve_candidate_turnover_ratio(history, stock),
                    expected,
                )

    def test_active_real_pool_same_code_uses_turnover_resolver(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            self.write_history(root)
            self.write_review_candidate_history(root, "2026-07-10")
            history_path = root / "2026-07-10_review_candidate_history.json"
            history = json.loads(history_path.read_text(encoding="utf-8"))
            history["records"][0]["turnover"] = 51.53
            history["records"][0]["source"] = "akshare:stock_zh_a_hist"
            history_path.write_text(
                json.dumps(history, ensure_ascii=False),
                encoding="utf-8",
            )
            add_pool_item(
                "hot",
                "002829",
                "星网宇达",
                "2026-07-10",
                "人工确认",
                sectors=("军工",),
                database_path=database_path,
            )

            request = prepare_detailed_review_request(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                database_path,
            )
            candidate = next(
                item for item in request.candidates if item.code == "002829"
            )

            self.assertEqual(candidate.turnover, 0.5153)
            self.assertEqual(candidate.origin, "系统发现候选 + 用户真实池")

    def test_workbuddy_base64_transport_preserves_chinese_response(self):
        response_text = '{"model_response":{"conclusion":"中文复盘"}}'
        encoded = base64.b64encode(response_text.encode("utf-8")).decode("ascii")

        self.assertEqual(decode_workbuddy_response_base64(encoded), response_text)

    def test_cli_accepts_workbuddy_base64_stdin(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            request = prepare_detailed_review_request(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
            )
            response = self.valid_response(None, request.system_prompt, request.user_prompt)
            envelope = self.workbuddy_response_envelope(request, response)
            encoded = base64.b64encode(
                json.dumps(envelope, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            stdout = StringIO()

            with patch("stock_review.cli.sys.stdin", StringIO(encoded)), redirect_stdout(stdout):
                exit_code = main(
                    [
                        "review",
                        "create-detailed",
                        "--date",
                        "2026-07-10",
                        "--framework",
                        "stock-review.md",
                        "--snapshot-dir",
                        str(root),
                        "--database",
                        str(root / "stock_review.sqlite"),
                        "--output-dir",
                        str(root / "reports"),
                        "--log-path",
                        str(root / "review.log"),
                        "--workbuddy-response-base64-stdin",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("已生成详细复盘", stdout.getvalue())
            self.assertTrue((root / "reports" / "2026-07-10_detailed_review.md").exists())

    def test_workbuddy_response_rejects_stale_request_before_overwriting_report(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            request = prepare_detailed_review_request(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
            )
            response = self.valid_response(None, request.system_prompt, request.user_prompt)
            envelope = self.workbuddy_response_envelope(request, response)
            envelope["request_id"] = "0" * 64
            output_dir = root / "reports"
            output_dir.mkdir()
            output_path = output_dir / "2026-07-10_detailed_review.md"
            output_path.write_text("旧报告\n", encoding="utf-8")
            log_path = root / "review.log"
            log_path.write_text("旧日志\n", encoding="utf-8")

            with self.assertRaisesRegex(DetailedReviewError, "请求指纹"):
                create_detailed_review_report(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    output_dir=output_dir,
                    log_path=log_path,
                    workbuddy_response_text=json.dumps(envelope, ensure_ascii=False),
                )

            self.assertEqual(output_path.read_text(encoding="utf-8"), "旧报告\n")
            self.assertEqual(log_path.read_text(encoding="utf-8"), "旧日志\n")

    def test_workbuddy_response_reuses_candidate_scope_validation(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            request = prepare_detailed_review_request(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
            )
            response = self.outside_candidate_response(None, request.system_prompt, request.user_prompt)
            envelope = self.workbuddy_response_envelope(request, response)

            with self.assertRaisesRegex(DetailedReviewError, "有限候选范围之外"):
                create_detailed_review_report(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    output_dir=root / "reports",
                    log_path=root / "review.log",
                    workbuddy_response_text=json.dumps(envelope, ensure_ascii=False),
                )

            self.assertFalse((root / "reports" / "2026-07-10_detailed_review.md").exists())
            self.assertFalse((root / "review.log").exists())

    def test_report_renders_only_saved_user_preview_and_final_response(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            self.write_history(root)
            preview = add_manual_preview(
                "2026-07-10",
                "市场",
                "量能保持",
                "量能增强",
                "量能走弱",
                "证据失效",
                str(root / "2026-07-10_snapshot.json"),
                user_confirmed=True,
                database_path=database_path,
            )
            ManualReviewRepository(database_path).add_final_response(
                ManualFinalResponse(
                    final_id="FINAL-20260710-001",
                    review_date="2026-07-10",
                    response="只观察已确认条件",
                    risk_boundary="证据失效时停止观察",
                    evidence_reference=str(root / "2026-07-10_snapshot.json"),
                    plan_reference=str(root / "plan.md"),
                    plan_item="计划项",
                    pool_codes=("002829",),
                    preview_ids=(preview.preview_id,),
                    observation_ids=("OBS-20260710-001",),
                    created_at=datetime.now().isoformat(timespec="seconds"),
                )
            )

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                database_path,
                request_completion=self.valid_response,
                settings=self.settings(),
            )
            content = render_detailed_review(review)

            self.assertIn("状态：confirmed_by_user", content)
            self.assertIn(f"预演 ID：{preview.preview_id}", content)
            self.assertIn("最终应对：只观察已确认条件", content)
            self.assertNotIn("次日总纲：保持条件观察", content)

    def test_prompt_requires_style_feedback_and_combined_candidate_universe(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            captured_payload = {}

            def capture_response(_settings, _system_prompt, user_prompt):
                captured_payload.update(json.loads(user_prompt))
                return self.valid_response(_settings, _system_prompt, user_prompt)

            build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=capture_response,
                settings=self.settings(),
            )

            steps = {item["step_number"]: item for item in captured_payload["steps"]}
            self.assertIn("sentiment.limit_down_count", steps[2]["local_evidence_fields"])
            self.assertIn("最高连板仅代表复盘日高度事实", steps[2]["analysis_guidance"])
            merged_fields = {
                field_name
                for step in steps.values()
                for field_name in step["local_evidence_fields"]
            }
            self.assertNotIn("market.us_session", merged_fields)
            self.assertNotIn("stocks.longhubang", merged_fields)
            self.assertNotIn("candidate_history.capacity_ranking", merged_fields)
            self.assertIn(
                "evidence_field_keys",
                captured_payload["required_output"]["step_analyses"][0],
            )
            self.assertIn(
                "confidence",
                captured_payload["required_output"]["step_analyses"][0],
            )
            required_output = captured_payload["required_output"]
            self.assertNotIn("preview_drafts", required_output)
            for output_field in ("market_preview", "sector_preview", "stock_preview"):
                self.assertIn(output_field, required_output)
                self.assertNotIn("target", required_output[output_field])
            candidate_codes = {item["code"] for item in captured_payload["candidate_universe"]}
            self.assertEqual(candidate_codes, {"002829", "603580"})

    def test_step8_targets_and_evidence_routing_are_fixed_locally(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            snapshot_path = root / "2026-07-10_snapshot.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["sectors"].insert(
                0,
                {
                    "name": "低排名题材",
                    "rank": 3,
                    "limit_up_count": 2,
                    "source_type": "hhxg_hot_theme",
                },
            )
            snapshot_path.write_text(
                json.dumps(snapshot, ensure_ascii=False),
                encoding="utf-8",
            )

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.aliased_preview_response,
                settings=self.settings(),
            )

            self.assertEqual(
                [preview.target for preview in review.preview_drafts],
                ["大盘", "板块", "个股"],
            )
            self.assertIn("market.indices", review.preview_drafts[0].evidence_field_keys)
            self.assertIn("sectors.rankings", review.preview_drafts[1].evidence_field_keys)
            self.assertIn("stocks.current", review.preview_drafts[2].evidence_field_keys)
            self.assertEqual(review.preview_drafts[1].scope.object_name, "创新药")
            self.assertEqual(review.preview_drafts[1].scope.source_name, "热门题材榜")
            self.assertEqual(review.preview_drafts[2].scope.denominator, 2)
            self.assertEqual(review.preview_drafts[2].scope.source_name, "candidate_universe")
            self.assertIn(
                "固定对象：创新药；固定数据源：热门题材榜",
                review.preview_drafts[1].expectation,
            )
            self.assertIn("固定分母：2 只", review.preview_drafts[2].expectation)
            self.assertIn("不改变本地主分档", review.preview_drafts[2].expectation)
            self.assertEqual(
                set(review.preview_drafts[2].scope.candidate_codes),
                {"002829", "603580"},
            )
            stock_bands = {
                band.scenario: band.condition
                for band in review.preview_drafts[2].scenario_bands
            }
            self.assertIn("30% ≤", stock_bands["under_expectation"])
            self.assertIn("< 50%", stock_bands["under_expectation"])
            self.assertIn("50% ≤", stock_bands["expectation"])

    def test_step8_rejects_cross_ranking_fields_and_single_candidate_override(self):
        mutations = (
            lambda result: result["market_preview"].__setitem__(
                "expectation",
                "深证成指收盘不低于前收且成交额不低于复盘日",
            ),
            lambda result: result["sector_preview"].__setitem__(
                "expectation",
                "创新药强势概念榜净流入为正",
            ),
            lambda result: result["sector_preview"].__setitem__(
                "expectation",
                "通信在热门题材榜排名第 2 至 3 名",
            ),
            lambda result: result["stock_preview"].__setitem__(
                "expectation",
                "AI 候选中任一候选收盘不低于前收",
            ),
            lambda result: result["stock_preview"].__setitem__(
                "expectation",
                "002829 收盘不低于前收且涨幅达到 1%",
            ),
            lambda result: result["stock_preview"].__setitem__(
                "expectation",
                "AI 候选集至少 2 只封板",
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaises(DetailedReviewError):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

    def test_step9_priority_requires_matched_background(self):
        for weak_sentiment in (False, True):
            with self.subTest(weak_sentiment=weak_sentiment), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)
                if weak_sentiment:
                    self.write_snapshot(root, "2026-07-10", 3904, limit_down_count=210)
                review = build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.valid_response,
                    settings=self.settings(),
                )

                priority_numbers, _ = select_priority_pattern_numbers(review)
                checks_by_number = {
                    check.pattern_number: check for check in review.step9_checks
                }
                if weak_sentiment:
                    self.assertTrue(priority_numbers)
                else:
                    self.assertEqual(priority_numbers, ())
                self.assertTrue(
                    all(
                        checks_by_number[number].status
                        in {"background_matched_trigger_pending", "confirmed"}
                        for number in priority_numbers
                    )
                )
                self.assertFalse(
                    any(
                        checks_by_number[number].status
                        == "base_evidence_available_trigger_pending"
                        for number in priority_numbers
                    )
                )
                self.assertLessEqual(len(priority_numbers), 3)

    def test_report_rejects_confirmed_cycle_or_sector_stage_without_required_history(self):
        mutations = (
            (
                "step_three_cycle",
                lambda result: result["step_analyses"][2].__setitem__(
                    "conclusion", "短线情绪已经回暖。"
                ),
            ),
            (
                "step_four_core_sector",
                lambda result: result["step_analyses"][3].__setitem__(
                    "conclusion", "通信已经成为核心板块。"
                ),
            ),
            (
                "step_five_main_rise",
                lambda result: result["step_analyses"][4].__setitem__(
                    "conclusion", "通信已经进入主升阶段。"
                ),
            ),
            (
                "step_eight_cycle",
                lambda result: result["preview_outlook"].__setitem__(
                    "primary_judgment", "短线情绪进入修复阶段。"
                ),
            ),
            (
                "step_ten_summary",
                lambda result: result["final_plan_draft"].__setitem__(
                    "summary", "通信已经成为核心方向。"
                ),
            ),
            (
                "step_ten_condition",
                lambda result: result["final_plan_draft"].__setitem__(
                    "participation_condition",
                    "补齐个股身份、相对强弱和连续比较证据且通信主线排名不低于复盘日后重新评估",
                ),
            ),
        )
        for label, mutation in mutations:
            with self.subTest(label=label), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(
                    DetailedReviewError,
                    "缺少连续情绪历史|缺少板块连续 K 线",
                ):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

    def test_report_accepts_explicit_uncertainty_for_cycle_and_sector_stage(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def bounded_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][2]["confidence"] = "low"
                result["step_analyses"][2]["conclusion"] = (
                    "现有盘后事实低置信度倾向弱反弹或回暖，连续情绪历史仍待确认。"
                )
                result["step_analyses"][3]["conclusion"] = "通信仅为核心板块候选。"
                result["step_analyses"][4]["conclusion"] = "主升阶段尚未确认。"
                result["step_analyses"][2]["next_day_validation"] = "下一日观察情绪能否回暖。"
                result["step_analyses"][3]["next_day_validation"] = "下一日观察通信能否成为主线。"
                result["preview_outlook"]["primary_judgment"] = "情绪是否修复仍待确认。"
                result["final_plan_draft"]["summary"] = "通信是否成为核心方向仍待确认。"
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=bounded_response,
                settings=self.settings(),
            )

            self.assertIn("倾向弱反弹或回暖", review.analyses[2].conclusion)
            self.assertEqual(review.analyses[2].confidence, "low")

    def test_step_confidence_cannot_be_high_while_hard_gaps_remain(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def overconfident_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][2]["confidence"] = "high"
                result["step_analyses"][2]["conclusion"] = (
                    "现有盘后事实倾向弱反弹，连续情绪历史仍待确认。"
                )
                return result

            with self.assertRaisesRegex(DetailedReviewError, "confidence 不得为 high"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=overconfident_response,
                    settings=self.settings(),
                )

    def test_plan_conditions_reject_unquantified_vague_terms_but_accept_thresholds(self):
        mutations = (
            lambda result: result["market_preview"].__setitem__(
                "expectation", "上证维持近期箱体"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "participation_condition", "通信方向明显走强后观察"
            ),
            lambda result: result["final_plan_draft"]["candidate_actions"][0].__setitem__(
                "abandon_condition", "候选跌破关键位置时放弃"
            ),
            lambda result: result["final_plan_draft"]["candidate_actions"][0].__setitem__(
                "trigger", "002829 走强后继续观察"
            ),
            lambda result: result["sector_preview"].__setitem__(
                "over_expectation", "强势概念榜出现更多个股涨停"
            ),
            lambda result: result["stock_preview"].__setitem__(
                "over_expectation", "多只候选封板"
            ),
            lambda result: result["stock_preview"].__setitem__(
                "expectation", "多数候选晋级"
            ),
            lambda result: result["sector_preview"].__setitem__(
                "expectation", "板块个股普遍涨停"
            ),
            lambda result: result["market_preview"].__setitem__(
                "under_expectation", "市场出现大面积跌停"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "participation_condition", "关注候选集体封板后再人工确认"
            ),
            lambda result: result["sector_preview"].__setitem__(
                "over_expectation", "强势概念榜净流入进一步放大"
            ),
            lambda result: result["market_preview"].__setitem__(
                "over_expectation", "上证明确突破前收"
            ),
            lambda result: result["sector_preview"].__setitem__(
                "expectation", "前排个股继续封板且成交额高于 2 万亿元"
            ),
            lambda result: result["stock_preview"].__setitem__(
                "expectation", "候选整体维持红盘且成交额高于 2 万亿元"
            ),
            lambda result: result["sector_preview"].__setitem__(
                "over_expectation", "板块扩散至细分方向且成交额高于 2 万亿元"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "participation_condition", "所属方向延续且候选完成晋级"
            ),
            lambda result: result["market_preview"].__setitem__(
                "under_expectation", "外围市场连续大跌且两市成交额低于 2 万亿元"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "risk_boundary", "盘面急剧变化且跌停家数高于涨停家数"
            ),
            lambda result: result["sector_preview"].__setitem__(
                "over_expectation", "板块前排继续封板且成交额高于 2 万亿元"
            ),
            lambda result: result["stock_preview"].__setitem__(
                "expectation", "候选总体维持红盘且成交额高于 2 万亿元"
            ),
            lambda result: result["sector_preview"].__setitem__(
                "over_expectation", "向细分方向扩散且至少 1 只候选涨停"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "participation_condition", "所在方向延续且候选完成晋级"
            ),
            lambda result: result["market_preview"].__setitem__(
                "under_expectation", "海外市场连续下挫且两市成交额低于 2 万亿元"
            ),
            lambda result: result["market_preview"].__setitem__(
                "expectation", "上证高开高走且成交额不低于 2 万亿元"
            ),
            lambda result: result["market_preview"].__setitem__(
                "expectation", "上证高开高走且涨停率超过 1%"
            ),
            lambda result: result["market_preview"].__setitem__(
                "expectation", "上证高开高走且情绪评分高于 80 点"
            ),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation, index=index):
                    result = (
                        self.response_with_candidate_action(*args)
                        if index in {2, 3}
                        else self.valid_response(*args)
                    )
                    mutation(result)
                    return result

                with self.assertRaisesRegex(
                    DetailedReviewError,
                    "未量化的(?:模糊|市场路径)条件",
                ):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def threshold_response(*args):
                result = self.valid_response(*args)
                result["market_preview"]["expectation"] = "上证维持近期箱体 3900 至 4000 点"
                result["sector_preview"]["over_expectation"] = "至少 3 只候选涨停"
                result["stock_preview"]["over_expectation"] = "3 只候选集体封板"
                result["stock_preview"]["expectation"] = "AI 候选集收盘不低于前收的占比达到 50%"
                result["sector_preview"]["expectation"] = "板块排名延续且不低于复盘日"
                result["sector_preview"]["abandon_condition"] = (
                    "至少 2 个已命名细分方向各至少 1 只涨停"
                )
                result["market_preview"]["under_expectation"] = "上证指数连续 2 日各跌超过 2%"
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=threshold_response,
                settings=self.settings(),
            )
            self.assertIn("3900", review.preview_drafts[0].expectation)

            def bounded_market_path_response(*args):
                result = self.valid_response(*args)
                result["market_preview"]["expectation"] = (
                    "上证高开高走且收盘涨幅超过 1%"
                )
                return result

            bounded_market_path_review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=bounded_market_path_response,
                settings=self.settings(),
            )
            self.assertIn("收盘涨幅超过 1%", bounded_market_path_review.preview_drafts[0].expectation)

    def test_missing_sentiment_history_rejects_uncalibrated_state_claims(self):
        mutations = (
            lambda result: result["step_analyses"][1].__setitem__(
                "interpretation", "炸板率偏高，最高连板维持在中高位。"
            ),
            lambda result: result["step_analyses"][2].__setitem__(
                "conclusion", "当日情绪指标处于中等水平。"
            ),
            lambda result: result["step_analyses"][2].__setitem__(
                "interpretation", "涨停家数与最高连板处于中等水平，但周期无法确认。"
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "缺少连续情绪历史"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def bounded_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][1]["interpretation"] = (
                    "最高连板仅为复盘日事实，连续性待确认。"
                )
                result["step_analyses"][2]["interpretation"] = (
                    "炸板率仅为当日事实，不支持判为偏高。"
                )
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=bounded_response,
                settings=self.settings(),
            )
            self.assertIn("连续性待确认", review.analyses[1].interpretation)
            self.assertIn("不支持判为偏高", review.analyses[2].interpretation)

    def test_group_conditions_require_explicit_scope_or_count(self):
        mutations = (
            lambda result: result["stock_preview"].__setitem__(
                "expectation", "关注候选次日整体维持红盘"
            ),
            lambda result: result["sector_preview"].__setitem__(
                "expectation", "通信方向有个股涨停且强势概念榜净流入为正"
            ),
            lambda result: result["stock_preview"].__setitem__(
                "under_expectation", "个股高开回落"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "risk_boundary", "盘面出现跌停时停止观察"
            ),
            lambda result: result["sector_preview"].__setitem__(
                "expectation", "002829 仍待确认且通信方向有个股涨停"
            ),
            lambda result: result["stock_preview"].__setitem__(
                "expectation", "17 只候选中有个股涨停"
            ),
            lambda result: result["stock_preview"].__setitem__(
                "expectation", "候选涨停家数沿用复盘日口径"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "risk_boundary", "候选中至少 1 只红盘且盘面出现跌停"
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "对象范围|未量化的模糊条件"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.valid_response,
                settings=self.settings(),
            )
            self.assertIn("占比", review.preview_drafts[2].expectation)

            def selector_response(*args):
                result = self.valid_response(*args)
                result["stock_preview"]["expectation"] = (
                    "候选中的任一只收盘不低于前收"
                )
                return result

            with self.assertRaisesRegex(DetailedReviewError, "边缘候选"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=selector_response,
                    settings=self.settings(),
                )

    def test_preview_scenarios_reject_equivalent_or_overlapping_branches(self):
        mutations = (
            lambda result: result["sector_preview"].update(
                {
                    "expectation": "候选中至少一只封板且强势概念榜净流入保持为正",
                    "over_expectation": "候选中至少一只涨停且强势概念榜净流入为正",
                }
            ),
            lambda result: result["market_preview"].update(
                {
                    "expectation": "上证收盘涨跌幅不超过一个百分点且成交额不低于复盘日",
                    "under_expectation": "上证跌破前收或成交额低于复盘日",
                }
            ),
            lambda result: result["market_preview"].update(
                {
                    "under_expectation": "上证跌破前收",
                    "abandon_condition": "上证跌破前收",
                }
            ),
            lambda result: result["stock_preview"].update(
                {
                    "expectation": "候选中至少 3 只涨停",
                    "over_expectation": "候选中至少 1 只涨停",
                }
            ),
            lambda result: result["sector_preview"].update(
                {
                    "expectation": "候选中任一只封板且强势概念榜净流入为正",
                    "over_expectation": "候选中任一只达到涨停且强势概念榜净流入为正",
                }
            ),
            lambda result: result["market_preview"].update(
                {
                    "expectation": "三大指数收涨且成交额高于复盘日",
                    "over_expectation": "上证收盘不低于前收",
                }
            ),
            lambda result: result["market_preview"].update(
                {
                    "expectation": "上证站上 4000 点",
                    "over_expectation": "上证站上 3900 点",
                }
            ),
            lambda result: result["market_preview"].update(
                {
                    "expectation": "上证指数达到 4000 点",
                    "over_expectation": "上证站上 3900 点",
                }
            ),
            lambda result: result["market_preview"].update(
                {
                    "expectation": "上证涨幅不低于 3%",
                    "over_expectation": "上证涨幅不低于 1%",
                }
            ),
            lambda result: result["market_preview"].update(
                {
                    "expectation": "上证涨幅不高于 3%",
                    "over_expectation": "上证涨幅不高于 5%",
                }
            ),
            lambda result: result["market_preview"].update(
                {
                    "expectation": "上证涨跌幅绝对值不超过 1%",
                    "under_expectation": "上证收跌 0.5% 或成交额低于复盘日",
                }
            ),
            lambda result: result["stock_preview"].update(
                {
                    "expectation": "17 只候选中至少 9 只收盘不低于前收",
                    "over_expectation": "17 只候选中至少 8 只收盘不低于前收",
                }
            ),
            lambda result: result["stock_preview"].update(
                {
                    "under_expectation": "候选中的任一只炸板后未回封",
                    "abandon_condition": "任一候选炸板未回封",
                }
            ),
            lambda result: result["stock_preview"].update(
                {
                    "under_expectation": "候选中的任一只炸板未能回封",
                    "abandon_condition": "任一候选炸板未回封",
                }
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(
                    DetailedReviewError,
                    "四情景|情景|固定对象|边缘候选|候选集分母",
                ):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def separated_range_response(*args):
                result = self.valid_response(*args)
                result["market_preview"]["expectation"] = (
                    "上证涨跌幅绝对值不超过 1% 且成交额不低于复盘日"
                )
                result["market_preview"]["under_expectation"] = (
                    "上证收跌超过 2% 或成交额低于复盘日"
                )
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=separated_range_response,
                settings=self.settings(),
            )
            self.assertIn("超过 2%", review.preview_drafts[0].under_expectation)

            def chinese_quantity_response(*args):
                result = self.valid_response(*args)
                result["stock_preview"]["expectation"] = "候选中至少两只涨停"
                result["stock_preview"]["over_expectation"] = "候选中至少三只涨停"
                return result

            chinese_quantity_review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=chinese_quantity_response,
                settings=self.settings(),
            )
            self.assertIn("至少三只", chinese_quantity_review.preview_drafts[2].over_expectation)

    def test_next_day_validation_rejects_unbounded_terms(self):
        mutations = (
            lambda result: result["step_analyses"][0].__setitem__(
                "next_day_validation", "观察成交额是否出现急剧变化。"
            ),
            lambda result: result["step_analyses"][3].__setitem__(
                "next_day_validation", "观察通信方向能否延续强势并出现扩散。"
            ),
            lambda result: result["step_analyses"][5].__setitem__(
                "next_day_validation", "观察候选是否盘中抗跌或带动板块。"
            ),
            lambda result: result["step_analyses"][6].__setitem__(
                "next_day_validation", "观察关注对象能否满足各自触发条件。"
            ),
            lambda result: result["step_analyses"][3].__setitem__(
                "next_day_validation", "候选中至少一只涨停；观察通信方向是否扩散。"
            ),
            lambda result: result["step_analyses"][3].__setitem__(
                "next_day_validation",
                "候选中任一只收盘不低于前收或通信方向扩散。",
            ),
            lambda result: result["step_analyses"][3].__setitem__(
                "next_day_validation",
                "候选中任一只收盘不低于前收或通信方向走强。",
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "次日验证点"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def bounded_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][3]["next_day_validation"] = (
                    "核对通信方向涨停家数是否高于复盘日，以此确认扩散。"
                )
                result["step_analyses"][5]["next_day_validation"] = (
                    "核对候选中任一只是否收盘不低于前收，以此定义盘中抗跌。"
                )
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=bounded_response,
                settings=self.settings(),
            )
            self.assertIn("涨停家数", review.analyses[3].next_day_validation)
            self.assertIn("定义盘中抗跌", review.analyses[5].next_day_validation)

    def test_missing_relative_comparison_supports_no_action_and_bounded_candidate_watch(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.valid_response,
                settings=self.settings(),
            )

            self.assertEqual(review.final_plan_draft.focus_codes, ())
            self.assertEqual(review.final_plan_draft.candidate_actions, ())
            self.assertEqual(review.final_plan_draft.participation_decision, "no")
            content = render_detailed_review(review)
            self.assertIn("无可复核的逐候选行动", content)
            self.assertIn("重新评估条件", content)
            self.assertIn("不代表恢复参与", content)
            self.assertIn("判定顺序：放弃条件", content)
            self.assertIn("AI 证据重评边界", content)
            self.assertNotIn("以下给出 AI 条件行动方案", content)

            def missing_reassessment_response(*args):
                result = self.valid_response(*args)
                result["final_plan_draft"]["participation_condition"] = (
                    "不设置参与条件，仅保持观察"
                )
                return result

            with self.assertRaisesRegex(DetailedReviewError, "重新评估条件"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=missing_reassessment_response,
                    settings=self.settings(),
                )

            invalid_reassessment_conditions = (
                "个股身份、相对强弱和连续比较证据仍不可确认时重新评估",
                "仍未补齐个股身份、相对强弱和连续比较证据时重新评估",
                "没有获得个股身份、相对强弱和连续比较证据时重新评估",
                "无法获得个股身份、相对强弱和连续比较证据时重新评估",
                "不能取得个股身份、相对强弱和连续比较证据时重新评估",
                "补齐个股身份、相对强弱和连续比较证据后重新评估；候选中任一只涨停则参与",
                "个股身份、相对强弱和连续比较仍缺失；补齐市场证据后重新评估",
                "个股身份、相对强弱和连续比较仍缺失但市场证据齐备后重新评估",
                "补齐个股身份、相对强弱和连续比较证据后重新评估并开仓",
                "补齐个股身份、相对强弱和连续比较证据后重新评估并下单",
                "补齐个股身份、相对强弱和连续比较证据后重新评估并加仓",
                "补齐个股身份、相对强弱和连续比较证据后重新评估并进场",
                "补齐个股身份、相对强弱和连续比较证据后重新评估并介入",
                "并非已经获得个股身份、相对强弱和连续比较证据后重新评估",
                "获得市场数据但个股身份、相对强弱和连续比较仍缺失后重新评估",
            )
            for condition in invalid_reassessment_conditions:
                with self.subTest(condition=condition):

                    def invalid_reassessment_response(*args, condition=condition):
                        result = self.valid_response(*args)
                        result["final_plan_draft"]["participation_condition"] = condition
                        return result

                    with self.assertRaisesRegex(
                        DetailedReviewError,
                        "重新评估条件|条件观察文本",
                    ):
                        build_detailed_review(
                            "2026-07-10",
                            Path("stock-review.md"),
                            root,
                            root / "stock_review.sqlite",
                            request_completion=invalid_reassessment_response,
                            settings=self.settings(),
                        )

            def postfix_reassessment_response(*args):
                result = self.valid_response(*args)
                result["final_plan_draft"]["participation_condition"] = (
                    "不设置可执行仓位；个股身份、相对强弱和连续比较相关证据均补齐后重新评估"
                )
                return result

            postfix_review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=postfix_reassessment_response,
                settings=self.settings(),
            )
            self.assertIn(
                "证据均补齐后重新评估",
                postfix_review.final_plan_draft.participation_condition,
            )

            watch_review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.response_with_candidate_action,
                settings=self.settings(),
            )
            self.assertEqual(watch_review.final_plan_draft.participation_decision, "no")
            self.assertEqual(watch_review.final_plan_draft.candidate_actions[0].action, "watch")

    def test_missing_relative_comparison_rejects_strength_selection(self):
        mutations = (
            lambda result: result["step_analyses"][5].__setitem__(
                "conclusion", "候选相对抗跌待核查。"
            ),
            lambda result: result["step_analyses"][6].__setitem__(
                "conclusion", "当前无法筛出重点对象，但仍列出强势候选。"
            ),
            lambda result: result["preview_outlook"].__setitem__(
                "primary_judgment", "次日优先观察强区间对象。"
            ),
            lambda result: result["stock_preview"].__setitem__(
                "expectation", "AI 候选集近期走弱对象占比达到 50% 且收盘不低于前收"
            ),
            lambda result: result["stock_preview"].__setitem__(
                "over_expectation", "AI 候选集收盘不低于前收的占比达到 80%，且走势独立"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "summary", "相对偏弱对象列为回避。"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "summary", "优先观察涨幅突出对象，回避涨幅有限对象。"
            ),
            lambda result: result["preview_outlook"].__setitem__(
                "primary_judgment", "优先寻找具有趋势延续价值的候选。"
            ),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation, index=index):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "缺少相对强弱或连续比较证据"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

    def test_missing_relative_comparison_allows_evidence_ranked_focus_but_rejects_strength_claims(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def missing_boundary_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][6]["conclusion"] = "当前证据只能保留候选。"
                return result

            candidate_review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=missing_boundary_response,
                settings=self.settings(),
            )
            self.assertEqual(
                candidate_review.analyses[6].conclusion,
                "当前证据只能保留候选。",
            )

            def validation_question_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][6]["next_day_validation"] = "下一日观察候选是否逆势抗跌。"
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=validation_question_response,
                settings=self.settings(),
            )
            self.assertIn("是否逆势抗跌", review.analyses[6].next_day_validation)

            def market_validation_question_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][0]["next_day_validation"] = "下一日观察能否维持成交额。"
                return result

            market_review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=market_validation_question_response,
                settings=self.settings(),
            )
            self.assertIn("能否维持成交额", market_review.analyses[0].next_day_validation)

    def test_report_rejects_unrendered_candidate_volume_and_turnover_claims(self):
        mutations = (
            lambda result: result["step_analyses"][5].__setitem__(
                "next_day_validation", "观察候选成交能否持续放大。"
            ),
            lambda result: result["step_analyses"][6].__setitem__(
                "next_day_validation", "观察候选成交能否持续。"
            ),
            lambda result: result["final_plan_draft"]["candidate_actions"][0].__setitem__(
                "rationale", "候选成交与换手活跃，身份仍待确认"
            ),
            lambda result: result["final_plan_draft"]["candidate_actions"][0].__setitem__(
                "trigger", "候选封板且成交放大后继续观察"
            ),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation, index=index):
                    result = (
                        self.response_with_candidate_action(*args)
                        if index in {2, 3}
                        else self.valid_response(*args)
                    )
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "当前证据未提供的候选日 K"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

    def test_sector_net_inflow_requires_ranking_scope_in_preview_and_final_plan(self):
        mutations = (
            lambda result: result["sector_preview"].__setitem__(
                "expectation", "板块排行保持首位且净流入为正"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "risk_boundary", "板块净流入转负时停止观察"
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "必须明确热门题材榜"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

    def test_missing_sector_kline_rejects_path_claims_but_allows_ranked_validation(self):
        mutations = (
            lambda result: result["step_analyses"][3].__setitem__(
                "interpretation", "通信方向创出新高，板块趋势已经确认。"
            ),
            lambda result: result["step_analyses"][4].__setitem__(
                "conclusion", "板块首次回调后重新走强。"
            ),
            lambda result: result["preview_outlook"].__setitem__(
                "primary_judgment", "通信方向连续两日强势。"
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "缺少板块连续 K 线"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def ranked_validation_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][3]["next_day_validation"] = (
                    "缺少板块 K 线，无法确认能否创出新高。"
                )
                result["preview_outlook"]["primary_judgment"] = (
                    "上证指数创出新高，板块方向仍待确认。"
                )
                result["sector_preview"]["expectation"] = (
                    "创新药在热门题材榜排名第 2 至 3 名"
                )
                result["market_preview"]["over_expectation"] = "上证指数收盘创出新高"
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=ranked_validation_response,
                settings=self.settings(),
            )
            self.assertIn("第 2 至 3 名", review.preview_drafts[1].expectation)

    def test_probability_claims_require_calibration_in_primary_and_summary(self):
        mutations = (
            lambda result: result["preview_outlook"].__setitem__(
                "primary_judgment", "次日上涨的可能性较高。"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "summary", "通信方向大概率延续。"
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "缺少校准依据"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def bounded_response(*args):
                result = self.valid_response(*args)
                result["preview_outlook"]["primary_judgment"] = (
                    "次日主情景暂按震荡预演，不代表概率判断。"
                )
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=bounded_response,
                settings=self.settings(),
            )
            self.assertIn("不代表概率判断", review.preview_outlook.primary_judgment)

    def test_market_amount_history_claims_require_history_but_numeric_future_condition_is_allowed(self):
        mutations = (
            lambda result: result["step_analyses"][0].__setitem__(
                "conclusion", "市场成交额维持在较高水平。"
            ),
            lambda result: result["preview_outlook"].__setitem__(
                "primary_judgment", "市场量能持续处于高位。"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "summary", "成交额维持高位，继续观察。"
            ),
            lambda result: result["step_analyses"][0].__setitem__(
                "interpretation", "复盘日成交额达到本地证据值并维持高位。"
            ),
            lambda result: result["step_analyses"][0].__setitem__(
                "interpretation", "成交额维持高位，但连续性待确认。"
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "缺少连续成交额历史"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def numeric_condition_response(*args):
                result = self.valid_response(*args)
                result["market_preview"]["expectation"] = "上证指数对应两市成交额维持在 2 万亿元以上"
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=numeric_condition_response,
                settings=self.settings(),
            )
            self.assertIn("2 万亿元以上", review.preview_drafts[0].expectation)

    def test_report_rejects_single_day_liquidity_and_unavailable_index_path_claims(self):
        mutations = (
            lambda result: result["step_analyses"][0].__setitem__(
                "interpretation", "复盘日成交额较大，说明资金仍然活跃。"
            ),
            lambda result: result["step_analyses"][0].__setitem__(
                "interpretation", "复盘日成交额较大，资金保持活跃，但连续性待确认。"
            ),
            lambda result: result["step_analyses"][0].__setitem__(
                "interpretation", "五根日 K 首尾变化有限，区间内未出现单边趋势。"
            ),
            lambda result: result["step_analyses"][0].__setitem__(
                "interpretation", "五根日 K 首尾变化有限，未出现单边趋势，但路径待确认。"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "summary", "指数五根日 K 未出现单边趋势，保持观察。"
            ),
            lambda result: result["step_analyses"][0].__setitem__(
                "conclusion", "指数整体仍处于震荡格局。"
            ),
            lambda result: result["preview_outlook"].__setitem__(
                "primary_judgment", "次日预计延续震荡。"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "summary", "市场预计延续震荡。"
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(
                    DetailedReviewError,
                    "缺少连续成交额历史|连续性未验证的本地样本路径",
                ):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def bounded_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][0]["interpretation"] = (
                    "复盘日成交额达到本地证据值，仅能作为当日事实；"
                    "仅凭首尾变化不能确认是否连续上涨或处于震荡格局。"
                )
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=bounded_response,
                settings=self.settings(),
            )
            self.assertIn("仅能作为当日事实", review.analyses[0].interpretation)

    def test_unverified_profit_effect_rejects_directional_interpretation(self):
        mutations = (
            lambda result: result["step_analyses"][1].__setitem__(
                "interpretation", "赚钱效应为正，可作为情绪偏暖依据。"
            ),
            lambda result: result["step_analyses"][2].__setitem__(
                "conclusion", "赚钱效应偏强，表明短线反馈改善。"
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation):
                    result = self.valid_response(*args)
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "赚钱效应公开公式待确认"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            snapshot_path = root / "2026-07-10_snapshot.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["sentiment"]["profit_effect_calculation"] = "limit_up_ratio_proxy"
            snapshot_path.write_text(
                json.dumps(snapshot, ensure_ascii=False),
                encoding="utf-8",
            )

            def proxy_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][2]["conclusion"] = (
                    "赚钱效应偏强，表明短线反馈改善。"
                )
                return result

            with self.assertRaisesRegex(DetailedReviewError, "赚钱效应公开公式待确认"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=proxy_response,
                    settings=self.settings(),
                )

        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            def bounded_response(*args):
                result = self.valid_response(*args)
                result["step_analyses"][1]["interpretation"] = (
                    "赚钱效应公式未公开且无法据此确认偏强。"
                )
                result["step_analyses"][2]["conclusion"] = (
                    "赚钱效应公开公式待确认，不据此判断情绪方向。"
                )
                return result

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=bounded_response,
                settings=self.settings(),
            )
            self.assertIn("不据此判断", review.analyses[2].conclusion)

    def test_plan_conditions_reject_unverified_stock_identity_terms(self):
        mutations = (
            lambda result: result["stock_preview"].__setitem__(
                "expectation", "龙头完成晋级"
            ),
            lambda result: result["final_plan_draft"].__setitem__(
                "participation_condition", "核心票完成晋级后继续观察"
            ),
            lambda result: result["final_plan_draft"]["candidate_actions"][0].__setitem__(
                "trigger", "中军收盘不低于前收"
            ),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation), TemporaryDirectory() as temp_path:
                root = Path(temp_path)
                self.write_history(root)

                def response(*args, mutation=mutation, index=index):
                    result = (
                        self.response_with_candidate_action(*args)
                        if index == 2
                        else self.valid_response(*args)
                    )
                    mutation(result)
                    return result

                with self.assertRaisesRegex(DetailedReviewError, "个股身份缺失"):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

    def test_report_rejects_model_cross_step_evidence_field(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "本 STEP 白名单之外"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.cross_step_response,
                    settings=self.settings(),
                )

    def test_report_rejects_missing_or_empty_evidence_field_keys(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            for response in (
                self.response_without_evidence_field_keys,
                self.response_with_empty_evidence_field_keys,
            ):
                with self.subTest(response=response.__name__), self.assertRaisesRegex(
                    DetailedReviewError,
                    "证据字段|规定字段",
                ):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

    def test_report_keeps_only_model_declared_step_evidence_fields(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            self.write_review_candidate_history(root, "2026-07-10")

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.omitted_required_fields_response,
                settings=self.settings(),
            )

            analyses = {analysis.step_number: analysis for analysis in review.analyses}
            self.assertNotIn("candidate_history.trend_capacity", analyses[2].evidence_field_keys)
            self.assertNotIn("`candidate_history.trend_capacity`", render_detailed_review(review))
            for number in (6, 7):
                self.assertNotIn("stocks.current", analyses[number].evidence_field_keys)
                self.assertEqual(
                    analyses[number].evidence_field_keys,
                    ("candidate_history.latest",),
                )

    def test_report_rejects_unavailable_candidate_history_field(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "本 STEP 白名单之外"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.omitted_required_fields_response,
                    settings=self.settings(),
                )

    def test_report_rejects_analysis_removed_entirely_by_numeric_sanitization(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "清理后没有可用定性内容"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.created_number_response,
                    settings=self.settings(),
                )

    def test_report_removes_model_number_next_to_chinese_text(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "清理后没有可用定性内容"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.chinese_adjacent_number_response,
                    settings=self.settings(),
                )

    def test_report_removes_whole_sentence_containing_numeric_fact(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "清理后没有可用定性内容"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.decline_magnitude_response,
                    settings=self.settings(),
                )

    def test_report_accepts_broken_board_rate_rendered_as_percentage(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.broken_board_percentage_response,
                settings=self.settings(),
            )

            self.assertEqual(review.evidence_facts["sentiment.broken_board_rate"], "炸板率：31%")
            self.assertEqual(review.analyses[1].interpretation, "仍需结合其它负反馈核查。")
            self.assertIn("`sentiment.broken_board_rate`：炸板率：31%", render_detailed_review(review))

    def test_report_removes_approximate_numbers_without_comparing_each_expression(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            self.write_snapshot(root, "2026-07-10", 3904, limit_down_count=210)

            for response in (
                self.approximate_local_number_response,
                self.unrelated_approximate_number_response,
            ):
                with self.subTest(response=response.__name__), self.assertRaisesRegex(
                    DetailedReviewError,
                    "清理后没有可用定性内容",
                ):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

    def test_report_removes_numeric_clause_even_when_direction_is_wrong(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "清理后没有可用定性内容"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.increase_from_decline_response,
                    settings=self.settings(),
                )

    def test_report_allows_trade_and_position_language_in_analysis(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.trade_instruction_response,
                settings=self.settings(),
            )

            self.assertIn("可以直接买入", review.analyses[0].conclusion)
            self.assertIn("低吸", review.analyses[0].next_day_validation)

    def test_report_accepts_explicit_non_trading_boundary_statement(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.non_trading_boundary_response,
                settings=self.settings(),
            )

            self.assertIn("不构成买卖建议或仓位指令", review.analyses[0].interpretation)

    def test_report_allows_position_language_in_analysis(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.position_description_response,
                settings=self.settings(),
            )

            self.assertIn("仓位仍需用户自行确认", review.analyses[0].interpretation)

    def test_report_removes_step_numbers_from_model_prose_but_still_requires_all_steps(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            for response in (self.current_step_label_response, self.cross_step_label_response):
                with self.subTest(response=response.__name__), self.assertRaisesRegex(
                    DetailedReviewError,
                    "清理后没有可用定性内容",
                ):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )
            with self.assertRaisesRegex(DetailedReviewError, "完整覆盖"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.missing_step_response,
                    settings=self.settings(),
                )

    def test_report_rejects_focus_code_outside_limited_candidates(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "有限候选范围之外"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.outside_candidate_response,
                    settings=self.settings(),
                )

    def test_partial_core_evidence_rejects_aggressive_final_plan(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            for response in (
                self.participation_yes_response,
                self.buy_action_response,
                self.executable_position_response,
                self.textual_buy_response,
                self.mixed_position_response,
            ):
                with self.subTest(response=response.__name__), self.assertRaisesRegex(
                    DetailedReviewError,
                    "核心票身份尚未验证",
                ):
                    build_detailed_review(
                        "2026-07-10",
                        Path("stock-review.md"),
                        root,
                        root / "stock_review.sqlite",
                        request_completion=response,
                        settings=self.settings(),
                    )

    def test_unverified_candidates_allow_capped_conditional_watch_plan(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.capped_conditional_response,
                settings=self.settings(),
            )

            self.assertEqual(review.final_plan_draft.participation_decision, "conditional")
            self.assertEqual(len(review.final_plan_draft.focus_codes), 1)
            self.assertEqual(review.final_plan_draft.candidate_actions[0].action, "watch")
            self.assertIn("上限", review.final_plan_draft.position_guidance)
            content = render_detailed_review(review)
            self.assertIn("AI 条件行动方案", content)
            self.assertIn("未确认 AI 候选", content)

    def test_candidate_action_rejects_sector_not_bound_to_local_candidate(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "板块归属不属于该候选"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.mismatched_candidate_sector_response,
                    settings=self.settings(),
                )

    def test_candidate_action_rejects_unowned_leader_role(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "板块领涨角色"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.mismatched_candidate_role_response,
                    settings=self.settings(),
                )

    def test_plan_rejects_daily_kline_trigger_without_daily_kline_evidence(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "当前证据未提供的候选日 K"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.unsupported_daily_kline_response,
                    settings=self.settings(),
                )

    def test_report_downgrades_unverified_candidate_identity_claims(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.unverified_identity_response,
                settings=self.settings(),
            )

            self.assertIn("具备连板候选特征", review.analyses[5].conclusion)
            self.assertIn("相对表现仍待确认", review.analyses[5].conclusion)
            self.assertNotIn("具备连板龙头特征", review.analyses[5].conclusion)

    def test_report_rejects_candidate_action_outside_focus_codes(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "有限关注范围之外"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.outside_focus_action_response,
                    settings=self.settings(),
                )

    def test_report_rejects_plan_prose_code_outside_limited_candidates(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(DetailedReviewError, "有限候选范围之外"):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.outside_plan_prose_code_response,
                    settings=self.settings(),
                )

    def test_step_two_requires_every_available_key_style_evidence(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            with self.assertRaisesRegex(
                DetailedReviewError,
                "必须纳入本地已可用的关键证据字段.*sectors.rankings",
            ):
                build_detailed_review(
                    "2026-07-10",
                    Path("stock-review.md"),
                    root,
                    root / "stock_review.sqlite",
                    request_completion=self.height_only_style_response,
                    settings=self.settings(),
                )

    @staticmethod
    def settings() -> OpenAICompatibleSettings:
        return OpenAICompatibleSettings("https://example.invalid/v1", "test-key", "test-model")

    @staticmethod
    def workbuddy_response_envelope(request, response):
        return {
            "schema_version": 1,
            "review_date": request.review_date,
            "request_id": request.request_id,
            "model_response": response,
        }

    @staticmethod
    def valid_response(_settings, _system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        market_scope = payload["preview_scopes"]["大盘"]
        sector_scope = payload["preview_scopes"]["板块"]
        preferred_fields = {
            1: ["market.history_range"],
            2: ["sentiment.history_style", "sentiment.history_cycle", "candidate_history.trend_capacity"],
            3: ["sentiment.history_cycle"],
            4: ["sectors.history_repeated_rankings"],
            5: ["sectors.rankings"],
            6: ["stocks.current", "candidate_history.return_5d_percent", "pool_history.return_5d_percent"],
            7: ["stocks.current", "candidate_history.return_5d_percent", "pool.active_real"],
        }

        def selected_fields(step):
            available_fields = step["local_evidence_fields"]
            selected = [
                field_name
                for field_name in preferred_fields[step["step_number"]]
                if field_name in available_fields
            ]
            if step["step_number"] == 2:
                selected.extend(
                    field_name
                    for field_name in (
                        "sentiment.board_ladder",
                        "candidate_history.capacity_ranking",
                        "sectors.rankings",
                        "stocks.longhubang",
                    )
                    if field_name in available_fields and field_name not in selected
                )
            return selected or [next(iter(available_fields))]

        return {
            "step_analyses": [
                {
                    "step_number": step["step_number"],
                    "confidence": "low" if step["hard_missing_fields"] else "medium",
                    "conclusion": (
                        "当前只将本地证据排序对象作为次日重点核验候选，不确认核心身份。"
                        if step["step_number"] == 7
                        else "当前证据只支持形成待核查推断。"
                    ),
                    "interpretation": "已有事实提供了比较基础，但硬缺口仍限制确定性判断。",
                    "evidence_field_keys": selected_fields(step),
                    "next_day_validation": "继续观察现有事实是否延续，并在缺口补齐后重新核查。",
                }
                for step in payload["steps"]
            ],
            "preview_outlook": {
                "primary_judgment": "次日主情景仍需连续情绪、板块与候选反馈共同确认。",
                "odds_direction": "rising",
                "evidence_field_keys": ["sentiment.history_cycle"],
            },
            "market_preview": {
                "expectation": f"{market_scope['object_name']}收盘不低于前收且成交额不低于复盘日",
                "over_expectation": f"{market_scope['object_name']}收盘涨幅不低于 1% 且成交额高于复盘日",
                "under_expectation": f"{market_scope['object_name']}收盘低于前收且成交额低于复盘日",
                "abandon_condition": f"{market_scope['object_name']}收盘跌幅超过 2%",
            },
            "sector_preview": {
                "expectation": f"{sector_scope['object_name']}在{sector_scope['source_name']}排名第 2 至 3 名",
                "over_expectation": f"{sector_scope['object_name']}在{sector_scope['source_name']}排名第 1 名且涨停家数高于复盘日",
                "under_expectation": f"{sector_scope['object_name']}在{sector_scope['source_name']}排名第 4 至 5 名",
                "abandon_condition": f"{sector_scope['object_name']}退出{sector_scope['source_name']}前 5 名",
            },
            "stock_preview": {
                "expectation": "AI 候选集收盘不低于前收的占比达到 50%",
                "over_expectation": "AI 候选集收盘不低于前收的占比达到 80%",
                "under_expectation": "AI 候选集收盘不低于前收的占比低于 50%",
                "abandon_condition": "AI 候选集收盘不低于前收的占比低于 30%",
            },
            "final_plan_draft": {
                "participation_decision": "no",
                "position_guidance": "不设置可执行仓位",
                "summary": "保持条件观察",
                "participation_condition": "补齐个股身份、相对强弱和连续比较证据后重新评估",
                "risk_boundary": "跌停家数高于涨停家数时停止当前观察逻辑",
                "focus_codes": [],
                "candidate_actions": [],
                "evidence_field_keys": ["market.history_range", "stocks.current"],
            },
        }

    @staticmethod
    def response_with_candidate_action(*args):
        result = DetailedReviewTest.valid_response(*args)
        payload = json.loads(args[-1])
        code = payload["ai_candidate_shortlist"][0]["code"]
        result["final_plan_draft"]["focus_codes"] = [code]
        result["final_plan_draft"]["candidate_actions"] = [
            {
                "code": code,
                "action": "watch",
                "rationale": "当前仅有候选事实，身份仍待确认",
                "trigger": "候选完成晋级后继续观察",
                "position_guidance": "不设置可执行仓位",
                "abandon_condition": "候选炸板后未回封时放弃",
            }
        ]
        return result

    @staticmethod
    def capped_conditional_response(*args):
        result = DetailedReviewTest.response_with_candidate_action(*args)
        code = result["final_plan_draft"]["focus_codes"][0]
        result["final_plan_draft"].update(
            {
                "participation_decision": "conditional",
                "position_guidance": "仅报告内条件方案，总仓位上限 20%",
                "summary": "未确认 AI 候选仅作条件观察",
                "participation_condition": (
                    f"{code} 收盘不低于前收时，由用户人工评估是否参与"
                ),
            }
        )
        result["final_plan_draft"]["candidate_actions"][0][
            "position_guidance"
        ] = "单只仓位上限 10%"
        return result

    @staticmethod
    def cross_step_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][0]["evidence_field_keys"] = ["stocks.current"]
        return result

    @staticmethod
    def market_amount_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][0]["evidence_field_keys"].append("market.total_amount")
        return result

    @staticmethod
    def response_without_evidence_field_keys(*args):
        result = DetailedReviewTest.valid_response(*args)
        for analysis in result["step_analyses"]:
            analysis.pop("evidence_field_keys", None)
        result["preview_outlook"].pop("evidence_field_keys", None)
        result["final_plan_draft"].pop("evidence_field_keys", None)
        return result

    @staticmethod
    def response_with_empty_evidence_field_keys(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][0]["evidence_field_keys"] = []
        return result

    @staticmethod
    def aliased_preview_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["market_preview"]["target"] = "市场"
        result["sector_preview"]["target"] = "核心板块"
        result["stock_preview"]["target"] = "核心票"
        return result

    @staticmethod
    def omitted_required_fields_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        payload = json.loads(args[-1])
        step_two_fields = set(payload["steps"][1]["local_evidence_fields"])
        result["step_analyses"][1]["evidence_field_keys"] = [
            field_name
            for field_name in (
                "sentiment.history_cycle",
                "sentiment.board_ladder",
                "candidate_history.capacity_ranking",
                "sectors.rankings",
                "stocks.longhubang",
            )
            if field_name in step_two_fields
        ]
        for index in (5, 6):
            result["step_analyses"][index]["evidence_field_keys"] = ["candidate_history.latest"]
        return result

    @staticmethod
    def created_number_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][0]["conclusion"] = "当前证据支持连续 99 日判断。"
        return result

    @staticmethod
    def chinese_adjacent_number_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["preview_outlook"]["primary_judgment"] = "候选价格75元时修复价值上升。"
        return result

    @staticmethod
    def decline_magnitude_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][0]["evidence_field_keys"] = ["market.indices"]
        result["step_analyses"][0]["interpretation"] = "上证指数下跌 3.05%，市场表现转弱。"
        return result

    @staticmethod
    def broken_board_percentage_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][1]["evidence_field_keys"].append(
            "sentiment.broken_board_rate"
        )
        result["step_analyses"][1]["interpretation"] = "炸板率为 31%。仍需结合其它负反馈核查。"
        return result

    @staticmethod
    def approximate_local_number_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][1]["evidence_field_keys"].extend(
            ("sentiment.limit_down_count", "sentiment.highest_board")
        )
        result["step_analyses"][1]["interpretation"] = (
            "情绪处于退潮末期或冰点，连续两日跌停近200家，最高连板降至3板，赚钱效应极低。"
        )
        return result

    @staticmethod
    def unrelated_approximate_number_response(*args):
        result = DetailedReviewTest.approximate_local_number_response(*args)
        result["step_analyses"][1]["interpretation"] = "当前跌停近900家，仍需继续核查。"
        return result

    @staticmethod
    def increase_from_decline_response(*args):
        result = DetailedReviewTest.decline_magnitude_response(*args)
        result["step_analyses"][0]["interpretation"] = "上证指数上涨 3.05%，市场表现增强。"
        return result

    @staticmethod
    def trade_instruction_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][0]["conclusion"] = "当前可以直接买入。"
        result["step_analyses"][0]["next_day_validation"] = (
            "若回调承接稳定，可以低吸并控制仓位。"
        )
        return result

    @staticmethod
    def non_trading_boundary_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][0]["interpretation"] = (
            "当前证据仅用于盘后复盘，不构成买卖建议或仓位指令。"
        )
        return result

    @staticmethod
    def position_description_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][0]["interpretation"] = (
            "现有证据只支持盘后核查，仓位仍需用户自行确认。"
        )
        return result

    @staticmethod
    def current_step_label_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][6]["interpretation"] = "STEP 7 当前仍只支持条件观察。"
        return result

    @staticmethod
    def cross_step_label_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][0]["interpretation"] = "STEP 7 当前仍只支持条件观察。"
        return result

    @staticmethod
    def missing_step_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"].pop()
        return result

    @staticmethod
    def outside_candidate_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["final_plan_draft"]["focus_codes"] = ["999999"]
        return result

    @staticmethod
    def participation_yes_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["final_plan_draft"]["participation_decision"] = "yes"
        return result

    @staticmethod
    def participation_conditional_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["final_plan_draft"]["participation_decision"] = "conditional"
        return result

    @staticmethod
    def buy_action_response(*args):
        result = DetailedReviewTest.response_with_candidate_action(*args)
        result["final_plan_draft"]["candidate_actions"][0]["action"] = "buy"
        return result

    @staticmethod
    def executable_position_response(*args):
        result = DetailedReviewTest.response_with_candidate_action(*args)
        result["final_plan_draft"]["position_guidance"] = "总仓位 30%"
        result["final_plan_draft"]["candidate_actions"][0]["position_guidance"] = "单只 10%"
        return result

    @staticmethod
    def textual_buy_response(*args):
        result = DetailedReviewTest.response_with_candidate_action(*args)
        result["final_plan_draft"]["candidate_actions"][0]["trigger"] = (
            "候选完成晋级后买入"
        )
        return result

    @staticmethod
    def mixed_position_response(*args):
        result = DetailedReviewTest.response_with_candidate_action(*args)
        result["final_plan_draft"]["candidate_actions"][0]["position_guidance"] = (
            "仅观察，触发后仓位 50%"
        )
        return result

    @staticmethod
    def mismatched_candidate_sector_response(*args):
        result = DetailedReviewTest.response_with_candidate_action(*args)
        result["final_plan_draft"]["candidate_actions"][0]["rationale"] = (
            "创新药方向保持活跃，但身份仍待确认"
        )
        return result

    @staticmethod
    def mismatched_candidate_role_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        payload = json.loads(args[-1])
        candidate = next(
            item
            for item in payload["ai_candidate_shortlist"]
            if "板块领涨" not in item["role"]
        )
        result["final_plan_draft"]["focus_codes"] = [candidate["code"]]
        result["final_plan_draft"]["candidate_actions"] = [
            {
                "code": candidate["code"],
                "action": "watch",
                "rationale": f"{candidate['name']}是板块领涨事实候选，身份仍待确认",
                "trigger": "候选完成晋级后继续观察",
                "position_guidance": "不设置可执行仓位",
                "abandon_condition": "候选炸板后未回封时放弃",
            }
        ]
        return result

    @staticmethod
    def unsupported_daily_kline_response(*args):
        result = DetailedReviewTest.response_with_candidate_action(*args)
        result["final_plan_draft"]["candidate_actions"][0]["trigger"] = (
            "缩量回踩 5 日线企稳后再观察"
        )
        return result

    @staticmethod
    def unverified_identity_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][5]["conclusion"] = (
            "艾艾精工具备连板龙头特征，星网宇达走势独立。"
        )
        return result

    @staticmethod
    def outside_focus_action_response(*args):
        result = DetailedReviewTest.response_with_candidate_action(*args)
        result["final_plan_draft"]["candidate_actions"][0]["code"] = "999999"
        return result

    @staticmethod
    def outside_plan_prose_code_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["stock_preview"]["expectation"] = "999999 出现强反馈"
        return result

    @staticmethod
    def height_only_style_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][1]["evidence_field_keys"] = ["sentiment.highest_board"]
        return result

    @staticmethod
    def pool_only_candidate_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][5]["evidence_field_keys"] = ["pool_history.return_5d_percent"]
        return result

    @classmethod
    def write_history(cls, root: Path) -> None:
        for offset, trade_date in enumerate(("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10")):
            cls.write_snapshot(root, trade_date, 3900 + offset)

    @staticmethod
    def write_snapshot(
        root: Path,
        trade_date: str,
        index_close: float,
        limit_down_count: int = 17,
    ) -> None:
        payload = {
            "trade_date": trade_date,
            "sample_date": trade_date,
            "source": "manual",
            "market": {
                "indices": [{"name": "上证指数", "close": index_close, "change_percent": -3.05}],
                "total_amount": 1450000000000,
            },
            "sentiment": {
                "limit_up_count": 29,
                "limit_down_count": limit_down_count,
                "broken_board_rate": 0.31,
                "highest_board": 3,
                "profit_effect_percent": 55,
                "profit_effect_source": "hhxg:market.sentiment_index",
                "profit_effect_calculation": "source_provided_formula_unverified",
            },
            "sectors": [
                {"name": "创新药", "rank": 1, "limit_up_count": 7, "source_type": "hhxg_hot_theme"}
            ],
            "stocks": [
                {"code": "002829", "name": "星网宇达", "sector": "军工", "role": "板块领涨事实"},
                {"code": "603580", "name": "艾艾精工", "sector": "轻工制造", "role": "连板事实候选"},
            ],
            "field_sources": {
                "market": "akshare",
                "sentiment": "hhxg",
                "sectors": "hhxg",
                "stocks": "akshare",
            },
            "events": [{"source": "hhxg", "title": "历史快照"}],
        }
        (root / f"{trade_date}_snapshot.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def write_pool_history(root: Path, trade_date: str) -> None:
        payload = {
            "trade_date": trade_date,
            "sample_date": trade_date,
            "records": [
                {
                    "code": "002829",
                    "name": "星网宇达",
                    "close": 19.8,
                    "return_5d_percent": 4.2,
                    "return_20d_percent": 12.5,
                    "missing_fields": [],
                }
            ],
        }
        (root / f"{trade_date}_real_pool_stock_history.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def write_review_candidate_history(root: Path, trade_date: str) -> None:
        payload = {
            "trade_date": trade_date,
            "sample_date": trade_date,
            "records": [
                {
                    "code": "002829",
                    "name": "星网宇达",
                    "close": 19.8,
                    "return_5d_percent": 4.2,
                    "return_20d_percent": 12.5,
                    "average_5d_amount": 800000000,
                    "circulating_market_value": 4800000000,
                    "missing_fields": [],
                },
                {
                    "code": "603580",
                    "name": "艾艾精工",
                    "close": 31.2,
                    "return_5d_percent": -6.5,
                    "return_20d_percent": -18.0,
                    "average_5d_amount": 420000000,
                    "circulating_market_value": 4100000000,
                    "missing_fields": [],
                },
            ],
        }
        (root / f"{trade_date}_review_candidate_history.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
