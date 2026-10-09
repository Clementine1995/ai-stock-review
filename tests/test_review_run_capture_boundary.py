# 本文件验证新版只读清单与外部证据信封不会越过日期、来源、范围和不可变文件边界。

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from uuid import UUID

from stock_review.evidence.build_evidence_bundle import (
    build_evidence_capture,
    read_capture_payload,
    seal_evidence_bundle,
    write_capture_payload,
)
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.plan_daily_review import plan_daily_review
from stock_review.review_runs.run_manifest import RunManifest


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> UUID:
        self.value += 1
        return UUID(int=self.value)


class ReviewRunCaptureBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
        self.manifest = plan_daily_review(
            "2026-09-04",
            ("manual:snapshot",),
            ("tongdaxin:index-history?missing:market.index_history",),
            output_dir=Path("reports") / "daily",
            evidence_dir=Path("data") / "evidence",
            now=self.now,
            manifest_lifetime=timedelta(minutes=20),
            id_factory=SequentialIds(),
        )

    # daily plan 只能生成可核验清单，不应触碰清单声明的输出位置。
    def test_plan_expands_capture_slots_without_creating_files(self):
        self.assertEqual(2, len(self.manifest.captures))
        self.assertEqual("manual", self.manifest.captures[0].source)
        self.assertFalse(self.manifest.captures[0].is_conditional)
        self.assertTrue(self.manifest.captures[1].is_conditional)
        self.assertEqual(("2026-09-04",), self.manifest.captures[0].allowed_dates)
        self.manifest.validate_hash()
        for effect in self.manifest.file_effects:
            self.assertFalse(Path(effect).exists())

    def test_structured_profile_binds_limited_objects_and_record_caps(self):
        manifest = plan_daily_review(
            "2026-09-04",
            capture_profile={
                "captures": [
                    {
                        "source": "workbuddy",
                        "scope": "sector-history",
                        "allowed_objects": ["banking"],
                        "max_records": 1,
                    },
                    {
                        "source": "workbuddy",
                        "scope": "stocks",
                        "allowed_objects": ["000001", "000002"],
                    },
                ]
            },
            now=self.now,
            manifest_lifetime=timedelta(minutes=20),
            id_factory=SequentialIds(),
        )

        self.assertEqual(("banking",), manifest.captures[0].allowed_objects)
        self.assertEqual(("sector_history",), manifest.captures[0].allowed_fields)
        self.assertEqual(1, manifest.captures[0].max_records)
        self.assertEqual(("000001", "000002"), manifest.captures[1].allowed_objects)
        self.assertEqual(2, manifest.captures[1].max_records)
        self.assertEqual(manifest, RunManifest.from_dict(manifest.to_dict()))

    def test_bounded_workbuddy_scopes_reject_unbounded_or_loose_profiles(self):
        invalid_inputs = (
            (("workbuddy:stocks",), None),
            ((), {"captures": [{"source": "workbuddy", "scope": "sector-history"}]}),
            ((), {"captures": [{"source": "workbuddy", "scope": "stocks",
                                  "allowed_objects": ["000001"], "max_records": True}]}),
            ((), {"captures": [{"source": "workbuddy", "scope": "stocks",
                                  "allowed_objects": ["000001"], "slot_id": "caller-owned"}]}),
        )

        for capture_specs, profile in invalid_inputs:
            with self.subTest(capture_specs=capture_specs, profile=profile):
                with self.assertRaises(ReviewRunDomainError):
                    plan_daily_review(
                        "2026-09-04",
                        capture_specs,
                        capture_profile=profile,
                        now=self.now,
                        manifest_lifetime=timedelta(minutes=20),
                        id_factory=SequentialIds(),
                    )

    def test_available_capture_round_trips_through_immutable_file(self):
        slot = self.manifest.captures[0]
        envelope = self.envelope(slot, {"market": {"indices": []}, "sentiment": {}, "sectors": [], "stocks": []})
        capture, payload = build_evidence_capture(
            self.manifest,
            slot.slot_id,
            slot.operation_id,
            envelope,
            captured_at=self.now,
        )
        with TemporaryDirectory() as temp_path:
            output_path = Path(temp_path) / "capture.json"
            write_capture_payload(output_path, payload)
            loaded = read_capture_payload(output_path, capture)
            self.assertEqual(payload, loaded)
            write_capture_payload(output_path, payload)

            changed = dict(payload)
            changed["failure_reason"] = "篡改"
            with self.assertRaisesRegex(ReviewRunDomainError, "不可变采集证据"):
                write_capture_payload(output_path, changed)

    def test_concurrent_distinct_capture_payloads_cannot_overwrite_winner(self):
        slot = self.manifest.captures[0]
        candidates = []
        for close_value in (3800, 3900):
            _capture, payload = build_evidence_capture(
                self.manifest,
                slot.slot_id,
                slot.operation_id,
                self.envelope(
                    slot,
                    {"market": {"indices": [{"name": "上证指数", "close": close_value}]}},
                ),
                captured_at=self.now,
            )
            candidates.append(payload)

        with TemporaryDirectory() as temp_path:
            output_path = Path(temp_path) / "capture.json"
            barrier = Barrier(2)

            def publish(position):
                barrier.wait()
                try:
                    write_capture_payload(output_path, candidates[position])
                except ReviewRunDomainError:
                    return position, False
                return position, True

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(publish, (0, 1)))

            winners = [position for position, saved in results if saved]
            self.assertEqual(1, len(winners))
            self.assertEqual(
                candidates[winners[0]],
                json.loads(output_path.read_text(encoding="utf-8")),
            )

    def test_cross_date_or_extra_field_is_rejected(self):
        slot = self.manifest.captures[0]
        wrong_date = self.envelope(slot, {"market": {}}, sample_date="2026-09-03")
        with self.assertRaisesRegex(ReviewRunDomainError, "sample_date"):
            build_evidence_capture(self.manifest, slot.slot_id, slot.operation_id, wrong_date)

        extra_field = self.envelope(slot, {"market": {}, "unauthorized": []})
        with self.assertRaisesRegex(ReviewRunDomainError, "超出 RunManifest"):
            build_evidence_capture(self.manifest, slot.slot_id, slot.operation_id, extra_field)

    def test_dot_path_field_allowlist_rejects_extra_record_fields(self):
        manifest = plan_daily_review(
            "2026-09-04",
            ("tongdaxin:stock-identity",),
            now=self.now,
            manifest_lifetime=timedelta(minutes=20),
            id_factory=SequentialIds(),
        )
        slot = manifest.captures[0]
        envelope = {
            "run_id": manifest.run_id,
            "trade_date": manifest.trade_date,
            "sample_date": manifest.trade_date,
            "source": slot.source,
            "scope": slot.scope,
            "operation_id": slot.operation_id,
            "status": "available",
            "normalized": {
                "stocks": [
                    {
                        "code": "000001",
                        "name": "平安银行",
                        "exchange": "SZSE",
                        "unapproved_price": 12.3,
                    }
                ]
            },
            "raw": {},
            "missing_fields": [],
        }

        with self.assertRaisesRegex(ReviewRunDomainError, r"stocks\[0\].unapproved_price"):
            build_evidence_capture(manifest, slot.slot_id, slot.operation_id, envelope)

    def test_allowed_objects_applies_to_scope_collection_in_external_envelope(self):
        manifest = plan_daily_review(
            "2026-09-04",
            capture_profile={
                "captures": [
                    {
                        "source": "workbuddy",
                        "scope": "stocks",
                        "allowed_objects": ["000001"],
                    }
                ]
            },
            now=self.now,
            manifest_lifetime=timedelta(minutes=20),
            id_factory=SequentialIds(),
        )
        slot = manifest.captures[0]
        envelope = {
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
                "stocks": [{"code": "000002", "name": "越界对象"}],
            },
            "raw": {},
            "missing_fields": [],
        }

        with self.assertRaisesRegex(ReviewRunDomainError, "采集对象超出"):
            build_evidence_capture(manifest, slot.slot_id, slot.operation_id, envelope)

    def test_bounded_sector_history_accepts_normalized_scope_and_sector_id(self):
        manifest = plan_daily_review(
            "2026-09-04",
            capture_profile={
                "captures": [
                    {
                        "source": "workbuddy",
                        "scope": "sector-history",
                        "allowed_objects": ["banking"],
                    }
                ]
            },
            now=self.now,
            manifest_lifetime=timedelta(minutes=20),
            id_factory=SequentialIds(),
        )
        slot = manifest.captures[0]
        envelope = {
            "run_id": manifest.run_id,
            "trade_date": manifest.trade_date,
            "sample_date": manifest.trade_date,
            "source": slot.source,
            "scope": slot.scope,
            "operation_id": slot.operation_id,
            "status": "available",
            "normalized": {
                "sector_history": [
                    {
                        "sector_id": "banking",
                        "sample_dates": ["2026-09-03", "2026-09-04"],
                    }
                ]
            },
            "raw": {},
            "missing_fields": [],
        }

        capture, _payload = build_evidence_capture(
            manifest,
            slot.slot_id,
            slot.operation_id,
            envelope,
            captured_at=self.now,
        )

        self.assertEqual("available", capture.status)

    def test_bundle_requires_every_capture_slot_terminal_state(self):
        slot = self.manifest.captures[0]
        capture, _ = build_evidence_capture(
            self.manifest,
            slot.slot_id,
            slot.operation_id,
            self.envelope(slot, {"market": {}, "sentiment": {}, "sectors": [], "stocks": []}),
            captured_at=self.now,
        )
        with self.assertRaisesRegex(ReviewRunDomainError, "每个 capture slot"):
            seal_evidence_bundle(
                self.manifest,
                (capture,),
                evidence_bundle_id="EVIDENCE-one",
                sealed_at=self.now,
            )

    def envelope(self, slot, normalized, *, sample_date="2026-09-04"):
        return {
            "run_id": self.manifest.run_id,
            "trade_date": self.manifest.trade_date,
            "sample_date": sample_date,
            "source": slot.source,
            "scope": slot.scope,
            "operation_id": slot.operation_id,
            "status": "available",
            "normalized": normalized,
            "raw": {"Rows": []},
            "missing_fields": [],
        }


if __name__ == "__main__":
    unittest.main()
