from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from stock_review.observations.manage_observation import (
    ObservationError,
    add_observation,
    list_observations,
    review_observation,
)


class ObservationTest(unittest.TestCase):
    def test_observation_can_be_added_and_listed_as_pending(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"

            observation = self.add_sample_observation(database_path)
            observations = list_observations(
                "2026-07-06",
                "pending",
                database_path=database_path,
            )

            self.assertEqual(observation.observation_id, "OBS-20260706-001")
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].related_target, "机器人板块")
            self.assertFalse(observations[0].eligible_for_learning)

    def test_observation_requires_conditions_and_evidence_source(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"
            required_cases = (
                ("confirmation_condition", "成立条件不能为空"),
                ("invalidation_condition", "失效条件不能为空"),
                ("evidence_source", "证据来源不能为空"),
            )

            for field_name, expected_message in required_cases:
                values = {
                    "confirmation_condition": "板块次日继续放量",
                    "invalidation_condition": "板块跌幅超过 2%",
                    "evidence_source": "2026-07-06 Evidence Snapshot",
                }
                values[field_name] = ""
                with self.subTest(field_name=field_name):
                    with self.assertRaisesRegex(ObservationError, expected_message):
                        add_observation(
                            review_date="2026-07-06",
                            topic="机器人板块延续性",
                            hypothesis="机器人板块次日保持强势",
                            related_target="机器人板块",
                            database_path=database_path,
                            **values,
                        )

    def test_duplicate_observation_is_rejected(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"
            self.add_sample_observation(database_path)

            with self.assertRaisesRegex(ObservationError, "已存在相同主题和假设"):
                self.add_sample_observation(database_path)

    def test_pending_observation_can_be_reviewed_without_duplicate_review_rows(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"
            observation = self.add_sample_observation(database_path)

            reviewed = review_observation(
                observation.observation_id,
                "hit",
                actual_result="板块次日上涨 3%",
                review_note="成立条件满足",
                database_path=database_path,
            )
            updated = review_observation(
                observation.observation_id,
                "invalid",
                actual_result="后续发现样本日期不匹配",
                review_note="改为无效样本",
                database_path=database_path,
            )

            connection = sqlite3.connect(database_path)
            try:
                review_count = connection.execute("SELECT COUNT(*) FROM observation_reviews").fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(reviewed.status, "hit")
            self.assertTrue(reviewed.eligible_for_learning)
            self.assertEqual(updated.status, "invalid")
            self.assertFalse(updated.eligible_for_learning)
            self.assertEqual(updated.actual_result, "后续发现样本日期不匹配")
            self.assertEqual(review_count, 1)

    def test_review_reports_unknown_observation(self):
        with TemporaryDirectory() as temp_path:
            database_path = Path(temp_path) / "stock_review.sqlite"

            with self.assertRaisesRegex(ObservationError, "未找到 Observation"):
                review_observation(
                    "OBS-20260706-999",
                    "miss",
                    actual_result="未达到成立条件",
                    database_path=database_path,
                )

    def add_sample_observation(self, database_path: Path):
        return add_observation(
            review_date="2026-07-06",
            topic="机器人板块延续性",
            related_target="机器人板块",
            hypothesis="机器人板块次日保持强势",
            confirmation_condition="板块次日继续放量",
            invalidation_condition="板块跌幅超过 2%",
            evidence_source="2026-07-06 Evidence Snapshot",
            plan_item="计划项 1",
            database_path=database_path,
        )


if __name__ == "__main__":
    unittest.main()
