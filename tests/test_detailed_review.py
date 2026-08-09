from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_review.llm.openai_compatible_client import OpenAICompatibleSettings
from stock_review.pools.manage_pool_item import add_pool_item
from stock_review.review_documents.create_detailed_review import (
    DetailedReviewError,
    build_detailed_review,
    create_detailed_review_report,
    render_detailed_review,
)
from stock_review.review_documents.manage_manual_review import (
    ManualFinalResponse,
    add_manual_preview,
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
            self.assertIn("连续市场事实：上证指数 5 日区间", content)
            self.assertIn("多日板块重复排行事实：创新药", content)
            self.assertIn("002829 星网宇达 5 日涨跌幅 4.2", content)
            self.assertIn("值博率方向：rising", content)
            self.assertIn("background_matched_trigger_pending", content)
            self.assertIn("603580 艾艾精工｜板块：轻工制造｜角色事实：连板事实候选", content)
            self.assertIn("603580 艾艾精工｜来源：系统发现候选｜计划：观察", content)
            self.assertIn("### 模式 1：题材上升初期和发酵阶段的弹性标", content)
            self.assertIn("### 模式 12：情绪指数双低迷尾盘炸板", content)
            self.assertIn("### 大盘", content)
            self.assertIn("符合预期：现有市场事实延续", content)
            self.assertIn("次日能否参与：条件满足后参与", content)
            self.assertIn("建议总仓位：总仓位不超过 20%", content)
            self.assertIn("次日总纲：保持条件观察", content)
            self.assertNotIn("## 指数全貌", content)
            self.assertTrue((root / "review.log").exists())

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
            self.assertIn("最高连板只代表高度仍在", steps[2]["analysis_guidance"])
            self.assertNotIn(
                "evidence_field_keys",
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

    def test_report_ignores_model_cross_step_evidence_field(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.cross_step_response,
                settings=self.settings(),
            )

            self.assertIn("market.indices", review.analyses[0].evidence_field_keys)
            self.assertNotIn("stocks.current", review.analyses[0].evidence_field_keys)

    def test_report_builds_when_model_returns_no_evidence_field_keys(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.response_without_evidence_field_keys,
                settings=self.settings(),
            )

            self.assertIn("market.indices", review.analyses[0].evidence_field_keys)
            self.assertIn("stocks.current", review.analyses[5].evidence_field_keys)

    def test_report_normalizes_omitted_required_available_evidence_fields(self):
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
            self.assertIn("candidate_history.trend_capacity", analyses[2].evidence_field_keys)
            self.assertIn("`candidate_history.trend_capacity`", render_detailed_review(review))
            for number in (6, 7):
                self.assertIn("stocks.current", analyses[number].evidence_field_keys)
                self.assertIn("candidate_history.return_5d_percent", analyses[number].evidence_field_keys)

    def test_report_does_not_normalize_unavailable_candidate_history_fields(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

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
            for number in (6, 7):
                self.assertNotIn("candidate_history.return_5d_percent", analyses[number].evidence_field_keys)

    def test_report_removes_model_created_numeric_clause(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.created_number_response,
                settings=self.settings(),
            )

            self.assertEqual(review.analyses[0].conclusion, "具体数值与对象代码以本步证据链为准。")

    def test_report_removes_model_number_next_to_chinese_text(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.chinese_adjacent_number_response,
                settings=self.settings(),
            )

            self.assertEqual(review.preview_outlook.primary_judgment, "具体数值与对象代码以本步证据链为准。")

    def test_report_removes_whole_sentence_containing_numeric_fact(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.decline_magnitude_response,
                settings=self.settings(),
            )

            self.assertEqual(review.analyses[0].interpretation, "具体数值与对象代码以本步证据链为准。")

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
            self.assertEqual(review.analyses[1].interpretation, "具体数值与对象代码以本步证据链为准。")
            self.assertIn("`sentiment.broken_board_rate`：炸板率：31%", render_detailed_review(review))

    def test_report_removes_approximate_numbers_without_comparing_each_expression(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)
            self.write_snapshot(root, "2026-07-10", 3904, limit_down_count=210)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.approximate_local_number_response,
                settings=self.settings(),
            )

            unrelated_review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.unrelated_approximate_number_response,
                settings=self.settings(),
            )
            self.assertEqual(review.analyses[1].interpretation, "具体数值与对象代码以本步证据链为准。")
            self.assertEqual(unrelated_review.analyses[1].interpretation, "具体数值与对象代码以本步证据链为准。")

    def test_report_removes_numeric_clause_even_when_direction_is_wrong(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.increase_from_decline_response,
                settings=self.settings(),
            )

            self.assertEqual(review.analyses[0].interpretation, "具体数值与对象代码以本步证据链为准。")

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

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.current_step_label_response,
                settings=self.settings(),
            )
            self.assertEqual(review.analyses[6].interpretation, "具体数值与对象代码以本步证据链为准。")

            cross_step_review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.cross_step_label_response,
                settings=self.settings(),
            )
            self.assertEqual(cross_step_review.analyses[0].interpretation, "具体数值与对象代码以本步证据链为准。")
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

    def test_report_preserves_numeric_plan_thresholds_and_position_actions(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.numeric_action_plan_response,
                settings=self.settings(),
            )
            content = render_detailed_review(review)

            self.assertIn("符合预期：上证指数站上 3900 点且成交额改善", content)
            self.assertIn("次日能否参与：可以参与", content)
            self.assertIn("建议总仓位：总仓位 30%", content)
            self.assertIn("条件买入｜仓位：单只 10%", content)

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

    def test_report_uses_local_style_and_candidate_evidence_fields(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_history(root)

            style_review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.height_only_style_response,
                settings=self.settings(),
            )
            self.assertIn("sentiment.limit_down_count", style_review.analyses[1].evidence_field_keys)
            self.assertIn("sentiment.broken_board_rate", style_review.analyses[1].evidence_field_keys)
            review = build_detailed_review(
                "2026-07-10",
                Path("stock-review.md"),
                root,
                root / "stock_review.sqlite",
                request_completion=self.pool_only_candidate_response,
                settings=self.settings(),
            )
            self.assertIn("stocks.current", review.analyses[5].evidence_field_keys)

    @staticmethod
    def settings() -> OpenAICompatibleSettings:
        return OpenAICompatibleSettings("https://example.invalid/v1", "test-key", "test-model")

    @staticmethod
    def valid_response(_settings, _system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        preferred_fields = {
            1: ["market.history_range"],
            2: ["sentiment.history_style", "sentiment.history_cycle", "candidate_history.trend_capacity"],
            3: ["sentiment.history_cycle"],
            4: ["sectors.history_repeated_rankings"],
            5: ["sectors.rankings"],
            6: ["stocks.current", "candidate_history.return_5d_percent", "pool_history.return_5d_percent"],
            7: ["stocks.current", "candidate_history.return_5d_percent", "pool.active_real"],
        }
        candidate_codes = [item["code"] for item in payload["candidate_universe"]]
        return {
            "step_analyses": [
                {
                    "step_number": step["step_number"],
                    "conclusion": "当前证据只支持形成待核查推断。",
                    "interpretation": "已有事实提供了比较基础，但硬缺口仍限制确定性判断。",
                    "evidence_field_keys": preferred_fields[step["step_number"]],
                    "next_day_validation": "继续观察现有事实是否延续，并在缺口补齐后重新核查。",
                }
                for step in payload["steps"]
            ],
            "preview_outlook": {
                "primary_judgment": "连续弱势背景下，修复博弈价值相对上升，但修复仍待次日事实确认。",
                "odds_direction": "rising",
                "evidence_field_keys": ["sentiment.history_cycle"],
            },
            "market_preview": {
                "expectation": "现有市场事实延续",
                "over_expectation": "市场事实明显改善",
                "under_expectation": "市场事实继续转弱",
                "abandon_condition": "市场证据不再支持当前推断",
            },
            "sector_preview": {
                "expectation": "现有板块排行延续",
                "over_expectation": "板块内部表现进一步增强",
                "under_expectation": "板块排行和内部表现转弱",
                "abandon_condition": "板块事实不再支持当前推断",
            },
            "stock_preview": {
                "expectation": "候选对象保持相对强度",
                "over_expectation": "候选对象带动相关方向增强",
                "under_expectation": "候选对象出现明显弱反馈",
                "abandon_condition": "候选身份或相对强度证据失效",
            },
            "final_plan_draft": {
                "participation_decision": "conditional",
                "position_guidance": "总仓位不超过 20%",
                "summary": "保持条件观察",
                "participation_condition": "市场、板块和候选对象形成一致反馈后再人工确认",
                "risk_boundary": "任一关键证据失效时停止当前观察逻辑",
                "focus_codes": candidate_codes[:2],
                "candidate_actions": [
                    {
                        "code": code,
                        "action": "watch",
                        "rationale": "当前仅有候选事实，身份仍待确认",
                        "trigger": "符合预期后再观察",
                        "position_guidance": "单只不超过 10%",
                        "abandon_condition": "候选事实失效时放弃",
                    }
                    for code in candidate_codes[:2]
                ],
                "evidence_field_keys": ["market.history_range", "stocks.current"],
            },
        }

    @staticmethod
    def cross_step_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][0]["evidence_field_keys"] = ["stocks.current"]
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
    def aliased_preview_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["market_preview"]["target"] = "市场"
        result["sector_preview"]["target"] = "核心板块"
        result["stock_preview"]["target"] = "核心票"
        return result

    @staticmethod
    def omitted_required_fields_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][1]["evidence_field_keys"] = ["sentiment.history_cycle"]
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
        result["step_analyses"][1]["evidence_field_keys"] = [
            "sentiment.broken_board_rate",
            "sentiment.history_cycle",
        ]
        result["step_analyses"][1]["interpretation"] = "炸板率为 31%，仍需结合其它负反馈核查。"
        return result

    @staticmethod
    def approximate_local_number_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["step_analyses"][1]["evidence_field_keys"] = [
            "sentiment.limit_down_count",
            "sentiment.highest_board",
        ]
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
    def numeric_action_plan_response(*args):
        result = DetailedReviewTest.valid_response(*args)
        result["market_preview"]["expectation"] = "上证指数站上 3900 点且成交额改善"
        result["final_plan_draft"]["participation_decision"] = "yes"
        result["final_plan_draft"]["position_guidance"] = "总仓位 30%"
        result["final_plan_draft"]["candidate_actions"][0]["action"] = "buy"
        result["final_plan_draft"]["candidate_actions"][0]["position_guidance"] = "单只 10%"
        result["final_plan_draft"]["candidate_actions"][0]["trigger"] = "放量突破 20 日线后执行"
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
        result = DetailedReviewTest.valid_response(*args)
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
