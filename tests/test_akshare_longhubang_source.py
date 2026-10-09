from __future__ import annotations

from datetime import date
import unittest

from stock_review.evidence.akshare_longhubang_source import (
    AKSHARE_LONGHUBANG_SOURCE,
    collect_akshare_daily_longhubang_evidence,
)
from stock_review.evidence.longhubang_source import LonghubangSourceError


class FakeFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        if orient != "records":
            raise AssertionError("测试帧只支持 records。")
        return self.rows


class FakeAkshareLonghubangClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def stock_lhb_detail_em(self, *, start_date: str, end_date: str) -> FakeFrame:
        self.calls.append(("daily", start_date, end_date))
        return FakeFrame(
            [
                {
                    "上榜日": date(2026, 8, 14),
                    "代码": "300308",
                    "名称": "中际旭创",
                    "上榜原因": "日涨幅偏离值达到规定阈值",
                    "龙虎榜买入额": 5000,
                    "龙虎榜卖出额": 3000,
                    "龙虎榜净买额": 2000,
                },
                {
                    "上榜日": date(2026, 8, 14),
                    "代码": "600000",
                    "名称": "浦发银行",
                    "上榜原因": "池外公开日榜记录",
                },
            ]
        )

    def stock_lhb_stock_detail_em(
        self,
        *,
        symbol: str,
        date: str,
        flag: str,
    ) -> FakeFrame:
        self.calls.append(("seats", symbol, date, flag))
        duplicate_institution = {
            "交易营业部名称": "机构专用",
            "类型": "机构专用",
            "买入金额": 1000,
            "卖出金额": 200,
            "净额": 800,
        }
        if flag == "买入":
            return FakeFrame(
                [
                    duplicate_institution,
                    {
                        "交易营业部名称": "某证券营业部",
                        "类型": "营业部",
                        "买入金额": 500,
                        "卖出金额": 100,
                        "净额": 400,
                    },
                ]
            )
        return FakeFrame(
            [
                duplicate_institution,
                {
                    "交易营业部名称": "机构专用",
                    "类型": "机构专用",
                    "买入金额": 100,
                    "卖出金额": 200,
                    "净额": -100,
                },
            ]
        )


class AkshareLonghubangAdapterTests(unittest.TestCase):
    def test_adapter_maps_actual_columns_and_only_requests_listed_candidates(self) -> None:
        client = FakeAkshareLonghubangClient()

        result = collect_akshare_daily_longhubang_evidence(
            "2026-08-14",
            [
                {"code": "300308", "name": "中际旭创"},
                {"code": "000001", "name": "平安银行"},
            ],
            client,
        )

        self.assertEqual(
            client.calls,
            [
                ("daily", "20260814", "20260814"),
                ("seats", "300308", "20260814", "买入"),
                ("seats", "300308", "20260814", "卖出"),
            ],
        )
        self.assertEqual(result["source"], AKSHARE_LONGHUBANG_SOURCE)
        self.assertEqual(result["public_listed_count"], 2)
        self.assertEqual(result["consumed_listed_count"], 1)

        listed = result["records"][0]
        self.assertEqual(listed["reason"], "日涨幅偏离值达到规定阈值")
        self.assertEqual(listed["buy_amount"], 5000)
        self.assertEqual(listed["sell_amount"], 3000)
        self.assertEqual(listed["net_amount"], 2000)
        self.assertEqual(listed["institutional_net_amount"], 700)
        self.assertEqual(listed["raw_seat_names"], ["机构专用", "某证券营业部", "机构专用"])

        not_listed = result["records"][1]
        self.assertEqual(not_listed["status"], "not_listed")
        self.assertIn("不构成负面证据", not_listed["note"])

    def test_adapter_rejects_daily_rows_from_another_date_before_seat_requests(self) -> None:
        class WrongDateClient(FakeAkshareLonghubangClient):
            def stock_lhb_detail_em(self, *, start_date: str, end_date: str) -> FakeFrame:
                self.calls.append(("daily", start_date, end_date))
                return FakeFrame(
                    [
                        {
                            "上榜日": date(2026, 8, 13),
                            "代码": "300308",
                            "名称": "中际旭创",
                        }
                    ]
                )

        client = WrongDateClient()

        with self.assertRaisesRegex(LonghubangSourceError, "日期与复盘日期不一致"):
            collect_akshare_daily_longhubang_evidence(
                "2026-08-14",
                [{"code": "300308", "name": "中际旭创"}],
                client,
            )

        self.assertEqual(client.calls, [("daily", "20260814", "20260814")])


if __name__ == "__main__":
    unittest.main()
