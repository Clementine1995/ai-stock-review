from pathlib import Path
import re
import unittest


class WorkBuddySkillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill_path = (
            Path(__file__).resolve().parents[1]
            / "integrations"
            / "workbuddy"
            / "stock-review"
            / "SKILL.md"
        )
        cls.content = cls.skill_path.read_text(encoding="utf-8")

    def test_skill_metadata_matches_agent_skills_specification(self):
        self.assertTrue(self.content.startswith("---\n"))
        frontmatter = self.content.split("---", maxsplit=2)[1]
        name_match = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
        description_match = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)

        self.assertIsNotNone(name_match)
        self.assertIsNotNone(description_match)
        name = name_match.group(1).strip()
        description = description_match.group(1).strip()
        self.assertEqual("stock-review", name)
        self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        self.assertLessEqual(len(name), 64)
        self.assertLessEqual(len(description), 1024)

    def test_skill_requires_preview_and_confirmation_before_real_run(self):
        preview_command = (
            'powershell -NoProfile -ExecutionPolicy Bypass -File '
            '".\\scripts\\run_daily_review.ps1" -TradeDate "YYYY-MM-DD" -DryRun'
        )
        real_command = (
            'powershell -NoProfile -ExecutionPolicy Bypass -File '
            '".\\scripts\\run_daily_review.ps1" -TradeDate "YYYY-MM-DD"'
        )

        self.assertIn(preview_command, self.content)
        self.assertIn(real_command, self.content)
        preview_position = self.content.index(preview_command)
        real_position = self.content.index(
            real_command,
            preview_position + len(preview_command),
        )
        self.assertLess(preview_position, real_position)
        self.assertIn("只有用户在当前任务中明确同意后", self.content)

    def test_skill_keeps_database_and_observation_writes_explicit(self):
        self.assertIn("不直接访问、编辑或执行 SQL 操作", self.content)
        self.assertIn("不得根据决策卡或 STEP 8 自动创建 Observation", self.content)
        self.assertIn("是否确认创建这条 Observation", self.content)
        self.assertIn("不得自行执行 `observation migrate-review-governance`", self.content)
        self.assertIn("不使用 `-WithLlm`", self.content)


if __name__ == "__main__":
    unittest.main()
