from pathlib import Path
import unittest

from stock_review.review_framework.parse_framework import (
    FrameworkParseError,
    parse_framework_file,
    parse_framework_text,
)


class ReviewFrameworkTest(unittest.TestCase):
    def test_current_framework_steps_are_fully_identified(self):
        framework = parse_framework_file(Path("stock-review.md"))

        self.assertEqual(len(framework.steps), 10)
        self.assertEqual([step.number for step in framework.steps], list(range(1, 11)))
        self.assertEqual(framework.steps[0].title, "判断大盘所属阶段")
        self.assertEqual(framework.steps[-1].title, "最终输出复盘文档")

    def test_markdown_without_step_reports_clear_error(self):
        content = "# 普通文档\n\n没有复盘 STEP 标题。"

        with self.assertRaisesRegex(FrameworkParseError, "未识别到 STEP 标题"):
            parse_framework_text(content)


if __name__ == "__main__":
    unittest.main()
