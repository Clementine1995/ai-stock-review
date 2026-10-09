# 本文件验证每日复盘脚本的 DryRun 权限清单及无副作用边界。

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class DailyReviewScriptTest(unittest.TestCase):
    # 测试统一使用 Windows PowerShell，保持与 WorkBuddy 调用方式一致。
    @classmethod
    def setUpClass(cls):
        cls.repository_root = Path(__file__).resolve().parents[1]
        cls.script_path = cls.repository_root / "scripts" / "run_daily_review.ps1"
        cls.powershell_path = shutil.which("powershell.exe") or shutil.which("powershell")

    # 每个用例在无虚拟环境的临时工作区运行，执行 CLI 会直接暴露为失败。
    def setUp(self):
        if self.powershell_path is None:
            self.skipTest("当前环境没有 Windows PowerShell。")

        self.trade_date = "2026-08-14"
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name)
        self.copied_script = self.workspace / "scripts" / self.script_path.name
        self.copied_script.parent.mkdir(parents=True)
        shutil.copy2(self.script_path, self.copied_script)

    # 外部 -File 调用返回原始 stdout，供各行为用例解析。
    def run_dry_run(self, *additional_arguments):
        return subprocess.run(
            [
                self.powershell_path,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.copied_script),
                "-TradeDate",
                self.trade_date,
                "-DryRun",
                *additional_arguments,
            ],
            cwd=self.workspace,
            capture_output=True,
            check=False,
        )

    # 权限清单必须是 stdout 中唯一且合法的 UTF-8 JSON。
    def parse_manifest(self, result):
        stdout = result.stdout.decode("utf-8-sig")
        stderr = result.stderr.decode("utf-8-sig", errors="replace")
        self.assertEqual(0, result.returncode, stderr)
        self.assertEqual(1, len(stdout.splitlines()))
        return json.loads(stdout)

    # DryRun 不得创建任何真实运行目录或虚拟环境。
    def assert_no_runtime_artifacts(self):
        self.assertFalse((self.workspace / ".venv").exists())
        self.assertFalse((self.workspace / "data").exists())
        self.assertFalse((self.workspace / "logs").exists())
        self.assertFalse((self.workspace / "reports").exists())

    # 默认 DryRun 同时验证人读命令和机器可核验的 arguments。
    def test_dry_run_outputs_permission_manifest_without_executing_commands(self):
        manifest = self.parse_manifest(self.run_dry_run())
        self.assertEqual(1, manifest["schema_version"])
        self.assertEqual(
            "stock_review.daily_review.permission_manifest",
            manifest["kind"],
        )
        self.assertTrue(manifest["dry_run"])
        self.assertFalse(manifest["executes_commands"])
        self.assertEqual(self.trade_date, manifest["trade_date"])
        self.assertFalse(manifest["includes_llm"])
        self.assertEqual(["akshare", "hhxg"], manifest["network_sources"])

        steps = manifest["steps"]
        self.assertEqual(5, len(steps))
        command_prefix = ".\\.venv\\Scripts\\python.exe -m stock_review.cli "
        for step in steps:
            self.assertIsInstance(step["arguments"], list)
            self.assertEqual(
                command_prefix + " ".join(step["arguments"]),
                step["command"],
            )
            self.assertIn(self.trade_date, step["arguments"])
        self.assertEqual(["evidence", "collect-daily"], steps[0]["arguments"][:2])
        self.assertEqual(["akshare"], steps[0]["network_sources"])
        self.assertEqual(["hhxg"], steps[1]["network_sources"])

        self.assertEqual(
            {
                f"data/evidence/{self.trade_date}_snapshot.json",
                f"data/evidence/{self.trade_date}_review_candidate_history.json",
                f"reports/daily/{self.trade_date}_brief_review.md",
                "logs/stock_review.log",
            },
            set(manifest["file_writes"]),
        )
        self.assertEqual(["data/stock_review.sqlite"], manifest["database_writes"])
        self.assert_no_runtime_artifacts()

    # 显式 WithLlm 只扩展权限清单，不得在 DryRun 中调用模型或创建产物。
    def test_dry_run_with_llm_includes_detailed_review_step_without_side_effects(self):
        manifest = self.parse_manifest(self.run_dry_run("-WithLlm"))
        self.assertTrue(manifest["includes_llm"])
        self.assertEqual(
            ["akshare", "hhxg", "openai-compatible-llm"],
            manifest["network_sources"],
        )
        self.assertEqual(6, len(manifest["steps"]))

        detailed_step = manifest["steps"][-1]
        self.assertEqual(
            ["review", "create-detailed"],
            detailed_step["arguments"][:2],
        )
        self.assertEqual(
            ["openai-compatible-llm"],
            detailed_step["network_sources"],
        )
        self.assertIn(
            f"reports/daily/{self.trade_date}_detailed_review.md",
            detailed_step["file_writes"],
        )
        self.assertIn(
            f"reports/daily/{self.trade_date}_detailed_review.md",
            manifest["file_writes"],
        )
        self.assertFalse(manifest["executes_commands"])
        self.assert_no_runtime_artifacts()

    # PowerShell 原生调用必须能从 success pipeline 捕获并解析清单。
    def test_dry_run_supports_native_powershell_capture_and_json_parsing(self):
        environment = os.environ.copy()
        environment["STOCK_REVIEW_SCRIPT_UNDER_TEST"] = str(self.copied_script)
        command = "\n".join(
            [
                "$manifestText = & $env:STOCK_REVIEW_SCRIPT_UNDER_TEST "
                f"-TradeDate '{self.trade_date}' -DryRun",
                "$manifest = $manifestText | ConvertFrom-Json",
                "Write-Output ($manifest | ConvertTo-Json -Depth 8 -Compress)",
            ]
        )
        result = subprocess.run(
            [
                self.powershell_path,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=self.workspace,
            env=environment,
            capture_output=True,
            check=False,
        )

        manifest = self.parse_manifest(result)
        self.assertEqual(
            "stock_review.daily_review.permission_manifest",
            manifest["kind"],
        )
        self.assertEqual(self.trade_date, manifest["trade_date"])
        self.assertEqual(5, len(manifest["steps"]))
        self.assert_no_runtime_artifacts()


if __name__ == "__main__":
    unittest.main()
