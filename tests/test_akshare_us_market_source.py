from __future__ import annotations

from datetime import date
import unittest

from stock_review.evidence.akshare_us_market_source import (
    AKSHARE_US_MARKET_SOURCE,
    collect_akshare_us_market_window_evidence,
)
from stock_review.evidence.us_market_session_source import (
    US_SESSION_AVAILABLE,
    US_SESSION_PENDING,
    UsMarketSessionSourceError,
)


class FakeFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        if orient != "records":
            raise AssertionError("测试帧只支持 records。")
        return self.rows


class FakeAkshareUsMarketClient:
    def __init__(self, histories: dict[str, list[dict[str, object]]] | None = None) -> None:
        self.histories = histories or {
            ".IXIC": [
                {"date": date(2026, 8, 13), "close": 100},
                {"date": date(2026, 8, 14), "close": 102},
            ],
            ".INX": [
                {"date": date(2026, 8, 13), "close": 200},
                {"date": date(2026, 8, 14), "close": 198},
            ],
        }
        self.calls: list[str] = []

    def index_us_stock_sina(self, *, symbol: str) -> FakeFrame:
        self.calls.append(symbol)
        return FakeFrame(self.histories[symbol])


class AkshareUsMarketAdapterTests(unittest.TestCase):
    def test_adapter_builds_completed_session_from_nasdaq_and_sp500(self) -> None:
        client = FakeAkshareUsMarketClient()

        result = collect_akshare_us_market_window_evidence(
            "2026-08-14",
            "2026-08-15T05:00:00+08:00",
            "2026-08-15T04:30:00+08:00",
            "2026-08-14T15:00:00+08:00",
            "2026-08-17T09:30:00+08:00",
            client,
        )

        self.assertEqual(client.calls, [".IXIC", ".INX"])
        self.assertEqual(result["source"], AKSHARE_US_MARKET_SOURCE)
        self.assertEqual(result["status"], US_SESSION_AVAILABLE)
        self.assertEqual(result["us_session_date"], "2026-08-14")
        self.assertEqual(result["session_close_at"], "2026-08-15T04:00:00+08:00")
        self.assertEqual(
            result["indices"],
            [
                {
                    "symbol": ".IXIC",
                    "name": "纳斯达克综合指数",
                    "close": 102,
                    "change_percent": 2,
                },
                {
                    "symbol": ".INX",
                    "name": "标普500指数",
                    "close": 198,
                    "change_percent": -1,
                },
            ],
        )

    def test_adapter_marks_session_pending_before_us_regular_close(self) -> None:
        result = collect_akshare_us_market_window_evidence(
            "2026-08-14",
            "2026-08-15T04:30:00+08:00",
            "2026-08-15T03:59:59+08:00",
            "2026-08-14T15:00:00+08:00",
            "2026-08-17T09:30:00+08:00",
            FakeAkshareUsMarketClient(),
        )

        self.assertEqual(result["status"], US_SESSION_PENDING)
        self.assertIsNone(result["us_session_date"])

    def test_adapter_requires_both_indices_with_valid_daily_change(self) -> None:
        histories = {
            ".IXIC": [
                {"date": date(2026, 8, 13), "close": 100},
                {"date": date(2026, 8, 14), "close": 102},
            ],
            ".INX": [{"date": date(2026, 8, 14), "close": 198}],
        }

        result = collect_akshare_us_market_window_evidence(
            "2026-08-14",
            "2026-08-15T05:00:00+08:00",
            "2026-08-15T04:30:00+08:00",
            "2026-08-14T15:00:00+08:00",
            "2026-08-17T09:30:00+08:00",
            FakeAkshareUsMarketClient(histories),
        )

        self.assertEqual(result["status"], US_SESSION_PENDING)
        self.assertEqual(result["indices"], [])

    def test_adapter_uses_winter_eastern_close_instead_of_fixed_offset(self) -> None:
        histories = {
            ".IXIC": [
                {"date": date(2026, 1, 14), "close": 100},
                {"date": date(2026, 1, 15), "close": 101},
            ],
            ".INX": [
                {"date": date(2026, 1, 14), "close": 200},
                {"date": date(2026, 1, 15), "close": 202},
            ],
        }

        result = collect_akshare_us_market_window_evidence(
            "2026-01-15",
            "2026-01-16T06:00:00+08:00",
            "2026-01-16T05:30:00+08:00",
            "2026-01-15T15:00:00+08:00",
            "2026-01-16T09:30:00+08:00",
            FakeAkshareUsMarketClient(histories),
        )

        self.assertEqual(result["status"], US_SESSION_AVAILABLE)
        self.assertEqual(result["session_close_at"], "2026-01-16T05:00:00+08:00")

    def test_adapter_rejects_a_share_close_from_another_review_date_before_requests(self) -> None:
        client = FakeAkshareUsMarketClient()

        with self.assertRaisesRegex(UsMarketSessionSourceError, "中国日期必须等于复盘日期"):
            collect_akshare_us_market_window_evidence(
                "2026-08-14",
                "2026-08-15T05:00:00+08:00",
                "2026-08-15T04:30:00+08:00",
                "2026-08-13T15:00:00+08:00",
                "2026-08-17T09:30:00+08:00",
                client,
            )

        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
