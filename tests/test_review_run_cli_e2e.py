# 本文件通过真实 CLI 子进程验证新版非参与日报从清单到正式发布的完整合同。

import base64
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from stock_review.cli import build_parser
from stock_review.review_runs.plan_daily_review import plan_daily_review
from stock_review.storage.plan_review_run_migration import TARGET_TABLES


class ReviewRunCliEndToEndTest(unittest.TestCase):
    def test_new_commands_default_to_isolated_review_run_database(self):
        expected = Path("data") / "review_run.sqlite"
        parser = build_parser()

        for arguments in (
            ["daily", "plan", "--date", "2026-09-04", "--capture", "manual:snapshot"],
            ["daily", "show", "--run-id", "RUN-example"],
            ["weekly", "create", "--end-date", "2026-09-04"],
            ["pool", "list"],
            ["observation", "list"],
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(expected, parser.parse_args(arguments).database)

        manifest = plan_daily_review(
            "2026-09-04",
            ("manual:snapshot",),
        )
        self.assertTrue(
            all("review_run.sqlite" in effect for effect in manifest.database_effects)
        )
        self.assertTrue(
            all("stock_review.sqlite" not in effect for effect in manifest.database_effects)
        )

        legacy = parser.parse_args(
            ["review", "create-brief", "--date", "2026-09-04"]
        )
        self.assertEqual(Path("data") / "stock_review.sqlite", legacy.database)

    def test_start_initializes_new_default_database_without_touching_legacy_name(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._run_json(
                "daily",
                "plan",
                "--date",
                "2026-09-04",
                "--capture",
                "manual:snapshot",
                cwd=root,
            )

            started = self._run_json(
                "daily",
                "start",
                "--manifest-base64-stdin",
                "--manifest-hash",
                manifest["content_hash"],
                stdin_value=manifest,
                cwd=root,
            )

            self.assertEqual("collecting", started["state"])
            self.assertTrue((root / "data" / "review_run.sqlite").is_file())
            self.assertFalse((root / "data" / "stock_review.sqlite").exists())
            self.assertTrue((root / "logs" / "review_run.log").is_file())

    def test_plan_accepts_bounded_capture_profile_from_stdin(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            profile = {
                "captures": [
                    {"source": "akshare", "scope": "market"},
                    {"source": "hhxg", "scope": "snapshot"},
                    {"source": "tongdaxin", "scope": "index-history"},
                    {
                        "source": "workbuddy",
                        "scope": "sector-history",
                        "allowed_objects": ["banking"],
                    },
                    {
                        "source": "workbuddy",
                        "scope": "stocks",
                        "allowed_objects": ["000001"],
                    },
                ]
            }

            manifest = self._run_json(
                "daily",
                "plan",
                "--date",
                "2026-09-04",
                "--capture-profile-base64-stdin",
                "--database",
                str(root / "review.sqlite"),
                "--evidence-dir",
                str(root / "evidence"),
                "--output-dir",
                str(root / "reports"),
                stdin_value=profile,
            )

            self.assertEqual(5, len(manifest["captures"]))
            self.assertEqual(["banking"], manifest["captures"][3]["allowed_objects"])
            self.assertEqual(1, manifest["captures"][3]["max_records"])
            self.assertEqual(["000001"], manifest["captures"][4]["allowed_objects"])

    def test_manual_evidence_reaches_one_verified_official_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "review.sqlite"
            evidence_dir = root / "evidence"
            report_dir = root / "reports"
            audit_log = root / "audit.jsonl"
            framework = Path("stock-review.md").resolve()

            manifest = self._run_json(
                "daily",
                "plan",
                "--date",
                "2026-09-04",
                "--capture",
                "manual:snapshot",
                "--database",
                str(database),
                "--evidence-dir",
                str(evidence_dir),
                "--output-dir",
                str(report_dir),
            )
            common = (
                "--database",
                str(database),
                "--audit-log",
                str(audit_log),
            )
            started = self._run_json(
                "daily",
                "start",
                "--manifest-base64-stdin",
                "--manifest-hash",
                manifest["content_hash"],
                *common,
                stdin_value=manifest,
            )
            self.assertEqual("collecting", started["state"])

            slot = manifest["captures"][0]
            envelope = {
                "run_id": manifest["run_id"],
                "trade_date": manifest["trade_date"],
                "sample_date": manifest["trade_date"],
                "source": slot["source"],
                "scope": slot["scope"],
                "operation_id": slot["operation_id"],
                "status": "available",
                "normalized": {
                    "market": {
                        "indices": [{"name": "上证指数", "close": 3800}],
                        "total_amount": 1200000000000,
                    },
                    "sentiment": {
                        "limit_up_count": 60,
                        "limit_down_count": 5,
                        "broken_board_rate": 0.2,
                        "highest_board": 4,
                    },
                    "sectors": [{
                        "sector_id": "banking",
                        "name": "银行",
                        "rank": 1,
                        "daily_bars": [
                            {"date": "2026-09-03", "change_percent": 0.8},
                            {"date": "2026-09-04", "change_percent": 1.2},
                        ],
                    }],
                    "stocks": [
                        {
                            "code": "000001",
                            "name": "平安银行",
                            "exchange": "SZSE",
                            "sector_id": "banking",
                            "role": "容量核心",
                            "role_source": "manual:confirmed",
                            "daily_bars": [
                                {"date": "2026-09-03", "close": 12.2},
                                {"date": "2026-09-04", "close": 12.5},
                            ],
                        },
                        {
                            "code": "000002",
                            "name": "万科A",
                            "exchange": "SZSE",
                            "sector_id": "banking",
                            "role": "风格样本",
                            "role_source": "manual:confirmed",
                            "daily_bars": [
                                {"date": "2026-09-03", "close": 8.3},
                                {"date": "2026-09-04", "close": 8.5},
                            ],
                        },
                    ],
                    "events": [],
                },
                "missing_fields": [],
                "raw": {"records": []},
            }
            attached = self._run_json(
                "daily",
                "attach-evidence",
                "--run-id",
                manifest["run_id"],
                "--date",
                manifest["trade_date"],
                "--manifest-hash",
                manifest["content_hash"],
                "--operation-id",
                slot["operation_id"],
                "--input-base64-stdin",
                *common,
                stdin_value=envelope,
            )
            self.assertEqual("available", attached["capture_slots"][0]["status"])

            sealed = self._run_json(
                "daily",
                "seal-evidence",
                "--run-id",
                manifest["run_id"],
                "--date",
                manifest["trade_date"],
                "--manifest-hash",
                manifest["content_hash"],
                *common,
            )
            self.assertEqual("evidence_ready", sealed["run"]["state"])

            draft = self._run_json(
                "daily",
                "complete-local",
                "--run-id",
                manifest["run_id"],
                "--date",
                manifest["trade_date"],
                "--manifest-hash",
                manifest["content_hash"],
                "--expected-current-revision-id",
                "null",
                "--operation-id",
                "OP-cli-local",
                "--framework",
                str(framework),
                *common,
            )
            self.assertFalse(
                any(draft["steps"][index]["evidence_gaps"] for index in range(6))
            )
            rejected = self._run_process(
                "daily", "propose-actions", "--run-id", manifest["run_id"],
                "--review-revision-id", draft["review_revision_id"],
                "--selections-base64-stdin", "--database", str(database),
                "--output-dir", str(report_dir),
                stdin_value={"participation_decision": "non_participation",
                             "decision_note": "量能不足，暂不参与。",
                             "approved_plan_item_ids": [], "rejected_plan_items": {}},
            )
            self.assertEqual(1, rejected.returncode)
            self.assertIn("通用模板", rejected.stderr)
            reviewed_steps = []
            for step in draft["steps"]:
                reviewed = dict(
                    step,
                    conclusion=step["title"] + "：核心证据已核对，未形成符合框架的行动机会。",
                    open_questions=[],
                )
                if step["step_number"] in {1, 2, 3, 4, 5, 6, 9}:
                    reviewed["status"] = "complete"
                if step["step_number"] == 9:
                    reviewed["pattern_assessments"] = [
                        dict(pattern, status="not_matched", object_ids=[])
                        for pattern in step["pattern_assessments"]
                    ]
                reviewed_steps.append(reviewed)
            draft = self._run_json(
                "daily", "revise", "--run-id", manifest["run_id"],
                "--base-revision-id", draft["review_revision_id"],
                "--expected-current-revision-id", draft["review_revision_id"],
                "--operation-id", "OP-cli-reviewed", "--edits-base64-stdin", *common,
                stdin_value={
                    "steps": reviewed_steps,
                    "candidate_plan_items": [],
                    "decision_note": "核心证据已核对，未发现符合框架的行动机会。",
                },
            )
            proposal_result = self._run_json(
                "daily",
                "propose-actions",
                "--run-id",
                manifest["run_id"],
                "--review-revision-id",
                draft["review_revision_id"],
                "--selections-base64-stdin",
                "--database",
                str(database),
                "--output-dir",
                str(report_dir),
                stdin_value={
                    "participation_decision": "non_participation",
                    "decision_note": "核心证据充分，用户确认次日没有合格行动对象。",
                    "approved_plan_item_ids": [],
                    "rejected_plan_items": {},
                },
            )
            proposal = proposal_result["proposal"]
            finalized = self._run_json(
                "daily",
                "finalize",
                "--proposal-base64-stdin",
                "--proposal-hash",
                proposal["content_hash"],
                *common,
                stdin_value=proposal,
            )

            verified = self._run_json(
                "daily",
                "verify",
                "--run-id",
                manifest["run_id"],
                "--database",
                str(database),
            )
            shown = self._run_json(
                "daily",
                "show",
                "--run-id",
                manifest["run_id"],
                "--database",
                str(database),
            )

            self.assertTrue(verified["valid"], verified)
            self.assertEqual("published", shown["run"]["state"])
            self.assertEqual(
                finalized["publication_id"], shown["publication"]["publication_id"]
            )
            self.assertTrue(Path(finalized["report_path"]).is_file())
            content = Path(finalized["report_path"]).read_text(encoding="utf-8")
            self.assertNotIn("上游已验证输入为空", content)
            self.assertNotIn("已冻结本步相关事实", content)
            self.assertIn("0%（空仓，不参与）", content)
            self.assertIn("未使用正式模型通道", content)
            self.assertLess(content.index("## STEP 10:"), content.index("## 证据与版本追溯"))
            log_lines = audit_log.read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(log_lines), 5)
            self.assertEqual(len(log_lines), len({json.loads(line)["outbox_id"] for line in log_lines}))

    def test_unmigrated_database_returns_domain_error_for_new_commands(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "legacy.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE evidence_snapshots (trade_date TEXT PRIMARY KEY)"
                )
                connection.commit()
            finally:
                connection.close()

            commands = (
                ("observation", "list", "--database", str(database)),
                ("pool", "list", "--database", str(database)),
                (
                    "daily",
                    "show",
                    "--run-id",
                    "RUN-missing",
                    "--database",
                    str(database),
                ),
                (
                    "weekly",
                    "create",
                    "--end-date",
                    "2026-09-04",
                    "--database",
                    str(database),
                    "--output-dir",
                    str(root / "weekly"),
                    "--audit-log",
                    str(root / "audit.jsonl"),
                ),
                (
                    "database",
                    "project-audit-log",
                    "--database",
                    str(database),
                    "--log",
                    str(root / "audit.jsonl"),
                ),
            )
            for command in commands:
                with self.subTest(command=command):
                    completed = self._run_process(*command)
                    self.assertEqual(1, completed.returncode)
                    self.assertIn("尚未迁移到 ReviewRun schema", completed.stderr)
                    self.assertNotIn("Traceback", completed.stderr)
                    self.assertEqual("", completed.stdout)

    def test_incomplete_current_schema_returns_domain_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "incomplete.sqlite"
            self._create_incomplete_review_run_database(database)

            completed = self._run_process(
                "daily",
                "show",
                "--run-id",
                "RUN-missing",
                "--database",
                str(database),
            )

            self.assertEqual(1, completed.returncode)
            self.assertIn("ReviewRun 数据库结构不完整", completed.stderr)
            self.assertIn("run_manifests", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertEqual("", completed.stdout)

    def test_malformed_current_schema_returns_domain_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "malformed.sqlite"
            connection = sqlite3.connect(database)
            try:
                for table_name in TARGET_TABLES:
                    if table_name == "schema_versions":
                        connection.execute("CREATE TABLE schema_versions (version INTEGER)")
                        connection.execute("INSERT INTO schema_versions VALUES (1)")
                    else:
                        connection.execute(f'CREATE TABLE "{table_name}" (id INTEGER)')
                connection.commit()
            finally:
                connection.close()

            completed = self._run_process(
                "pool",
                "list",
                "--database",
                str(database),
            )

            self.assertEqual(1, completed.returncode)
            self.assertIn("ReviewRun 数据库结构不完整", completed.stderr)
            self.assertIn("列定义不匹配", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertEqual("", completed.stdout)

    def test_review_run_command_groups_require_nested_command(self):
        for command in ("daily", "weekly", "database"):
            with self.subTest(command=command):
                completed = self._run_process(command)
                self.assertNotEqual(0, completed.returncode)
                self.assertIn("required", completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)

    def _run_json(self, *arguments: str, stdin_value=None, cwd=None):
        completed = self._run_process(*arguments, stdin_value=stdin_value, cwd=cwd)
        command = [sys.executable, "-m", "stock_review.cli", *arguments]
        self.assertEqual(
            0,
            completed.returncode,
            f"命令失败：{' '.join(command)}\nstdout={completed.stdout}\nstderr={completed.stderr}",
        )
        return json.loads(completed.stdout)

    def _run_process(self, *arguments: str, stdin_value=None, cwd=None):
        command = [sys.executable, "-m", "stock_review.cli", *arguments]
        encoded_input = None
        if stdin_value is not None:
            encoded_input = base64.b64encode(
                json.dumps(
                    stdin_value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).decode("ascii")
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            command,
            cwd=cwd or Path.cwd(),
            input=encoded_input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )
        return completed

    @staticmethod
    def _create_incomplete_review_run_database(database_path: Path) -> None:
        retained_tables = (
            "review_runs",
            "review_publications",
            "current_review_publications",
            "pool_status_events",
            "current_pool_status",
            "observations",
            "observation_result_revisions",
            "current_observation_results",
            "audit_outbox",
        )
        connection = sqlite3.connect(database_path)
        try:
            connection.execute("CREATE TABLE schema_versions (version INTEGER)")
            connection.execute("INSERT INTO schema_versions VALUES (1)")
            for table_name in retained_tables:
                connection.execute(f'CREATE TABLE "{table_name}" (id INTEGER)')
            connection.commit()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
