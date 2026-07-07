from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stock_review.evidence import akshare_source
from stock_review.evidence.akshare_source import collect_akshare_market_evidence
from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import collect_akshare_evidence_snapshot


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        self.orient = orient
        return self.rows


class FakeAkshareClient:
    def stock_zh_index_daily_em(self, symbol):
        rows_by_symbol = {
            "sh000001": [
                {"date": "2026-07-03", "close": "3000", "amount": "100"},
                {"date": "2026-07-06", "close": "3030", "amount": "120"},
            ],
            "sz399001": [
                {"date": "2026-07-03", "close": "9000", "amount": "200"},
                {"date": "2026-07-06", "close": "8910", "amount": "180"},
            ],
            "sz399006": [
                {"date": "2026-07-03", "close": "1800", "amount": "50"},
                {"date": "2026-07-06", "close": "1818", "amount": "60"},
            ],
        }
        return FakeFrame(rows_by_symbol[symbol])


class BrokenAkshareClient:
    def stock_zh_index_daily_em(self, symbol):
        raise RuntimeError(f"{symbol} blocked")


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "data": {
                "klines": [
                    "2026-07-03,100,100,101,99,1000,10.00,0.00",
                    "2026-07-06,104,105,106,103,1200,12.00,5.00",
                ]
            }
        }


class FakeSession:
    last_session = None

    def __init__(self):
        self.trust_env = True
        self.url = ""
        FakeSession.last_session = self

    def get(self, url, headers, timeout):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        return FakeResponse()


class AkshareSourceTest(unittest.TestCase):
    def test_collect_akshare_market_evidence_builds_market_snapshot_input(self):
        raw_data = collect_akshare_market_evidence("2026-07-06", ak_client=FakeAkshareClient())

        self.assertEqual(raw_data["source"], "akshare")
        self.assertEqual(raw_data["sample_date"], "2026-07-06")
        self.assertEqual(len(raw_data["market"]["indices"]), 3)
        self.assertEqual(raw_data["market"]["indices"][0]["change_percent"], 1)
        self.assertEqual(raw_data["market"]["indices"][1]["change_percent"], -1)
        self.assertEqual(raw_data["market"]["total_amount"], 360)
        self.assertEqual(
            raw_data["market"]["total_amount_source"],
            "akshare_stock_zh_index_daily_em_amount_sum",
        )

    def test_akshare_market_snapshot_keeps_non_market_gaps(self):
        raw_data = collect_akshare_market_evidence("2026-07-06", ak_client=FakeAkshareClient())
        snapshot = build_evidence_snapshot("2026-07-06", raw_data)

        self.assertNotIn("missing_indices", snapshot.missing_fields)
        self.assertNotIn("missing_total_amount", snapshot.missing_fields)
        self.assertIn("missing_sentiment", snapshot.missing_fields)
        self.assertIn("missing_sectors", snapshot.missing_fields)
        self.assertIn("missing_stocks", snapshot.missing_fields)

    def test_collect_akshare_market_evidence_uses_eastmoney_fallback_after_client_failure(self):
        original_request = akshare_source.request_eastmoney_index_rows

        def fake_request(symbol):
            return [
                {"date": "2026-07-03", "close": "100", "amount": "10"},
                {"date": "2026-07-06", "close": "105", "amount": "12"},
            ]

        akshare_source.request_eastmoney_index_rows = fake_request
        try:
            raw_data = collect_akshare_market_evidence("2026-07-06", ak_client=BrokenAkshareClient())
        finally:
            akshare_source.request_eastmoney_index_rows = original_request

        self.assertEqual(len(raw_data["market"]["indices"]), 3)
        self.assertEqual(raw_data["market"]["indices"][0]["change_percent"], 5)
        self.assertEqual(raw_data["market"]["total_amount"], 36)
        self.assertTrue(any(event["source"] == "eastmoney" for event in raw_data["events"]))

    def test_eastmoney_fallback_ignores_environment_proxy(self):
        import requests

        original_session = requests.Session
        requests.Session = FakeSession
        try:
            rows = akshare_source.request_eastmoney_index_rows("sh000001")
        finally:
            requests.Session = original_session

        self.assertEqual(rows[-1]["close"], "105")
        self.assertIsNotNone(FakeSession.last_session)
        self.assertFalse(FakeSession.last_session.trust_env)
        self.assertIn("fields1=f1,f2,f3,f4,f5", FakeSession.last_session.url)

    def test_collect_akshare_writes_standard_snapshot_path(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)

            from stock_review.evidence import manage_evidence_snapshot

            original_collect = manage_evidence_snapshot.collect_akshare_market_evidence
            manage_evidence_snapshot.collect_akshare_market_evidence = (
                lambda trade_date: collect_akshare_market_evidence(trade_date, ak_client=FakeAkshareClient())
            )
            try:
                output_path = collect_akshare_evidence_snapshot(
                    "2026-07-06",
                    output_dir=root,
                    database_path=root / "stock_review.sqlite",
                )
            finally:
                manage_evidence_snapshot.collect_akshare_market_evidence = original_collect

            self.assertEqual(output_path, root / "2026-07-06_snapshot.json")
            self.assertTrue(output_path.exists())


if __name__ == "__main__":
    unittest.main()
