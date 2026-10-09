from __future__ import annotations

import unittest

from stock_review.evidence.us_market_session_source import (
    PENDING_SESSION_NOTE,
    US_SESSION_AVAILABLE,
    US_SESSION_PENDING,
    UsMarketSessionSourceError,
    build_us_market_window_evidence,
    collect_us_market_window_evidence,
)


REVIEW_DATE = "2026-08-14"
A_SHARE_CLOSE = "2026-08-14T15:00:00+08:00"
NEXT_A_SHARE_OPEN = "2026-08-17T09:30:00+08:00"
GENERATED_AT = "2026-08-15T17:54:57+08:00"
AS_OF = "2026-08-15T17:30:00+08:00"


def completed_session(
    session_date: str,
    close_at: str,
    *,
    change_percent: object = "1.25%",
) -> dict[str, object]:
    return {
        "us_session_date": session_date,
        "session_type": "regular",
        "status": "completed",
        "regular_close_at": close_at,
        "indices": [
            {
                "symbol": "^NDX",
                "name": "纳斯达克100指数",
                "close": "23,500.5",
                "change_percent": change_percent,
            }
        ],
    }


class FakeUsMarketClient:
    source_name = "public:fake_us_market"

    def __init__(self, sessions: list[dict[str, object]]) -> None:
        self.sessions = sessions
        self.calls: list[dict[str, str]] = []

    def fetch_us_regular_sessions(self, **kwargs: str) -> list[dict[str, object]]:
        self.calls.append(kwargs)
        return self.sessions


class UsMarketSessionWindowTests(unittest.TestCase):
    def test_completed_regular_session_inside_window_is_available(self) -> None:
        result = build_us_market_window_evidence(
            REVIEW_DATE,
            GENERATED_AT,
            AS_OF,
            A_SHARE_CLOSE,
            NEXT_A_SHARE_OPEN,
            [completed_session("2026-08-14", "2026-08-15T04:00:00+08:00")],
        )

        self.assertEqual(result["review_date"], REVIEW_DATE)
        self.assertEqual(result["generated_at"], GENERATED_AT)
        self.assertEqual(result["as_of"], AS_OF)
        self.assertEqual(result["us_session_date"], "2026-08-14")
        self.assertEqual(result["status"], US_SESSION_AVAILABLE)
        self.assertEqual(result["session_close_at"], "2026-08-15T04:00:00+08:00")
        self.assertEqual(result["window"]["boundary"], "open_interval")
        self.assertEqual(
            result["indices"],
            [
                {
                    "symbol": "^NDX",
                    "name": "纳斯达克100指数",
                    "close": 23500.5,
                    "change_percent": 1.25,
                }
            ],
        )

    def test_pending_session_does_not_fall_back_to_old_completed_session(self) -> None:
        sessions = [
            completed_session("2026-08-13", "2026-08-14T04:00:00+08:00"),
            {
                "us_session_date": "2026-08-14",
                "session_type": "regular",
                "status": "in_progress",
                "regular_close_at": "2026-08-15T04:00:00+08:00",
            },
        ]

        result = build_us_market_window_evidence(
            REVIEW_DATE,
            "2026-08-14T18:00:00+08:00",
            "2026-08-14T17:59:00+08:00",
            A_SHARE_CLOSE,
            NEXT_A_SHARE_OPEN,
            sessions,
        )

        self.assertEqual(result["status"], US_SESSION_PENDING)
        self.assertIsNone(result["us_session_date"])
        self.assertIsNone(result["session_close_at"])
        self.assertEqual(result["indices"], [])
        self.assertEqual(result["note"], PENDING_SESSION_NOTE)

    def test_completed_session_after_cutoff_remains_pending(self) -> None:
        result = build_us_market_window_evidence(
            REVIEW_DATE,
            "2026-08-15T05:00:00+08:00",
            "2026-08-15T03:59:59+08:00",
            A_SHARE_CLOSE,
            NEXT_A_SHARE_OPEN,
            [completed_session("2026-08-14", "2026-08-15T04:00:00+08:00")],
        )

        self.assertEqual(result["status"], US_SESSION_PENDING)
        self.assertIsNone(result["us_session_date"])

    def test_sessions_on_or_outside_window_boundaries_are_excluded(self) -> None:
        result = build_us_market_window_evidence(
            REVIEW_DATE,
            "2026-08-17T10:00:00+08:00",
            "2026-08-17T10:00:00+08:00",
            A_SHARE_CLOSE,
            NEXT_A_SHARE_OPEN,
            [
                completed_session("2026-08-14", A_SHARE_CLOSE),
                completed_session("2026-08-16", NEXT_A_SHARE_OPEN),
            ],
        )

        self.assertEqual(result["status"], US_SESSION_PENDING)

    def test_latest_completed_session_in_long_window_is_selected(self) -> None:
        result = build_us_market_window_evidence(
            REVIEW_DATE,
            GENERATED_AT,
            AS_OF,
            A_SHARE_CLOSE,
            NEXT_A_SHARE_OPEN,
            [
                completed_session("2026-08-13", "2026-08-14T04:00:00+08:00"),
                completed_session("2026-08-14", "2026-08-15T04:00:00+08:00", change_percent=-0.5),
                completed_session("2026-08-15", "2026-08-15T05:00:00+08:00", change_percent=0.75),
            ],
        )

        self.assertEqual(result["us_session_date"], "2026-08-15")
        self.assertEqual(result["indices"][0]["change_percent"], 0.75)

    def test_collector_passes_explicit_window_and_cutoff_to_client(self) -> None:
        client = FakeUsMarketClient(
            [completed_session("2026-08-14", "2026-08-15T04:00:00+08:00")]
        )

        result = collect_us_market_window_evidence(
            REVIEW_DATE,
            GENERATED_AT,
            AS_OF,
            A_SHARE_CLOSE,
            NEXT_A_SHARE_OPEN,
            client,
        )

        self.assertEqual(
            client.calls,
            [
                {
                    "window_start": A_SHARE_CLOSE,
                    "window_end": NEXT_A_SHARE_OPEN,
                    "as_of": AS_OF,
                }
            ],
        )
        self.assertEqual(result["source"], "public:fake_us_market")

    def test_timezone_free_inputs_are_rejected(self) -> None:
        with self.assertRaisesRegex(UsMarketSessionSourceError, "必须包含明确时区"):
            build_us_market_window_evidence(
                REVIEW_DATE,
                "2026-08-15T17:54:57",
                AS_OF,
                A_SHARE_CLOSE,
                NEXT_A_SHARE_OPEN,
                [],
            )


if __name__ == "__main__":
    unittest.main()
