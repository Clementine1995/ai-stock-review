from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_review.pools.classify_pool_freshness import classify_active_real_pool_items
from stock_review.pools.manage_pool_item import PoolItem


class PoolFreshnessTest(unittest.TestCase):
    def test_active_real_item_becomes_stale_after_five_local_snapshot_days(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot_files(root, ("2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"))

            summary = classify_active_real_pool_items(
                [self.build_item(start_date="2026-08-03", updated_at="2026-08-03T18:00:00")],
                "2026-08-10",
                root,
            )

            self.assertEqual(summary.fresh_items, ())
            self.assertEqual(summary.stale_items[0].unconfirmed_trading_days, 5)
            self.assertEqual(summary.stale_items[0].last_confirmed_date, "2026-08-03")

    def test_recent_renewal_keeps_item_fresh(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot_files(root, ("2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"))
            renewed_item = replace(
                self.build_item(start_date="2026-08-03", updated_at="2026-08-03T18:00:00"),
                updated_at="2026-08-07T18:00:00",
            )

            summary = classify_active_real_pool_items([renewed_item], "2026-08-10", root)

            self.assertEqual([item.code for item in summary.fresh_items], ["002829"])
            self.assertEqual(summary.stale_items, ())

    def test_non_active_or_sample_items_do_not_enter_either_group(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            paused_item = replace(self.build_item(), status="paused")
            sample_item = replace(self.build_item(), code="600519", record_kind="sample")

            summary = classify_active_real_pool_items([paused_item, sample_item], "2026-08-10", root)

            self.assertEqual(summary.fresh_items, ())
            self.assertEqual(summary.stale_items, ())

    @staticmethod
    def build_item(
        start_date: str = "2026-08-03",
        updated_at: str = "2026-08-03T18:00:00",
    ) -> PoolItem:
        return PoolItem(
            pool_type="hot",
            code="002829",
            name="星网宇达",
            exchange="SZSE",
            sectors=("军工",),
            reason="人工确认",
            start_date=start_date,
            status="active",
            note="",
            created_at=f"{start_date}T18:00:00",
            updated_at=updated_at,
            record_kind="real",
        )

    @staticmethod
    def write_snapshot_files(root: Path, trade_dates: tuple[str, ...]) -> None:
        for trade_date in trade_dates:
            (root / f"{trade_date}_snapshot.json").write_text("{}", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
