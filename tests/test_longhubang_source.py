from __future__ import annotations

import unittest

from stock_review.evidence.longhubang_source import (
    LonghubangSourceError,
    NOT_LISTED_NOTE,
    build_daily_longhubang_evidence,
    collect_daily_longhubang_evidence,
)


class FakeLonghubangClient:
    source_name = "public:fake_longhubang"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def fetch_longhubang_daily(self, review_date: str) -> list[dict[str, object]]:
        self.calls.append(("daily", review_date))
        return [
            {
                "交易日期": review_date,
                "代码": 300308,
                "名称": "中际旭创",
                "上榜原因": "日涨幅偏离值达到规定阈值",
                "买入额": "1,200",
                "卖出额": 700,
                "龙虎榜净买额": 500,
            },
            {
                "交易日期": review_date,
                "代码": "600000",
                "名称": "浦发银行",
                "上榜原因": "公开日榜中的池外记录",
            },
        ]

    def fetch_longhubang_seats(self, review_date: str) -> list[dict[str, object]]:
        self.calls.append(("seats", review_date))
        return [
            {
                "交易日期": review_date,
                "代码": "300308",
                "席位名称": "机构专用",
                "席位类型": "机构",
                "净额": 300,
            },
            {
                "交易日期": review_date,
                "代码": "300308",
                "席位名称": "某证券营业部",
                "净额": 200,
            },
            {
                "交易日期": review_date,
                "代码": "300308",
                "席位名称": "机构专用",
                "席位类型": "机构",
                "净额": -50,
            },
        ]


class DailyLonghubangEvidenceTests(unittest.TestCase):
    def test_collector_filters_public_list_to_finite_candidates(self) -> None:
        client = FakeLonghubangClient()

        result = collect_daily_longhubang_evidence(
            "2026-08-14",
            [
                {"code": 300308, "name": "中际旭创"},
                {"code": "000001", "name": "平安银行"},
            ],
            client,
        )

        self.assertEqual(client.calls, [("daily", "2026-08-14"), ("seats", "2026-08-14")])
        self.assertEqual(result["source"], "public:fake_longhubang")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["public_listed_count"], 2)
        self.assertEqual(result["consumed_listed_count"], 1)
        self.assertEqual([record["code"] for record in result["records"]], ["300308", "000001"])

        listed = result["records"][0]
        self.assertTrue(listed["listed"])
        self.assertEqual(listed["reason"], "日涨幅偏离值达到规定阈值")
        self.assertEqual(listed["buy_amount"], 1200)
        self.assertEqual(listed["sell_amount"], 700)
        self.assertEqual(listed["net_amount"], 500)
        self.assertEqual(listed["institutional_net_amount"], 250)
        self.assertEqual(listed["raw_seat_names"], ["机构专用", "某证券营业部", "机构专用"])

    def test_not_listed_candidate_is_neutral_and_has_no_invented_values(self) -> None:
        result = build_daily_longhubang_evidence(
            "2026-08-14",
            [{"code": "000001", "name": "平安银行"}],
            [],
        )

        record = result["records"][0]
        self.assertEqual(record["status"], "not_listed")
        self.assertFalse(record["listed"])
        self.assertEqual(record["note"], NOT_LISTED_NOTE)
        self.assertIn("不构成负面证据", result["not_listed_semantics"])
        for field in (
            "reason",
            "buy_amount",
            "sell_amount",
            "net_amount",
            "institutional_net_amount",
            "raw_seat_names",
        ):
            self.assertIsNone(record[field])

    def test_missing_source_fields_remain_none_without_seat_identity_guessing(self) -> None:
        result = build_daily_longhubang_evidence(
            "2026-08-14",
            [{"code": "300308", "name": "中际旭创"}],
            [
                {
                    "trade_date": "2026-08-14",
                    "code": "300308",
                    "name": "中际旭创",
                    "buy_amount": float("nan"),
                }
            ],
            [
                {
                    "trade_date": "2026-08-14",
                    "code": "300308",
                    "seat_name": "某证券营业部",
                    "net_amount": 800,
                }
            ],
        )

        record = result["records"][0]
        self.assertIsNone(record["reason"])
        self.assertIsNone(record["buy_amount"])
        self.assertIsNone(record["sell_amount"])
        self.assertIsNone(record["net_amount"])
        self.assertIsNone(record["institutional_net_amount"])
        self.assertEqual(record["raw_seat_names"], ["某证券营业部"])

    def test_wrong_sample_date_is_rejected(self) -> None:
        with self.assertRaisesRegex(LonghubangSourceError, "日期与复盘日期不一致"):
            build_daily_longhubang_evidence(
                "2026-08-14",
                [{"code": "300308", "name": "中际旭创"}],
                [{"trade_date": "2026-08-13", "code": "300308", "name": "中际旭创"}],
            )

    def test_conflicting_daily_totals_are_rejected(self) -> None:
        with self.assertRaisesRegex(LonghubangSourceError, "多条日榜记录中不一致"):
            build_daily_longhubang_evidence(
                "2026-08-14",
                [{"code": "300308", "name": "中际旭创"}],
                [
                    {"code": "300308", "name": "中际旭创", "buy_amount": 100},
                    {"code": "300308", "name": "中际旭创", "buy_amount": 200},
                ],
            )


if __name__ == "__main__":
    unittest.main()
