from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.llm.openai_compatible_client import OpenAICompatibleSettings, read_local_env_file
from stock_review.pools.manage_pool_item import add_pool_item
from stock_review.review_documents.create_final_draft import (
    FinalDraftError,
    create_final_draft,
    render_final_draft,
    render_profit_effect,
    render_sectors,
    render_stock_relative_comparisons,
    render_stocks,
)


class FinalDraftTest(unittest.TestCase):
    def test_sector_facts_keep_complete_hhxg_rank_groups_and_net_inflow(self):
        sectors = [
            {
                "name": "液冷服务器" if index == 1 else f"题材{index}",
                "source_type": "hhxg_hot_theme",
                "rank": index,
                "net_inflow_yi": 6.07 if index == 1 else index,
            }
            for index in range(1, 7)
        ]
        sectors.extend(
            [
                {"name": "通信设备", "source_type": "hhxg_strong_sector", "rank": 1, "net_inflow_yi": 117},
                {"name": "通信服务", "source_type": "hhxg_strong_sector", "rank": 2, "net_inflow_yi": 21},
                {"name": "液冷服务器", "source_type": "hhxg_strong_sector", "rank": 1, "net_inflow_yi": 133},
                {"name": "共封装光学", "source_type": "hhxg_strong_sector", "rank": 2, "net_inflow_yi": 132},
            ]
        )

        rendered = render_sectors(sectors)

        self.assertIn(
            "热门题材、强势行业、强势概念为独立分组口径；净流入仅在所属分组内解释，不跨组比较或合并",
            rendered,
        )
        self.assertIn("热门题材：液冷服务器（排行 1，净流入 6.07 亿）", rendered)
        self.assertIn("题材6（排行 6，净流入 6 亿）", rendered)
        self.assertIn("强势行业：通信设备", rendered)
        self.assertIn("强势概念：液冷服务器（排行 1，净流入 133 亿）", rendered)

    def test_sector_facts_keep_akshare_change_rank_alongside_hhxg_groups(self):
        rendered = render_sectors(
            [
                {
                    "name": "通信设备",
                    "source_type": "industry",
                    "ranking_group": "akshare_industry_change_rank",
                    "rank": 1,
                    "change_percent": 4.2,
                    "up_count": 80,
                    "down_count": 5,
                    "leading_stock": "共进股份",
                },
                {
                    "name": "CPO",
                    "source_type": "hhxg_hot_theme",
                    "ranking_group": "hhxg_hot_theme",
                    "rank": 1,
                    "limit_up_count": 10,
                },
            ]
        )

        self.assertIn("AKShare 行业涨幅榜：通信设备（排行 1，涨跌幅 4.2%", rendered)
        self.assertIn("上涨/下跌 80/5 家", rendered)
        self.assertIn("领涨 共进股份", rendered)
        self.assertIn("热门题材：CPO", rendered)

    def test_stock_facts_keep_name_only_leader_for_deterministic_identity_join(self):
        rendered = render_stocks(
            [
                {
                    "code": "待确认",
                    "name": "共进股份",
                    "sector": "F5G",
                    "role": "板块领涨事实候选",
                    "role_source": "concept 板块领涨股",
                }
            ]
        )

        self.assertIn("代码待确认 共进股份", rendered)
        self.assertIn("板块 F5G", rendered)

    def test_stock_facts_keep_all_candidates_and_role_sources(self):
        stocks = [
            {
                "code": f"00000{index}",
                "name": f"候选{index}",
                "role": "个股事实候选",
                "role_source": "板块领涨股",
            }
            for index in range(1, 9)
        ]
        stocks.append(
            {
                "code": "300862",
                "name": "蓝盾光电",
                "role": "连板事实候选",
                "role_source": "涨停池连板股（5板）",
            }
        )

        rendered = render_stocks(stocks)

        self.assertIn("000008 候选8", rendered)
        self.assertIn(
            "300862 蓝盾光电（板块 待确认，角色 连板事实候选，角色来源 涨停池连板股（5板））",
            rendered,
        )

    def test_relative_comparison_facts_render_values_and_coverage(self):
        rendered = render_stock_relative_comparisons(
            [
                {
                    "code": "002829",
                    "name": "星网宇达",
                    "relative_strength": {
                        "completeness": "complete",
                        "benchmark": "000001",
                        "window_start": "2026-08-11",
                        "window_end": "2026-08-17",
                        "stock_return_percent": 12.5,
                        "benchmark_return_percent": 1.2,
                        "excess_return_percent": 11.3,
                        "direction": "相对偏强",
                    },
                    "continuous_comparison": {
                        "completeness": "complete",
                        "consecutive_up_days": 4,
                        "recent_up_ratio": 0.8,
                        "direction": "上行延续",
                    },
                },
                {
                    "code": "603580",
                    "name": "艾艾精工",
                    "relative_strength": {
                        "completeness": "incomplete",
                        "reason": "候选历史缺该个股",
                    },
                    "continuous_comparison": {
                        "completeness": "incomplete",
                        "reason": "候选历史缺该个股",
                    },
                },
            ]
        )

        self.assertIn("覆盖 1/2", rendered)
        self.assertIn("个股 12.5%", rendered)
        self.assertIn("基准 1.2%", rendered)
        self.assertIn("超额 11.3 个百分点", rendered)
        self.assertIn("末端连涨 4 日", rendered)
        self.assertIn("近 5 个交易对上涨比例 80%", rendered)

    def test_profit_effect_distinguishes_source_value_from_ratio_proxy(self):
        self.assertIn(
            "来源公式未公开",
            render_profit_effect(
                {
                    "profit_effect_percent": 81.5,
                    "profit_effect_source": "hhxg",
                    "profit_effect_calculation": "source_provided_formula_unverified",
                }
            ),
        )
        proxy = render_profit_effect(
            {
                "profit_effect_percent": 99.1,
                "profit_effect_source": "本地计算",
                "profit_effect_calculation": "limit_up_ratio_proxy",
            }
        )
        self.assertIn("涨跌停家数比例代理", proxy)
        self.assertIn("不等同完整赚钱效应", proxy)
        self.assertNotIn("来源公式未公开", proxy)

    def test_draft_renders_local_facts_and_all_real_pool_items(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            self.write_snapshot(root, "2026-07-14")
            self.write_pool_history(root, "2026-07-14")
            add_pool_item("hot", "002829", "星网宇达", "2026-07-14", "人工确认", database_path=database_path)

            draft = create_final_draft("2026-07-14", root, database_path, self.valid_response, self.settings())
            content = render_final_draft(draft)

            self.assertIn("上证指数 收盘 3967.13", content)
            self.assertIn("002829 星网宇达", content)
            self.assertIn("002829 星网宇达 5 日涨跌幅 4.2", content)
            self.assertIn("是否重点关注待用户确认", content)
            self.assertNotIn("市场偏暖", content)
            for field_name in (
                "market.advance_decline",
                "market.us_session",
                "sentiment.board_ladder",
                "stocks.longhubang",
                "candidate_history.capacity_ranking",
                "sectors.daily_kline",
            ):
                self.assertIn(field_name, draft.evidence_facts)

    def test_draft_rejects_unknown_evidence_field(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-14")

            with self.assertRaisesRegex(FinalDraftError, "本地允许的证据字段"):
                create_final_draft("2026-07-14", root, root / "stock_review.sqlite", self.unknown_field_response, self.settings())

    def test_prompt_filters_uncollected_fields_and_binds_each_field_reference(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-14")

            def captured_response(settings, system_prompt, user_prompt):
                payload = json.loads(user_prompt)
                allowed_fields = {
                    field_name
                    for step in payload["step_evidence"]
                    for field_name in step["allowed_evidence_field_keys"]
                }
                self.assertNotIn("market.us_session", allowed_fields)
                self.assertNotIn("stocks.longhubang", allowed_fields)
                self.assertNotIn("sectors.daily_kline", allowed_fields)
                self.assertEqual(
                    payload["evidence_references_by_field"]["market.indices"],
                    [str(root / "2026-07-14_snapshot.json")],
                )
                return self.valid_response(settings, system_prompt, user_prompt)

            create_final_draft(
                "2026-07-14",
                root,
                root / "stock_review.sqlite",
                captured_response,
                self.settings(),
            )

    def test_prompt_binds_relative_comparison_values_to_steps_six_and_seven(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(
                root,
                "2026-07-14",
                stocks=[
                    {
                        "code": "002829",
                        "name": "星网宇达",
                        "identity_verified": True,
                        "relative_strength": {
                            "completeness": "complete",
                            "window_start": "2026-07-08",
                            "window_end": "2026-07-14",
                            "stock_return_percent": 12.5,
                            "benchmark_return_percent": 1.2,
                            "excess_return_percent": 11.3,
                            "direction": "相对偏强",
                        },
                        "continuous_comparison": {
                            "completeness": "complete",
                            "consecutive_up_days": 4,
                            "recent_up_ratio": 0.8,
                            "direction": "上行延续",
                        },
                    }
                ],
            )

            def captured_response(settings, system_prompt, user_prompt):
                payload = json.loads(user_prompt)
                step_fields = {
                    item["step_number"]: item["allowed_evidence_field_keys"]
                    for item in payload["step_evidence"]
                }
                fact = payload["allowed_evidence_fields"][
                    "stocks.relative_comparison"
                ]
                self.assertIn("stocks.relative_comparison", step_fields[6])
                self.assertIn("stocks.relative_comparison", step_fields[7])
                self.assertIn("超额 11.3 个百分点", fact)
                self.assertIn("末端连涨 4 日", fact)
                self.assertEqual(
                    payload["evidence_references_by_field"][
                        "stocks.relative_comparison"
                    ],
                    [str(root / "2026-07-14_snapshot.json")],
                )
                return self.valid_response(settings, system_prompt, user_prompt)

            create_final_draft(
                "2026-07-14",
                root,
                root / "stock_review.sqlite",
                captured_response,
                self.settings(),
            )

    def test_draft_rejects_evidence_field_mapped_to_another_step(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-14")

            with self.assertRaisesRegex(FinalDraftError, "STEP 1 LLM 草案字段"):
                create_final_draft("2026-07-14", root, root / "stock_review.sqlite", self.cross_step_field_response, self.settings())

    def test_draft_rejects_reference_from_another_evidence_field(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-14")
            self.write_pool_history(root, "2026-07-14")

            with self.assertRaisesRegex(FinalDraftError, "实际来源一致"):
                create_final_draft(
                    "2026-07-14",
                    root,
                    root / "stock_review.sqlite",
                    self.mismatched_reference_response,
                    self.settings(),
                )

    def test_draft_rejects_missing_real_pool_association(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            self.write_snapshot(root, "2026-07-14")
            add_pool_item("hot", "002829", "星网宇达", "2026-07-14", "人工确认", database_path=database_path)

            with self.assertRaisesRegex(FinalDraftError, "全部有效真实池代码"):
                create_final_draft("2026-07-14", root, database_path, self.empty_pool_response, self.settings())

    def test_draft_allows_local_history_fields_only_in_step_one_to_step_four(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            for offset, trade_date in enumerate(("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10")):
                self.write_snapshot(root, trade_date, event_source="hhxg", index_close=3900 + offset)

            draft = create_final_draft("2026-07-10", root, root / "stock_review.sqlite", self.history_field_response, self.settings())
            content = render_final_draft(draft)

            self.assertIn("最近 5 个本地有效快照样本", content)
            self.assertIn("连续性未验证", content)
            self.assertNotIn("连续市场事实", content)
            self.assertIn("多日涨停与连板高度事实：2026-07-06", content)
            self.assertIn("多日短线情绪事实：2026-07-06", content)
            self.assertIn("多日板块重复排行事实：创新药", content)

    def test_local_env_file_reads_only_llm_settings(self):
        with TemporaryDirectory() as temp_path:
            env_path = Path(temp_path) / ".env"
            env_path.write_text("LLM_BASE_URL=https://example.invalid/v1\nLLM_API_KEY=test-key\nOTHER=value\n", encoding="utf-8")
            self.assertEqual(read_local_env_file(env_path), {"LLM_BASE_URL": "https://example.invalid/v1", "LLM_API_KEY": "test-key"})

    @staticmethod
    def settings() -> OpenAICompatibleSettings:
        return OpenAICompatibleSettings("https://example.invalid/v1", "test-key", "test-model")

    @staticmethod
    def valid_response(_settings, _system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        evidence_references = payload["evidence_references_by_field"]
        step_fields = {
            item["step_number"]: item["allowed_evidence_field_keys"]
            for item in payload["step_evidence"]
        }
        pool_codes = [item["code"] for item in payload["active_real_pool_items"]]
        selected_fields = {
            number: (
                "pool_history.return_5d_percent"
                if number in {6, 7}
                and "pool_history.return_5d_percent" in step_fields[number]
                else step_fields[number][0] if step_fields[number] else None
            )
            for number in range(1, 8)
        }
        return {
            "step_drafts": [
                {
                    "step_number": number,
                    "evidence_field_keys": (
                        [selected_fields[number]] if selected_fields[number] else []
                    ),
                    "evidence_references": (
                        evidence_references[selected_fields[number]]
                        if selected_fields[number]
                        else []
                    ),
                }
                for number in range(1, 8)
            ],
            "focus_candidates": [
                {
                    "code": code,
                    "evidence_field_keys": ["pool.active_real"],
                    "evidence_references": evidence_references["pool.active_real"],
                }
                for code in pool_codes
            ],
            "final_response": {"status": "pending_confirmation", "related_pool_codes": pool_codes, "related_preview_ids": []},
        }

    @staticmethod
    def unknown_field_response(*args):
        result = FinalDraftTest.valid_response(*args)
        result["step_drafts"][0]["evidence_field_keys"] = ["market.unavailable"]
        return result

    @staticmethod
    def cross_step_field_response(*args):
        result = FinalDraftTest.valid_response(*args)
        result["step_drafts"][0]["evidence_field_keys"] = ["stocks.current"]
        return result

    @staticmethod
    def mismatched_reference_response(*args):
        result = FinalDraftTest.valid_response(*args)
        result["step_drafts"][0]["evidence_references"] = result["step_drafts"][5][
            "evidence_references"
        ]
        return result

    @staticmethod
    def empty_pool_response(*args):
        result = FinalDraftTest.valid_response(*args)
        result["focus_candidates"] = []
        return result

    @staticmethod
    def history_field_response(*args):
        result = FinalDraftTest.valid_response(*args)
        payload = json.loads(args[-1])
        evidence_references = payload["evidence_references_by_field"]
        history_fields = [
            "market.history_range",
            "sentiment.history_style",
            "sentiment.history_cycle",
            "sectors.history_repeated_rankings",
        ]
        for index, field_name in enumerate(history_fields):
            result["step_drafts"][index]["evidence_field_keys"] = [field_name]
            result["step_drafts"][index]["evidence_references"] = evidence_references[
                field_name
            ]
        return result

    @staticmethod
    def write_snapshot(
        root: Path,
        trade_date: str,
        event_source: str | None = None,
        index_close: float = 3967.13,
        stocks: list[dict] | None = None,
    ) -> None:
        payload = {
            "trade_date": trade_date, "sample_date": trade_date, "source": "manual",
            "market": {"indices": [{"name": "上证指数", "close": index_close, "change_percent": 1.36}], "total_amount": 1450000000000},
            "sentiment": {"limit_up_count": 29, "limit_down_count": 172, "broken_board_rate": 0.4082, "highest_board": 3},
            "sectors": [{"name": "创新药", "rank": 1, "limit_up_count": 7, "source_type": "hhxg_hot_theme"}],
            "stocks": stocks if stocks is not None else [],
            "field_sources": {"market": "akshare", "sentiment": "hhxg", "sectors": "hhxg"},
            "events": [{"source": event_source, "title": "历史快照"}] if event_source else [],
        }
        (root / f"{trade_date}_snapshot.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

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
