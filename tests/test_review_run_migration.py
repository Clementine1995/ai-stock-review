# 本文件用临时旧库验证迁移计划只读、显式确认、备份、旧记录登记和幂等重放。

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.storage.plan_review_run_migration import (
    TARGET_TABLES,
    migrate_review_run_database,
    plan_review_run_migration,
    require_review_run_schema,
)
from stock_review.storage.review_run_repository import ReviewRunRepository


class ReviewRunMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "legacy.sqlite"
        self.backup_path = self.root / "backup" / "legacy.sqlite"
        self.evidence_dir = self.root / "evidence"
        self.daily_dir = self.root / "daily"
        self.weekly_dir = self.root / "weekly"
        self._create_legacy_database()
        self.evidence_dir.mkdir()
        self.daily_dir.mkdir()
        self.weekly_dir.mkdir()
        (self.evidence_dir / "2026-07-06_sample.json").write_text(
            '{"trade_date":"2026-07-06"}\n', encoding="utf-8"
        )
        (self.daily_dir / "2026-08-17_detailed_review.experimental.bak").write_text(
            "实验报告\n", encoding="utf-8"
        )
        self.source_hash = self._sha256(self.database_path)

    def test_plan_is_read_only_and_confirmed_migration_preserves_legacy_records(self):
        plan = self._plan()

        self.assertEqual(self.source_hash, self._sha256(self.database_path))
        self.assertFalse(self.backup_path.exists())
        self.assertTrue(plan.rename_legacy_observations)
        self.assertEqual(4, plan.estimated_legacy_import_count)
        with self.assertRaisesRegex(ReviewRunDomainError, "--confirmed"):
            migrate_review_run_database(plan, plan.content_hash, confirmed=False)

        result = migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        self.assertFalse(result["replayed"])
        self.assertEqual(plan.database_sha256, result["backup_snapshot_sha256"])
        self.assertTrue(self.backup_path.is_file())
        backup_connection = sqlite3.connect(self.backup_path)
        try:
            self.assertEqual(
                "ok",
                backup_connection.execute("PRAGMA integrity_check").fetchone()[0],
            )
            self.assertEqual(
                1,
                backup_connection.execute(
                    "SELECT COUNT(*) FROM evidence_snapshots"
                ).fetchone()[0],
            )
        finally:
            backup_connection.close()
        backup_plan = plan_review_run_migration(
            self.backup_path,
            1,
            self.root / "unused-backup.sqlite",
            evidence_dir=self.root / "missing-evidence",
            daily_report_dir=self.root / "missing-daily",
            weekly_report_dir=self.root / "missing-weekly",
            now=datetime(2026, 9, 5, 8, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(plan.database_sha256, backup_plan.database_sha256)
        repository = ReviewRunRepository(self.database_path)
        self.assertEqual(1, repository.list_schema_versions()[-1]["version"])
        imports = repository.list_legacy_imports()
        self.assertEqual(5, len(imports))
        self.assertEqual(
            {"sample", "experimental", "legacy", "applied"},
            {item["classification"] for item in imports},
        )
        connection = sqlite3.connect(self.database_path)
        try:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("legacy_observations_v0", names)
            self.assertIn("observations", names)
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM legacy_observations_v0"
                ).fetchone()[0],
            )
        finally:
            connection.close()

        replay = migrate_review_run_database(plan, plan.content_hash, confirmed=True)
        self.assertTrue(replay["replayed"])
        self.assertEqual(5, len(repository.list_legacy_imports()))

    def test_plan_classifies_report_in_revised_directory_as_superseded(self):
        revised_dir = self.daily_dir / "revised"
        revised_dir.mkdir()
        revised_report = revised_dir / "2026-07-17_detailed_review.md"
        revised_report.write_text("修订报告\n", encoding="utf-8")

        plan = self._plan()
        entry = next(
            item
            for item in plan.artifact_entries
            if Path(item["path"]) == revised_report.resolve()
        )

        self.assertEqual("superseded", entry["classification"])

    def test_current_schema_plan_is_idempotent_without_importing_new_artifacts(self):
        first_plan = self._plan()
        migrate_review_run_database(first_plan, first_plan.content_hash, confirmed=True)
        formal_artifact = self.daily_dir / "2026-09-05-official.md"
        formal_artifact.write_text("新版正式日报\n", encoding="utf-8")
        second_plan = plan_review_run_migration(
            self.database_path,
            1,
            self.root / "backup" / "current.sqlite",
            evidence_dir=self.evidence_dir,
            daily_report_dir=self.daily_dir,
            weekly_report_dir=self.weekly_dir,
            now=datetime(2026, 9, 5, 9, 1, tzinfo=timezone.utc),
        )

        self.assertEqual((), second_plan.artifact_entries)
        self.assertEqual({}, second_plan.table_counts)
        result = migrate_review_run_database(
            second_plan,
            second_plan.content_hash,
            confirmed=True,
        )
        replay = migrate_review_run_database(
            second_plan,
            second_plan.content_hash,
            confirmed=True,
        )

        self.assertFalse(result["replayed"])
        self.assertTrue(replay["replayed"])
        imports = ReviewRunRepository(self.database_path).list_legacy_imports()
        self.assertNotIn(str(formal_artifact.resolve()), {item["source_key"] for item in imports})

    def test_changed_input_is_rejected_before_database_write(self):
        plan = self._plan()
        evidence_path = self.evidence_dir / "2026-07-06_sample.json"
        evidence_path.write_text("已变化\n", encoding="utf-8")

        with self.assertRaisesRegex(ReviewRunDomainError, "历史产物"):
            migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        self.assertEqual(self.source_hash, self._sha256(self.database_path))
        self.assertFalse(self.backup_path.exists())

    def test_schema_readiness_rejects_correct_version_with_missing_tables(self):
        incomplete_database = self.root / "incomplete.sqlite"
        self._create_incomplete_review_run_database(incomplete_database)

        with self.assertRaises(ReviewRunDomainError) as raised:
            require_review_run_schema(incomplete_database)

        message = str(raised.exception)
        self.assertIn("ReviewRun 数据库结构不完整", message)
        self.assertIn("run_manifests", message)
        self.assertIn("action_sets", message)
        self.assertIn("weekly_revisions", message)

    def test_schema_readiness_rejects_all_named_tables_with_wrong_structure(self):
        malformed_database = self.root / "malformed.sqlite"
        connection = sqlite3.connect(malformed_database)
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

        with self.assertRaisesRegex(ReviewRunDomainError, "列定义不匹配"):
            require_review_run_schema(malformed_database)

    def test_schema_readiness_rejects_missing_table_check_constraint(self):
        database = self.root / "missing-check.sqlite"
        ReviewRunRepository(database).initialize_schema()
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA writable_schema = ON")
            connection.execute(
                """
                UPDATE sqlite_master
                SET sql = replace(sql, ' CHECK (sequence_number > 0)', '')
                WHERE type = 'table' AND name = 'review_run_events'
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ReviewRunDomainError, "建表约束不匹配"):
            require_review_run_schema(database)

    def test_schema_readiness_rejects_missing_index_and_immutable_trigger(self):
        cases = (
            (
                "missing-index.sqlite",
                "DROP INDEX review_runs_trade_date_idx",
                "缺少必需索引",
            ),
            (
                "missing-trigger.sqlite",
                "DROP TRIGGER review_runs_reject_update",
                "不可变触发器",
            ),
        )
        for filename, mutation, expected_message in cases:
            with self.subTest(mutation=mutation):
                database = self.root / filename
                ReviewRunRepository(database).initialize_schema()
                connection = sqlite3.connect(database)
                try:
                    connection.execute(mutation)
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaisesRegex(ReviewRunDomainError, expected_message):
                    require_review_run_schema(database)

    def test_replay_requires_existing_backup(self):
        plan = self._plan()
        migrate_review_run_database(plan, plan.content_hash, confirmed=True)
        self.backup_path.unlink()

        with self.assertRaisesRegex(ReviewRunDomainError, "备份文件不存在"):
            migrate_review_run_database(plan, plan.content_hash, confirmed=True)

    def test_replay_rejects_replaced_backup(self):
        plan = self._plan()
        migrate_review_run_database(plan, plan.content_hash, confirmed=True)
        self.backup_path.unlink()
        connection = sqlite3.connect(self.backup_path)
        try:
            connection.execute("CREATE TABLE replacement (value TEXT)")
            connection.execute("INSERT INTO replacement VALUES ('different')")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ReviewRunDomainError, "备份逻辑快照"):
            migrate_review_run_database(plan, plan.content_hash, confirmed=True)

    def test_replay_accepts_valid_backup(self):
        plan = self._plan()
        first = migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        replay = migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(plan.database_sha256, replay["backup_snapshot_sha256"])

    def test_replay_rejects_newer_current_schema_version(self):
        plan = self._plan()
        migrate_review_run_database(plan, plan.content_hash, confirmed=True)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "INSERT INTO schema_versions VALUES (?, ?, ?)",
                (2, "模拟后续升级", "2026-09-05T09:00:00+00:00"),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ReviewRunDomainError, "实际 schema version 为 2"):
            migrate_review_run_database(plan, plan.content_hash, confirmed=True)

    def test_replay_rejects_current_schema_with_missing_required_table(self):
        plan = self._plan()
        migrate_review_run_database(plan, plan.content_hash, confirmed=True)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("DROP TABLE current_review_publications")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ReviewRunDomainError, "缺少表 current_review_publications"):
            migrate_review_run_database(plan, plan.content_hash, confirmed=True)

    def test_failure_after_backup_rolls_back_original_database_snapshot(self):
        plan = self._plan()
        with patch.object(
            ReviewRunRepository,
            "register_legacy_import",
            side_effect=RuntimeError("模拟登记失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "模拟登记失败"):
                migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        self.assertTrue(self.backup_path.is_file())
        connection = sqlite3.connect(self.database_path)
        try:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertIn("observations", names)
        self.assertNotIn("review_runs", names)

    def test_migration_holds_cross_connection_writer_gate_until_commit(self):
        plan = self._plan()
        original_register = ReviewRunRepository.register_legacy_import
        writer_attempts: list[str] = []

        def register_with_competing_write(repository, *args, **kwargs):
            if not writer_attempts:
                competing = sqlite3.connect(self.database_path, timeout=0)
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        competing.execute(
                            "INSERT INTO evidence_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                "2026-09-01",
                                "concurrent",
                                "2026-09-01",
                                "{}",
                                "[]",
                                "2026-09-01T16:00:00+08:00",
                            ),
                        )
                    writer_attempts.append("blocked")
                finally:
                    competing.close()
            return original_register(repository, *args, **kwargs)

        with patch.object(
            ReviewRunRepository,
            "register_legacy_import",
            new=register_with_competing_write,
        ):
            result = migrate_review_run_database(
                plan,
                plan.content_hash,
                confirmed=True,
            )

        self.assertEqual(["blocked"], writer_attempts)
        self.assertFalse(result["replayed"])
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM evidence_snapshots WHERE trade_date = '2026-09-01'"
                ).fetchone()[0],
            )
        finally:
            connection.close()

    def test_migration_rolls_back_when_legacy_database_has_conflicting_target_table(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("CREATE TABLE current_review_publications (id INTEGER)")
            connection.commit()
        finally:
            connection.close()
        plan = self._plan()

        with self.assertRaisesRegex(ReviewRunDomainError, "结构合同校验失败"):
            migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        connection = sqlite3.connect(self.database_path)
        try:
            names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            columns = [
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(current_review_publications)"
                )
            ]
        finally:
            connection.close()
        self.assertIn("observations", names)
        self.assertNotIn("review_runs", names)
        self.assertEqual(["id"], columns)

    def test_plan_and_migrate_reject_hardlink_backup_alias(self):
        hardlink_path = self.root / "hardlink.sqlite"
        try:
            os.link(self.database_path, hardlink_path)
        except OSError as error:
            self.skipTest(f"当前文件系统不支持硬链接：{error}")
        with self.assertRaisesRegex(ReviewRunDomainError, "指向同一文件"):
            plan_review_run_migration(
                self.database_path,
                1,
                hardlink_path,
                evidence_dir=self.evidence_dir,
                daily_report_dir=self.daily_dir,
                weekly_report_dir=self.weekly_dir,
                now=datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc),
            )

        plan = self._plan()
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        os.link(self.database_path, self.backup_path)
        with self.assertRaisesRegex(ReviewRunDomainError, "指向同一文件"):
            migrate_review_run_database(plan, plan.content_hash, confirmed=True)

    def test_plan_and_migrate_reject_database_sidecar_as_backup(self):
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                sidecar_path = Path(f"{self.database_path}{suffix}")
                with self.assertRaisesRegex(ReviewRunDomainError, "sidecar"):
                    plan_review_run_migration(
                        self.database_path,
                        1,
                        sidecar_path,
                        evidence_dir=self.evidence_dir,
                        daily_report_dir=self.daily_dir,
                        weekly_report_dir=self.weekly_dir,
                        now=datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc),
                    )
                plan = self._plan()
                changed = replace(plan, backup_path=str(sidecar_path), content_hash="")
                changed = replace(changed, content_hash=changed.calculate_hash())
                with self.assertRaisesRegex(ReviewRunDomainError, "sidecar"):
                    migrate_review_run_database(
                        changed,
                        changed.content_hash,
                        confirmed=True,
                    )

    def test_backup_identity_is_rechecked_after_snapshot_creation(self):
        plan = self._plan()
        original_snapshot = __import__(
            "stock_review.storage.plan_review_run_migration",
            fromlist=["_write_consistent_database_snapshot"],
        )._write_consistent_database_snapshot

        def replace_snapshot_with_hardlink(source_path, target_path):
            original_snapshot(source_path, target_path)
            Path(target_path).unlink()
            os.link(source_path, target_path)

        with patch(
            "stock_review.storage.plan_review_run_migration._write_consistent_database_snapshot",
            side_effect=replace_snapshot_with_hardlink,
        ):
            with self.assertRaisesRegex(ReviewRunDomainError, "指向同一文件"):
                migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        connection = sqlite3.connect(self.database_path)
        try:
            names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertNotIn("review_runs", names)

    def test_backup_publish_does_not_overwrite_competing_file(self):
        plan = self._plan()
        original_link = os.link
        occupied = False

        def occupy_target_before_link(source_path, target_path):
            nonlocal occupied
            if Path(target_path) == self.backup_path and not occupied:
                occupied = True
                competing = sqlite3.connect(target_path)
                try:
                    competing.execute("CREATE TABLE competing_backup (id INTEGER)")
                    competing.commit()
                finally:
                    competing.close()
            return original_link(source_path, target_path)

        with patch(
            "stock_review.storage.plan_review_run_migration.os.link",
            side_effect=occupy_target_before_link,
        ):
            with self.assertRaisesRegex(ReviewRunDomainError, "不能覆盖"):
                migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        connection = sqlite3.connect(self.backup_path)
        try:
            names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertTrue(occupied)
        self.assertEqual({"competing_backup"}, names)

    def test_added_artifact_after_plan_is_rejected(self):
        plan = self._plan()
        (self.evidence_dir / "added-after-plan.json").write_text(
            '{"new":true}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ReviewRunDomainError, "历史产物清单"):
            migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        self.assertFalse(self.backup_path.exists())

    def test_interrupted_transaction_rolls_back_and_can_resume_from_backup(self):
        plan = self._plan()
        plan_path = self.root / "migration-plan.json"
        plan_path.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )
        script = """
import json
import os
from pathlib import Path
import sys

from stock_review.storage.plan_review_run_migration import MigrationPlan, migrate_review_run_database
from stock_review.storage.review_run_repository import ReviewRunRepository

plan = MigrationPlan.from_dict(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))

def interrupt_registration(_repository, *_args, **_kwargs):
    os._exit(91)

ReviewRunRepository.register_legacy_import = interrupt_registration
migrate_review_run_database(plan, plan.content_hash, confirmed=True)
"""
        interrupted = subprocess.run(
            [sys.executable, "-c", script, str(plan_path)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(91, interrupted.returncode, interrupted.stderr)
        self.assertTrue(self.backup_path.is_file())

        result = migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        self.assertFalse(result["replayed"])
        repository = ReviewRunRepository(self.database_path)
        self.assertEqual(1, repository.list_schema_versions()[-1]["version"])
        self.assertEqual(5, len(repository.list_legacy_imports()))

    def test_wal_backup_includes_committed_pages_before_checkpoint(self):
        self._append_uncheckpointed_wal_record()
        self.assertTrue(Path(f"{self.database_path}-wal").is_file())
        plan = self._plan()

        migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        connection = sqlite3.connect(self.backup_path)
        try:
            rows = connection.execute(
                "SELECT trade_date FROM evidence_snapshots ORDER BY trade_date"
            ).fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual([("2026-07-06",), ("2026-08-18",)], rows)
        self.assertEqual("ok", integrity)
        self.assertFalse(Path(f"{self.backup_path}-wal").exists())
        self.assertFalse(Path(f"{self.backup_path}-shm").exists())

    def test_wal_content_change_after_plan_requires_new_confirmation(self):
        plan = self._plan()
        self._append_uncheckpointed_wal_record()

        with self.assertRaisesRegex(ReviewRunDomainError, "目标数据库自"):
            migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        self.assertFalse(self.backup_path.exists())

    def test_wal_checkpoint_keeps_confirmed_logical_snapshot_valid(self):
        self._append_uncheckpointed_wal_record()
        plan = self._plan()
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

        result = migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        self.assertEqual(plan.database_sha256, result["backup_snapshot_sha256"])

    def test_wal_failure_recovery_removes_migration_sidecars(self):
        self._append_uncheckpointed_wal_record()
        plan = self._plan()

        with patch.object(
            ReviewRunRepository,
            "register_legacy_import",
            side_effect=RuntimeError("模拟登记失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "模拟登记失败"):
                migrate_review_run_database(plan, plan.content_hash, confirmed=True)

        self.assertFalse(Path(f"{self.database_path}-wal").exists())
        self.assertFalse(Path(f"{self.database_path}-shm").exists())
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT trade_date FROM evidence_snapshots ORDER BY trade_date"
            ).fetchall()
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual([("2026-07-06",), ("2026-08-18",)], rows)
        self.assertEqual("ok", integrity)
        self.assertIn("observations", names)
        self.assertNotIn("review_runs", names)

    def _plan(self):
        return plan_review_run_migration(
            self.database_path,
            1,
            self.backup_path,
            evidence_dir=self.evidence_dir,
            daily_report_dir=self.daily_dir,
            weekly_report_dir=self.weekly_dir,
            now=datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc),
        )

    def _create_legacy_database(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.executescript(
                """
                CREATE TABLE evidence_snapshots (
                    trade_date TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    sample_date TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    missing_fields_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO evidence_snapshots VALUES (
                    '2026-07-06', 'sample', '2026-07-06', '{}', '[]',
                    '2026-07-06T16:00:00+08:00'
                );
                CREATE TABLE observations (
                    observation_id TEXT PRIMARY KEY,
                    review_date TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    related_target TEXT NOT NULL,
                    hypothesis TEXT NOT NULL,
                    confirmation_condition TEXT NOT NULL,
                    invalidation_condition TEXT NOT NULL,
                    evidence_source TEXT NOT NULL,
                    plan_item TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO observations VALUES (
                    'OBS-old', '2026-08-17', '旧观察', '', '假设', '成立', '失效',
                    '旧报告', '', 'pending', '2026-08-17T16:00:00+08:00',
                    '2026-08-17T16:00:00+08:00'
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

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

    # 子进程模拟进程异常退出，使已提交记录只保留在 WAL sidecar 中且没有活动连接。
    def _append_uncheckpointed_wal_record(self) -> None:
        script = """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA journal_mode = WAL")
connection.execute("PRAGMA wal_autocheckpoint = 0")
connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
connection.execute(
    "INSERT INTO evidence_snapshots VALUES (?, ?, ?, ?, ?, ?)",
    (
        "2026-08-18",
        "wal-fixture",
        "2026-08-18",
        "{}",
        "[]",
        "2026-08-18T16:00:00+08:00",
    ),
)
connection.commit()
os._exit(0)
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.database_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
