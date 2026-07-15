from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.llm.openai_compatible_client import OpenAICompatibleSettings, read_local_env_file
from stock_review.pools.manage_pool_item import add_pool_item
from stock_review.review_documents.create_final_draft import FinalDraftError, create_final_draft, render_final_draft


class FinalDraftTest(unittest.TestCase):
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

    def test_draft_rejects_unknown_evidence_field(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-14")

            with self.assertRaisesRegex(FinalDraftError, "本地允许的证据字段"):
                create_final_draft("2026-07-14", root, root / "stock_review.sqlite", self.unknown_field_response, self.settings())

    def test_draft_rejects_evidence_field_mapped_to_another_step(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root, "2026-07-14")

            with self.assertRaisesRegex(FinalDraftError, "STEP 1 LLM 草案字段"):
                create_final_draft("2026-07-14", root, root / "stock_review.sqlite", self.cross_step_field_response, self.settings())

    def test_draft_rejects_missing_real_pool_association(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            self.write_snapshot(root, "2026-07-14")
            add_pool_item("hot", "002829", "星网宇达", "2026-07-14", "人工确认", database_path=database_path)

            with self.assertRaisesRegex(FinalDraftError, "全部有效真实池代码"):
                create_final_draft("2026-07-14", root, database_path, self.empty_pool_response, self.settings())

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
        evidence_reference = payload["allowed_evidence_references"][0]
        step_fields = {
            item["step_number"]: item["allowed_evidence_field_keys"]
            for item in payload["step_evidence"]
        }
        pool_codes = [item["code"] for item in payload["active_real_pool_items"]]
        selected_fields = {
            number: "pool_history.return_5d_percent" if number in {6, 7} else step_fields[number][0]
            for number in range(1, 8)
        }
        return {
            "step_drafts": [
                {"step_number": number, "evidence_field_keys": [selected_fields[number]], "evidence_references": [evidence_reference]}
                for number in range(1, 8)
            ],
            "focus_candidates": [{"code": code, "evidence_field_keys": ["pool.active_real"], "evidence_references": [evidence_reference]} for code in pool_codes],
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
    def empty_pool_response(*args):
        result = FinalDraftTest.valid_response(*args)
        result["focus_candidates"] = []
        return result

    @staticmethod
    def write_snapshot(root: Path, trade_date: str) -> None:
        payload = {
            "trade_date": trade_date, "sample_date": trade_date, "source": "manual",
            "market": {"indices": [{"name": "上证指数", "close": 3967.13, "change_percent": 1.36}], "total_amount": 1450000000000},
            "sentiment": {"limit_up_count": 29, "limit_down_count": 172, "broken_board_rate": 40.82, "highest_board": 3},
            "sectors": [{"name": "创新药", "rank": 1, "limit_up_count": 7}], "stocks": [], "events": [],
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
