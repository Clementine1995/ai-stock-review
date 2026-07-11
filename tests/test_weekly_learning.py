from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_review.learning.summarize_weekly_learning import create_weekly_learning
from stock_review.observations.manage_observation import (
    ObservationError,
    add_observation,
    review_observation,
)


class WeeklyLearningTest(unittest.TestCase):
    def test_weekly_learning_separates_samples_and_excludes_invalid_from_candidates(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            hit = self.add_observation(database_path, "机器人延续", "机器人板块保持强势")
            miss = self.add_observation(database_path, "旅游延续", "旅游板块保持强势")
            invalid = self.add_observation(database_path, "软件延续", "软件板块保持强势")
            self.add_observation(database_path, "油气延续", "油气板块保持强势")
            review_observation(
                hit.observation_id,
                "hit",
                "板块次日上涨 3%",
                database_path=database_path,
            )
            review_observation(
                miss.observation_id,
                "miss",
                "板块次日下跌 2%",
                database_path=database_path,
            )
            review_observation(
                invalid.observation_id,
                "invalid",
                "样本日期不匹配",
                database_path=database_path,
            )

            output_path = create_weekly_learning(
                "2026-07-06",
                "2026-07-10",
                database_path=database_path,
                output_dir=root / "weekly",
                log_path=root / "stock_review.log",
            )
            content = output_path.read_text(encoding="utf-8")
            candidate_content = content.split("## 经验候选", maxsplit=1)[1]

            self.assertIn("命中：1；失败：1；无效：1；待观察：1", content)
            self.assertIn(hit.observation_id, candidate_content)
            self.assertIn(miss.observation_id, candidate_content)
            self.assertNotIn(invalid.observation_id, candidate_content)
            self.assertIn("软件延续", content)
            self.assertIn("油气延续", content)
            self.assertTrue(output_path.exists())

    def test_weekly_learning_reports_insufficient_reviewed_samples(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            database_path = root / "stock_review.sqlite"
            self.add_observation(database_path, "机器人延续", "机器人板块保持强势")

            output_path = create_weekly_learning(
                "2026-07-06",
                "2026-07-10",
                database_path=database_path,
                output_dir=root / "weekly",
                log_path=root / "stock_review.log",
            )
            content = output_path.read_text(encoding="utf-8")

            self.assertIn("有效回填样本不足", content)
            self.assertIn("无有效候选", content)

    def test_weekly_learning_rejects_reversed_date_range(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)

            with self.assertRaisesRegex(ObservationError, "开始日期不能晚于结束日期"):
                create_weekly_learning(
                    "2026-07-10",
                    "2026-07-06",
                    database_path=root / "stock_review.sqlite",
                    output_dir=root / "weekly",
                    log_path=root / "stock_review.log",
                )

    def add_observation(self, database_path: Path, topic: str, hypothesis: str):
        return add_observation(
            review_date="2026-07-06",
            topic=topic,
            related_target=topic,
            hypothesis=hypothesis,
            confirmation_condition="次日继续放量上涨",
            invalidation_condition="次日跌幅超过 2%",
            evidence_source="2026-07-06 Evidence Snapshot",
            database_path=database_path,
        )


if __name__ == "__main__":
    unittest.main()
