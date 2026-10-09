# 本文件使用临时 SQLite 和假来源验证 ReviewRun 采集、降级、封存与不可变边界。

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.plan_daily_review import plan_daily_review
from stock_review.review_runs.review_run_event import ReviewRunEvent
from stock_review.review_runs.run_daily_review import (
    attach_daily_evidence,
    load_run_projection,
    seal_daily_evidence,
    start_daily_review,
)
from stock_review.storage.review_run_repository import ReviewRunRepository


class SequentialIds:
    def __init__(self, start: int = 0) -> None:
        self.value = start

    def __call__(self) -> UUID:
        self.value += 1
        return UUID(int=self.value)


class ReviewRunCollectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "review.sqlite"
        self.repository = ReviewRunRepository(self.database_path)
        self.now = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)

    def test_direct_and_external_captures_seal_one_bundle(self):
        manifest = self.plan(("fake:market", "manual:snapshot"))
        run = start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            collectors={"fake": self.fake_market_collector},
            now=self.now,
            event_id_factory=SequentialIds(100),
        )
        self.assertEqual("available", run.capture_slots[0].status)
        self.assertEqual("pending", run.capture_slots[1].status)

        external = manifest.captures[1]
        run = attach_daily_evidence(
            manifest.run_id,
            manifest.trade_date,
            manifest.content_hash,
            external.operation_id,
            self.external_envelope(manifest, external),
            self.repository,
            now=self.now,
            event_id_factory=SequentialIds(200),
        )
        self.assertEqual("available", run.capture_slots[1].status)

        run, bundle = seal_daily_evidence(
            manifest.run_id,
            manifest.trade_date,
            manifest.content_hash,
            self.repository,
            now=self.now,
            event_id_factory=SequentialIds(300),
        )
        self.assertEqual("evidence_ready", run.state)
        self.assertEqual(2, len(bundle.captures))
        bundle.validate_against_manifest(manifest)
        with self.assertRaisesRegex(ReviewRunDomainError, "封存后"):
            attach_daily_evidence(
                manifest.run_id,
                manifest.trade_date,
                manifest.content_hash,
                external.operation_id,
                self.external_envelope(manifest, external),
                self.repository,
                now=self.now,
            )

    def test_expired_manifest_blocks_new_run_before_schema_creation(self):
        manifest = self.plan(("manual:snapshot",))

        with self.assertRaisesRegex(ReviewRunDomainError, "RunManifest 已过期"):
            start_daily_review(
                manifest,
                manifest.content_hash,
                self.repository,
                now=self.now + timedelta(minutes=30),
                event_id_factory=SequentialIds(310),
            )

        self.assertFalse(self.database_path.exists())

    def test_expired_manifest_blocks_external_capture_write(self):
        manifest = self.plan(("manual:snapshot",))
        start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            now=self.now,
            event_id_factory=SequentialIds(320),
        )
        slot = manifest.captures[0]

        with self.assertRaisesRegex(ReviewRunDomainError, "RunManifest 已过期"):
            attach_daily_evidence(
                manifest.run_id,
                manifest.trade_date,
                manifest.content_hash,
                slot.operation_id,
                self.external_envelope(manifest, slot),
                self.repository,
                now=self.now + timedelta(minutes=30),
                event_id_factory=SequentialIds(330),
            )

        self.assertIsNone(
            self.repository.get_evidence_capture(f"CAPTURE-{slot.operation_id}")
        )
        self.assertEqual(
            "pending",
            load_run_projection(self.repository, manifest.run_id).capture_slots[0].status,
        )

    def test_expired_manifest_blocks_evidence_bundle_write(self):
        manifest = self.plan(("fake:market",))
        start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            collectors={"fake": self.fake_market_collector},
            now=self.now,
            event_id_factory=SequentialIds(340),
        )

        with self.assertRaisesRegex(ReviewRunDomainError, "RunManifest 已过期"):
            seal_daily_evidence(
                manifest.run_id,
                manifest.trade_date,
                manifest.content_hash,
                self.repository,
                now=self.now + timedelta(minutes=30),
                event_id_factory=SequentialIds(350),
            )

        projection = load_run_projection(self.repository, manifest.run_id)
        self.assertEqual("collecting", projection.state)
        self.assertFalse(projection.evidence_bundle_id)

    def test_copied_database_cannot_receive_manifest_authorized_writes(self):
        manifest = self.plan(("manual:snapshot",))
        start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            now=self.now,
            event_id_factory=SequentialIds(350),
        )
        clone_path = self.root / "clone.sqlite"
        shutil.copy2(self.database_path, clone_path)
        clone_repository = ReviewRunRepository(clone_path)
        slot = manifest.captures[0]

        with self.assertRaisesRegex(ReviewRunDomainError, "实际 SQLite 路径"):
            attach_daily_evidence(
                manifest.run_id,
                manifest.trade_date,
                manifest.content_hash,
                slot.operation_id,
                self.external_envelope(manifest, slot),
                clone_repository,
                now=self.now,
            )
        with self.assertRaisesRegex(ReviewRunDomainError, "实际 SQLite 路径"):
            seal_daily_evidence(
                manifest.run_id,
                manifest.trade_date,
                manifest.content_hash,
                clone_repository,
                now=self.now,
            )
        self.assertEqual([], clone_repository.list_evidence_captures(manifest.run_id))

    def test_start_rejects_manifest_with_incomplete_database_effects(self):
        manifest = self.plan(("manual:snapshot",))
        incomplete = replace(
            manifest,
            database_effects=manifest.database_effects[:-1],
            content_hash="",
        )
        incomplete = replace(incomplete, content_hash=incomplete.calculate_hash())

        with self.assertRaisesRegex(ReviewRunDomainError, "未完整覆盖"):
            start_daily_review(
                incomplete,
                incomplete.content_hash,
                self.repository,
                now=self.now,
            )
        self.assertFalse(self.database_path.exists())

    def test_source_failure_is_unavailable_and_does_not_abort_seal(self):
        manifest = self.plan(("fake:market",))

        def failed_collector(_trade_date, _slot):
            raise TimeoutError("来源超时")

        run = start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            collectors={"fake": failed_collector},
            now=self.now,
            event_id_factory=SequentialIds(400),
        )
        self.assertEqual("unavailable", run.capture_slots[0].status)
        sealed, bundle = seal_daily_evidence(
            manifest.run_id,
            manifest.trade_date,
            manifest.content_hash,
            self.repository,
            now=self.now,
            event_id_factory=SequentialIds(500),
        )
        self.assertEqual("evidence_ready", sealed.state)
        self.assertIn("TimeoutError", bundle.captures[0].failure_reason)

    def test_replaying_start_does_not_duplicate_run_or_capture(self):
        manifest = self.plan(("fake:market",))
        ids = SequentialIds(600)
        first = start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            collectors={"fake": self.fake_market_collector},
            now=self.now,
            event_id_factory=ids,
        )
        second = start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            collectors={"fake": self.fake_market_collector},
            now=self.now,
            event_id_factory=ids,
        )
        self.assertEqual(first, second)
        self.assertEqual(2, len(self.repository.list_review_run_events(manifest.run_id)))
        self.assertEqual(1, len(self.repository.list_evidence_captures(manifest.run_id)))

    def test_start_previews_competing_started_event_before_append(self):
        manifest = self.plan(("fake:market",))
        original_list = self.repository.list_review_run_events
        original_append = self.repository.append_review_run_event
        list_calls = 0

        def list_with_competing_start(run_id):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 1:
                return original_list(run_id)
            if list_calls == 2:
                competing = ReviewRunEvent.create(
                    event_id="EVENT-competing-run-start",
                    run_id=manifest.run_id,
                    event_type="run_started",
                    occurred_at=(self.now + timedelta(seconds=1)).isoformat(),
                    manifest_hash=manifest.content_hash,
                )
                original_append(
                    competing.run_id,
                    competing.event_id,
                    competing.event_type,
                    competing.to_dict(),
                    None,
                )
            return original_list(run_id)

        with patch.object(
            self.repository,
            "list_review_run_events",
            side_effect=list_with_competing_start,
        ):
            with self.assertRaisesRegex(ReviewRunDomainError, "只能开始一次"):
                start_daily_review(
                    manifest,
                    manifest.content_hash,
                    self.repository,
                    collectors={"fake": self.fake_market_collector},
                    now=self.now,
                    event_id_factory=SequentialIds(650),
                )

        records = original_list(manifest.run_id)
        self.assertEqual(["EVENT-competing-run-start"], [item["event_id"] for item in records])

    def test_false_conditional_capture_is_recorded_as_skipped(self):
        manifest = plan_daily_review(
            "2026-09-04",
            ("fake:market",),
            ("fake:index-history?missing:market.indices",),
            evidence_dir=self.root / "evidence",
            output_dir=self.root / "reports",
            database_path=self.database_path,
            now=self.now,
            manifest_lifetime=timedelta(minutes=30),
            id_factory=SequentialIds(),
        )
        run = start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            collectors={"fake": self.fake_market_collector},
            now=self.now,
            event_id_factory=SequentialIds(700),
        )
        self.assertEqual(("available", "skipped"), tuple(item.status for item in run.capture_slots))

    def test_tampered_capture_file_blocks_seal(self):
        manifest = self.plan(("fake:market",))
        start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            collectors={"fake": self.fake_market_collector},
            now=self.now,
            event_id_factory=SequentialIds(800),
        )
        output_path = Path(manifest.captures[0].output_location)
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        payload["normalized"]["market"]["indices"][0]["close"] = 9999
        output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ReviewRunDomainError, "payload_hash"):
            seal_daily_evidence(
                manifest.run_id,
                manifest.trade_date,
                manifest.content_hash,
                self.repository,
                now=self.now,
                event_id_factory=SequentialIds(900),
            )

    def test_seal_recovers_one_bundle_after_event_append_failure(self):
        manifest = self.plan(("fake:market",))
        start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            collectors={"fake": self.fake_market_collector},
            now=self.now,
            event_id_factory=SequentialIds(1000),
        )
        with patch.object(
            self.repository,
            "append_review_run_event",
            side_effect=RuntimeError("模拟封存事件失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "模拟封存事件失败"):
                seal_daily_evidence(
                    manifest.run_id,
                    manifest.trade_date,
                    manifest.content_hash,
                    self.repository,
                    now=self.now,
                )

        recovered, bundle = seal_daily_evidence(
            manifest.run_id,
            manifest.trade_date,
            manifest.content_hash,
            self.repository,
            now=self.now + timedelta(minutes=1),
        )
        replayed, replayed_bundle = seal_daily_evidence(
            manifest.run_id,
            manifest.trade_date,
            manifest.content_hash,
            self.repository,
            now=self.now + timedelta(minutes=2),
        )

        self.assertEqual("evidence_ready", recovered.state)
        self.assertEqual(bundle, replayed_bundle)
        self.assertEqual(recovered, replayed)
        self.assertEqual(
            1,
            sum(
                record["event_type"] == "evidence_bundle_sealed"
                for record in self.repository.list_review_run_events(manifest.run_id)
            ),
        )

    def test_attach_recovers_frozen_capture_after_event_append_failure(self):
        manifest = self.plan(("manual:snapshot",))
        start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            now=self.now,
            event_id_factory=SequentialIds(1100),
        )
        slot = manifest.captures[0]
        envelope = self.external_envelope(manifest, slot)
        with patch.object(
            self.repository,
            "append_review_run_event",
            side_effect=RuntimeError("模拟采集事件失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "模拟采集事件失败"):
                attach_daily_evidence(
                    manifest.run_id,
                    manifest.trade_date,
                    manifest.content_hash,
                    slot.operation_id,
                    envelope,
                    self.repository,
                    now=self.now,
                    event_id_factory=SequentialIds(1200),
                )

        saved_before_retry = self.repository.get_evidence_capture(
            f"CAPTURE-{slot.operation_id}"
        )
        recovered = attach_daily_evidence(
            manifest.run_id,
            manifest.trade_date,
            manifest.content_hash,
            slot.operation_id,
            envelope,
            self.repository,
            now=self.now + timedelta(minutes=1),
            event_id_factory=SequentialIds(1300),
        )
        saved_after_retry = self.repository.get_evidence_capture(
            f"CAPTURE-{slot.operation_id}"
        )

        self.assertEqual("available", recovered.capture_slots[0].status)
        self.assertEqual(saved_before_retry, saved_after_retry)
        self.assertEqual(1, len(self.repository.list_evidence_captures(manifest.run_id)))

    def test_attach_recovers_capture_file_when_database_save_failed(self):
        manifest = self.plan(("manual:snapshot",))
        start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            now=self.now,
            event_id_factory=SequentialIds(1600),
        )
        slot = manifest.captures[0]
        envelope = self.external_envelope(manifest, slot)
        with patch.object(
            self.repository,
            "save_evidence_capture",
            side_effect=RuntimeError("模拟文件落盘后数据库写入失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "数据库写入失败"):
                attach_daily_evidence(
                    manifest.run_id,
                    manifest.trade_date,
                    manifest.content_hash,
                    slot.operation_id,
                    envelope,
                    self.repository,
                    now=self.now,
                    event_id_factory=SequentialIds(1700),
                )
        output_path = Path(slot.output_location)
        file_before_retry = output_path.read_bytes()
        payload_before_retry = json.loads(file_before_retry)

        recovered = attach_daily_evidence(
            manifest.run_id,
            manifest.trade_date,
            manifest.content_hash,
            slot.operation_id,
            envelope,
            self.repository,
            now=self.now + timedelta(minutes=1),
            event_id_factory=SequentialIds(1800),
        )
        saved = self.repository.get_evidence_capture(f"CAPTURE-{slot.operation_id}")

        self.assertEqual("available", recovered.capture_slots[0].status)
        self.assertEqual(file_before_retry, output_path.read_bytes())
        self.assertEqual(
            payload_before_retry["capture"]["captured_at"],
            saved["payload"]["capture"]["captured_at"],
        )
        self.assertEqual(
            payload_before_retry["payload_hash"],
            saved["payload"]["payload_hash"],
        )

    def test_attach_rejects_different_envelope_when_only_capture_file_exists(self):
        manifest = self.plan(("manual:snapshot",))
        start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            now=self.now,
            event_id_factory=SequentialIds(1900),
        )
        slot = manifest.captures[0]
        envelope = self.external_envelope(manifest, slot)
        with patch.object(
            self.repository,
            "save_evidence_capture",
            side_effect=RuntimeError("模拟文件落盘后数据库写入失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "数据库写入失败"):
                attach_daily_evidence(
                    manifest.run_id,
                    manifest.trade_date,
                    manifest.content_hash,
                    slot.operation_id,
                    envelope,
                    self.repository,
                    now=self.now,
                    event_id_factory=SequentialIds(2000),
                )
        changed_envelope = {
            **envelope,
            "normalized": {"stocks": [{"code": "000002", "name": "万科A"}]},
        }

        with self.assertRaisesRegex(ReviewRunDomainError, "落盘内容.*本次.*信封"):
            attach_daily_evidence(
                manifest.run_id,
                manifest.trade_date,
                manifest.content_hash,
                slot.operation_id,
                changed_envelope,
                self.repository,
                now=self.now + timedelta(minutes=1),
                event_id_factory=SequentialIds(2100),
            )
        self.assertIsNone(
            self.repository.get_evidence_capture(f"CAPTURE-{slot.operation_id}")
        )

    def test_start_recovers_saved_direct_capture_without_recollecting(self):
        manifest = self.plan(("fake:market",))
        original_append = self.repository.append_review_run_event

        def fail_capture_event(*args, **kwargs):
            if args[2] == "capture_completed":
                raise RuntimeError("模拟直接采集事件失败")
            return original_append(*args, **kwargs)

        with patch.object(
            self.repository,
            "append_review_run_event",
            side_effect=fail_capture_event,
        ):
            with self.assertRaisesRegex(RuntimeError, "模拟直接采集事件失败"):
                start_daily_review(
                    manifest,
                    manifest.content_hash,
                    self.repository,
                    collectors={"fake": self.fake_market_collector},
                    now=self.now,
                    event_id_factory=SequentialIds(1400),
                )

        def unexpected_recollect(_trade_date, _slot):
            raise AssertionError("恢复已保存 capture 时不应再次调用来源")

        recovered = start_daily_review(
            manifest,
            manifest.content_hash,
            self.repository,
            collectors={"fake": unexpected_recollect},
            now=self.now + timedelta(minutes=1),
            event_id_factory=SequentialIds(1500),
        )

        self.assertEqual("available", recovered.capture_slots[0].status)
        self.assertEqual(1, len(self.repository.list_evidence_captures(manifest.run_id)))

    def plan(self, captures):
        return plan_daily_review(
            "2026-09-04",
            captures,
            evidence_dir=self.root / "evidence",
            output_dir=self.root / "reports",
            database_path=self.database_path,
            now=self.now,
            manifest_lifetime=timedelta(minutes=30),
            id_factory=SequentialIds(),
        )

    @staticmethod
    def fake_market_collector(trade_date, _slot):
        return {
            "source": "fake",
            "sample_date": trade_date,
            "market": {"indices": [{"name": "上证指数", "close": 3800}]},
        }

    @staticmethod
    def external_envelope(manifest, slot):
        return {
            "run_id": manifest.run_id,
            "trade_date": manifest.trade_date,
            "sample_date": manifest.trade_date,
            "source": slot.source,
            "scope": slot.scope,
            "operation_id": slot.operation_id,
            "status": "available",
            "normalized": {
                "market": {},
                "sentiment": {},
                "sectors": [],
                "stocks": [],
                "events": [],
            },
            "raw": {"Rows": []},
            "missing_fields": [],
        }


if __name__ == "__main__":
    unittest.main()
