# 本文件验证封存证据可生成保守的联合 RevisionBundle，并受期望当前修订保护。

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from stock_review.review_runs.canonical_hash import canonical_hash
from stock_review.review_runs.complete_daily_review import complete_local_review
from stock_review.review_runs.domain_error import ReviewRunDomainError
from stock_review.review_runs.plan_daily_review import plan_daily_review
from stock_review.review_runs.review_run_event import ReviewRunEvent
from stock_review.review_runs.run_daily_review import seal_daily_evidence, start_daily_review
from stock_review.storage.review_run_repository import (
    OptimisticConcurrencyError,
    ReviewRunRepository,
)


class SequentialIds:
    def __init__(self, start: int = 0) -> None:
        self.value = start

    def __call__(self) -> UUID:
        self.value += 1
        return UUID(int=self.value)


class CompleteDailyReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.now = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
        self.repository = ReviewRunRepository(self.root / "review.sqlite")
        self.manifest = plan_daily_review(
            "2026-09-04",
            ("fake:market",),
            evidence_dir=self.root / "evidence",
            output_dir=self.root / "reports",
            database_path=self.root / "review.sqlite",
            now=self.now,
            manifest_lifetime=timedelta(minutes=30),
            id_factory=SequentialIds(),
        )
        start_daily_review(
            self.manifest,
            self.manifest.content_hash,
            self.repository,
            collectors={"fake": self.fake_collector},
            now=self.now,
            event_id_factory=SequentialIds(100),
        )
        seal_daily_evidence(
            self.manifest.run_id,
            self.manifest.trade_date,
            self.manifest.content_hash,
            self.repository,
            now=self.now,
            event_id_factory=SequentialIds(200),
        )

    def test_local_completion_adds_one_conservative_joint_revision(self):
        revision = complete_local_review(
            self.manifest.run_id,
            self.manifest.trade_date,
            None,
            self.manifest.content_hash,
            "OP-local-draft",
            self.repository,
            framework_path=Path("stock-review.md"),
            now=self.now,
        )
        self.assertEqual("draft", revision.revision_kind)
        self.assertEqual("undecided", revision.participation_decision)
        self.assertEqual(tuple(range(1, 11)), tuple(step.step_number for step in revision.steps))
        self.assertTrue(all(step.status == "incomplete" for step in revision.steps))
        step_nine = revision.steps[8]
        self.assertEqual(
            tuple(range(1, 13)),
            tuple(item.pattern_number for item in step_nine.pattern_assessments),
        )
        self.assertFalse(revision.candidate_plan_items)
        self.assertEqual("draft_ready", self.current_run_state())

    def test_copied_database_cannot_receive_local_revision(self):
        clone_path = self.root / "clone.sqlite"
        shutil.copy2(self.repository.database_path, clone_path)
        clone_repository = ReviewRunRepository(clone_path)

        with self.assertRaisesRegex(ReviewRunDomainError, "实际 SQLite 路径"):
            complete_local_review(
                self.manifest.run_id,
                self.manifest.trade_date,
                None,
                self.manifest.content_hash,
                "OP-clone-local",
                clone_repository,
                framework_path=Path("stock-review.md"),
                now=self.now,
            )
        revision_id = f"REV-{canonical_hash({'run_id': self.manifest.run_id, 'operation_id': 'OP-clone-local'})[:24]}"
        self.assertIsNone(clone_repository.get_review_revision(revision_id))

    def test_expired_manifest_blocks_local_revision_write(self):
        operation_id = "OP-expired-manifest-local"

        with self.assertRaisesRegex(ReviewRunDomainError, "RunManifest 已过期"):
            complete_local_review(
                self.manifest.run_id,
                self.manifest.trade_date,
                None,
                self.manifest.content_hash,
                operation_id,
                self.repository,
                framework_path=Path("stock-review.md"),
                now=self.now + timedelta(minutes=30),
            )

        revision_id = (
            "REV-"
            + canonical_hash(
                {"run_id": self.manifest.run_id, "operation_id": operation_id}
            )[:24]
        )
        self.assertIsNone(self.repository.get_review_revision(revision_id))
        self.assertEqual("evidence_ready", self.current_run_state())

    def test_available_index_reference_does_not_hide_turnover_or_sector_gaps(self):
        revision = self.complete("OP-capability-gaps", None)
        self.assertTrue(revision.steps[0].evidence_references)
        self.assertIn("缺少实际可用的两市量能", revision.steps[0].evidence_gaps)
        self.assertNotIn("缺少实际可用的指数事实", revision.steps[0].evidence_gaps)
        self.assertIn("缺少实际可用的板块连续比较", revision.steps[4].evidence_gaps)
        self.assertTrue(revision.steps[8].evidence_gaps)

    def test_non_participation_cannot_publish_unreviewed_local_template(self):
        from stock_review.review_runs.finalize_daily_review import propose_daily_actions

        revision = self.complete("OP-template", None)
        with self.assertRaisesRegex(ReviewRunDomainError, "通用模板"):
            propose_daily_actions(
                self.manifest.run_id, revision.review_revision_id,
                {"participation_decision": "non_participation",
                 "decision_note": "缺少量能，次日暂不参与。",
                 "approved_plan_item_ids": [], "rejected_plan_items": {}},
                self.repository, output_dir=self.root / "reports", now=self.now,
            )

    def test_step_minimum_groups_keep_real_gaps_and_accept_alternative_stock_evidence(self):
        from stock_review.review_runs.validate_model_response import step_evidence_gaps

        for number, label in ((1, "指数事实"), (1, "两市量能"),
                              (3, "涨跌停、炸板率和连板高度"), (4, "板块事实"),
                              (6, "个股身份、板块和角色来源")):
            with self.subTest(step=number, capability=label):
                self.assertIn("缺少实际可用的" + label, step_evidence_gaps(number, set()))
        self.assertEqual(
            (),
            step_evidence_gaps(2, {"sentiment_core", "stock_style_sample"}),
        )
        self.assertIn(
            "至少两个可比较个股的风格样本",
            "；".join(step_evidence_gaps(2, {"sentiment_core", "stocks"})),
        )

    def test_placeholder_and_identity_only_values_do_not_form_evidence_capabilities(self):
        from types import SimpleNamespace

        from stock_review.review_runs.validate_model_response import (
            capture_evidence_capabilities,
        )

        capabilities = capture_evidence_capabilities(
            SimpleNamespace(scope="snapshot"),
            {
                "normalized": {
                    "market": {
                        "indices": [{"name": "上证指数", "close": float("nan")}],
                        "total_amount": False,
                    },
                    "sentiment": {
                        "limit_up_count": False,
                        "limit_down_count": float("inf"),
                        "broken_board_rate": "待确认",
                        "highest_board": "",
                    },
                    "sectors": [{"name": "银行"}],
                    "stocks": [{"code": "000001", "name": "平安银行"}],
                    "call_auction": "待确认",
                }
            },
        )

        self.assertFalse(
            capabilities
            & {
                "market",
                "market_indices",
                "market_turnover",
                "sentiment",
                "sentiment_core",
                "sectors",
                "stocks",
                "call_auction",
            }
        )

    def test_style_sample_needs_multiple_stocks_and_daily_kline_needs_history(self):
        from types import SimpleNamespace

        from stock_review.review_runs.validate_model_response import (
            capture_evidence_capabilities,
        )

        one_stock = {
            "code": "000001",
            "name": "平安银行",
            "close": 12.5,
            "daily_bars": [{"date": "2026-09-04", "close": 12.5}],
        }
        one_stock_capabilities = capture_evidence_capabilities(
            SimpleNamespace(scope="stocks"),
            {"normalized": {"stocks": [one_stock]}},
        )

        self.assertIn("stocks", one_stock_capabilities)
        self.assertNotIn("stock_style_sample", one_stock_capabilities)
        self.assertNotIn("daily_kline", one_stock_capabilities)

        second_stock = {
            "code": "000002",
            "name": "万科A",
            "close": 8.5,
            "daily_bars": [
                {"date": "2026-09-03", "close": 8.3},
                {"date": "2026-09-04", "close": 8.5},
            ],
        }
        comparable_capabilities = capture_evidence_capabilities(
            SimpleNamespace(scope="stocks"),
            {"normalized": {"stocks": [one_stock, second_stock]}},
        )

        self.assertIn("stock_style_sample", comparable_capabilities)
        self.assertIn("daily_kline", comparable_capabilities)

    def test_same_operation_replays_without_new_revision_event(self):
        first = self.complete("OP-repeat", None)
        second = self.complete("OP-repeat", None)
        self.assertEqual(first, second)
        revision_events = [
            event
            for event in self.repository.list_review_run_events(self.manifest.run_id)
            if event["event_type"] == "revision_added"
        ]
        self.assertEqual(1, len(revision_events))

    def test_stale_expected_revision_is_rejected(self):
        first = self.complete("OP-first", None)
        with self.assertRaisesRegex(ReviewRunDomainError, "期望当前修订"):
            self.complete("OP-stale", None)
        second = self.complete("OP-second", first.review_revision_id)
        self.assertEqual(first.review_revision_id, second.replaces_review_revision_id)

    def test_event_head_change_after_preview_rejects_local_revision_event(self):
        original_append = self.repository.append_review_run_event
        inserted = False

        def append_after_competing_event(*args, **kwargs):
            nonlocal inserted
            if not inserted and args[2] == "revision_added":
                inserted = True
                records = self.repository.list_review_run_events(self.manifest.run_id)
                competing = ReviewRunEvent.create(
                    event_id="EVENT-competing-local-revision",
                    run_id=self.manifest.run_id,
                    event_type="revision_added",
                    occurred_at=(self.now + timedelta(seconds=1)).isoformat(),
                    operation_id="OP-competing-local-revision",
                    review_revision_id="REV-competing-local-revision",
                    expected_current_revision_id=None,
                )
                original_append(
                    competing.run_id,
                    competing.event_id,
                    competing.event_type,
                    competing.to_dict(),
                    records[-1]["event_id"],
                )
            return original_append(*args, **kwargs)

        with patch.object(
            self.repository,
            "append_review_run_event",
            side_effect=append_after_competing_event,
        ):
            with self.assertRaisesRegex(OptimisticConcurrencyError, "版本冲突"):
                self.complete("OP-lost-event-race", None)

        revision_events = [
            ReviewRunEvent.from_dict(record["payload"])
            for record in self.repository.list_review_run_events(self.manifest.run_id)
            if record["event_type"] == "revision_added"
        ]
        self.assertEqual(
            ["REV-competing-local-revision"],
            [event.review_revision_id for event in revision_events],
        )

    def complete(self, operation_id, expected_revision_id):
        return complete_local_review(
            self.manifest.run_id,
            self.manifest.trade_date,
            expected_revision_id,
            self.manifest.content_hash,
            operation_id,
            self.repository,
            framework_path=Path("stock-review.md"),
            now=self.now,
        )

    def current_run_state(self):
        from stock_review.review_runs.run_daily_review import load_run_projection

        return load_run_projection(self.repository, self.manifest.run_id).state

    @staticmethod
    def fake_collector(trade_date, _slot):
        return {
            "source": "fake",
            "sample_date": trade_date,
            "market": {"indices": [{"name": "上证指数", "close": 3800}]},
        }


if __name__ == "__main__":
    unittest.main()
