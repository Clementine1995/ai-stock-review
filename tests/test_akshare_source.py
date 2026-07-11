from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.cli import build_parser
from stock_review.evidence import akshare_source
from stock_review.evidence.akshare_source import (
    AkshareRequestPacer,
    classify_akshare_error,
    collect_akshare_market_evidence,
    collect_akshare_sector_evidence,
    collect_akshare_sentiment_evidence,
    collect_akshare_stock_evidence,
)
from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import (
    collect_akshare_evidence_snapshot,
    is_akshare_scope_covered,
)


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


class FakeSentimentAkshareClient:
    def stock_zt_pool_em(self, date):
        self.limit_up_date = date
        return FakeFrame(
            [
                {"代码": "300024", "名称": "机器人", "连板数": 3, "涨跌幅": 20, "所属行业": "软件开发"},
                {"代码": "002747", "名称": "埃斯顿", "连板数": 2, "涨跌幅": 10, "所属行业": "自动化设备"},
            ]
        )

    def stock_zt_pool_zbgc_em(self, date):
        self.broken_board_date = date
        return FakeFrame([{"代码": "000001", "名称": "平安银行"}])

    def stock_zt_pool_dtgc_em(self, date):
        self.limit_down_date = date
        return FakeFrame([{"代码": "600000", "名称": "浦发银行"}])


class FakeStockAkshareClient(FakeSentimentAkshareClient):
    def stock_zt_pool_em(self, date):
        frame = super().stock_zt_pool_em(date)
        return FakeFrame(
            [
                *frame.rows,
                {"代码": "000001", "名称": "平安银行", "连板数": 1, "涨跌幅": 10, "所属行业": "银行"},
            ]
        )


class FakeSectorAkshareClient:
    def stock_board_concept_name_em(self):
        return FakeFrame(
            [
                {
                    "板块名称": "机器人",
                    "板块代码": "BK0001",
                    "涨跌幅": 3.8,
                    "总市值": 120000000000,
                    "换手率": 4.2,
                    "上涨家数": 20,
                    "下跌家数": 3,
                    "领涨股票": "机器人",
                    "领涨股票-涨跌幅": 12.4,
                },
                {
                    "板块名称": "低位样例",
                    "板块代码": "BK0002",
                    "涨跌幅": -1.2,
                    "总市值": 1,
                    "换手率": 1,
                    "上涨家数": 1,
                    "下跌家数": 10,
                    "领涨股票": "样例",
                    "领涨股票-涨跌幅": 0.1,
                },
            ]
        )

    def stock_board_industry_name_em(self):
        return FakeFrame(
            [
                {
                    "板块名称": "电力设备",
                    "板块代码": "BK1001",
                    "涨跌幅": 1.6,
                    "总市值": 76000000000,
                    "换手率": 2.1,
                    "上涨家数": 15,
                    "下跌家数": 6,
                    "领涨股票": "宁德时代",
                    "领涨股票-涨跌幅": 5.5,
                }
            ]
        )


class PartiallyBrokenSectorAkshareClient(FakeSectorAkshareClient):
    def stock_board_industry_name_em(self):
        raise RuntimeError("industry blocked")


class EastmoneyBrokenThsSectorAkshareClient:
    def stock_board_concept_name_em(self):
        raise RuntimeError("concept blocked")

    def stock_board_industry_name_em(self):
        raise RuntimeError("industry blocked")

    def stock_board_industry_summary_ths(self):
        return FakeFrame(
            [
                {
                    "板块": "油气开采及服务",
                    "涨跌幅": 3.52,
                    "总成交量": 1110.38,
                    "总成交额": 85.41,
                    "净流入": 5.38,
                    "上涨家数": 17,
                    "下跌家数": 2,
                    "领涨股": "科力股份",
                    "领涨股-涨跌幅": 11.13,
                }
            ]
        )


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def read(self):
        return (
            b'{"data":{"klines":['
            b'"2026-07-03,100,100,101,99,1000,10.00,0.00",'
            b'"2026-07-06,104,105,106,103,1200,12.00,5.00"'
            b"]}}"
        )


class FakeOpener:
    last_opener = None

    def __init__(self):
        self.request = None
        self.timeout = None
        FakeOpener.last_opener = self

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return FakeResponse()


class AkshareSourceTest(unittest.TestCase):
    def test_akshare_request_pacer_waits_between_requests_only(self):
        delays = []
        request_pacer = AkshareRequestPacer(sleep=delays.append)

        request_pacer.wait_before_request()
        request_pacer.wait_before_request()
        request_pacer.wait_before_request()

        self.assertEqual(delays, [0.3, 0.3])

    def test_akshare_error_classification_does_not_assert_ip_blocking(self):
        self.assertEqual(classify_akshare_error(RuntimeError("HTTP 429 too many requests")), "rate_limited")
        self.assertEqual(classify_akshare_error(RuntimeError("ReadTimeout")), "network_timeout")
        self.assertEqual(classify_akshare_error(RuntimeError("HTTP 403 Forbidden")), "access_denied")
        self.assertEqual(classify_akshare_error(RuntimeError("connection reset")), "upstream_unavailable")

    def test_existing_akshare_scope_is_reused_only_when_scope_fields_are_complete(self):
        snapshot_data = {
            "source": "akshare",
            "market": {"indices": [{"name": "上证指数"}], "total_amount": 1},
            "sentiment": {
                "limit_up_count": 1,
                "limit_down_count": 0,
                "broken_board_rate": 0,
                "highest_board": 1,
            },
            "sectors": [{"name": "机器人"}],
            "stocks": [{"name": "样例股"}],
        }

        self.assertTrue(is_akshare_scope_covered(snapshot_data, "market"))
        self.assertTrue(is_akshare_scope_covered(snapshot_data, "sentiment"))
        self.assertTrue(is_akshare_scope_covered(snapshot_data, "sectors"))
        self.assertTrue(is_akshare_scope_covered(snapshot_data, "stocks"))
        snapshot_data["sentiment"]["highest_board"] = None
        self.assertFalse(is_akshare_scope_covered(snapshot_data, "sentiment"))

    def test_collect_akshare_reuses_existing_scope_without_calling_source(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            snapshot_path = root / "2026-07-06_snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "source": "akshare",
                        "market": {"indices": [{"name": "上证指数"}], "total_amount": 1},
                    }
                ),
                encoding="utf-8",
            )

            from stock_review.evidence import manage_evidence_snapshot

            original_collect = manage_evidence_snapshot.collect_akshare_market_evidence
            manage_evidence_snapshot.collect_akshare_market_evidence = lambda trade_date: self.fail("不应重复调用数据源")
            try:
                output_path = collect_akshare_evidence_snapshot("2026-07-06", output_dir=root)
            finally:
                manage_evidence_snapshot.collect_akshare_market_evidence = original_collect

            self.assertEqual(output_path, snapshot_path)

    def test_evidence_collect_refresh_requires_explicit_flag(self):
        parser = build_parser()

        default_args = parser.parse_args(
            ["evidence", "collect", "--date", "2026-07-06", "--source", "akshare", "--scope", "market", "--output-dir", "data/evidence"]
        )
        refresh_args = parser.parse_args(
            [
                "evidence",
                "collect",
                "--date",
                "2026-07-06",
                "--source",
                "akshare",
                "--scope",
                "market",
                "--output-dir",
                "data/evidence",
                "--refresh",
            ]
        )

        self.assertFalse(default_args.refresh)
        self.assertTrue(refresh_args.refresh)

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

    def test_collect_akshare_sentiment_evidence_builds_sentiment_snapshot_input(self):
        client = FakeSentimentAkshareClient()

        raw_data = collect_akshare_sentiment_evidence("2026-07-06", ak_client=client)
        snapshot = build_evidence_snapshot("2026-07-06", raw_data)

        self.assertEqual(client.limit_up_date, "20260706")
        self.assertEqual(raw_data["sentiment"]["limit_up_count"], 2)
        self.assertEqual(raw_data["sentiment"]["limit_down_count"], 1)
        self.assertEqual(raw_data["sentiment"]["highest_board"], 3)
        self.assertEqual(raw_data["sentiment"]["broken_board_rate"], 0.3333)
        self.assertIn("missing_emotion_temperature", snapshot.missing_fields)
        self.assertIn("missing_indices", snapshot.missing_fields)

    def test_collect_akshare_sector_evidence_builds_sector_snapshot_input(self):
        raw_data = collect_akshare_sector_evidence("2026-07-06", ak_client=FakeSectorAkshareClient())
        snapshot = build_evidence_snapshot("2026-07-06", raw_data)

        self.assertEqual(len(raw_data["sectors"]), 3)
        self.assertEqual(raw_data["sectors"][0]["name"], "机器人")
        self.assertEqual(raw_data["sectors"][0]["source_type"], "concept")
        self.assertEqual(raw_data["sectors"][0]["change_percent"], 3.8)
        self.assertEqual(raw_data["sectors"][0]["market_value"], 120000000000)
        self.assertEqual(raw_data["sectors"][0]["leading_stock"], "机器人")
        self.assertNotIn("missing_sectors", snapshot.missing_fields)
        self.assertIn("missing_stocks", snapshot.missing_fields)

    def test_collect_akshare_sector_evidence_keeps_partial_sector_results(self):
        raw_data = collect_akshare_sector_evidence("2026-07-06", ak_client=PartiallyBrokenSectorAkshareClient())
        snapshot = build_evidence_snapshot("2026-07-06", raw_data)

        self.assertTrue(raw_data["sectors"])
        self.assertTrue(any(event["title"] == "industry 板块采集失败" for event in raw_data["events"]))
        self.assertNotIn("missing_sectors", snapshot.missing_fields)

    def test_collect_akshare_stock_evidence_uses_sector_leaders_and_consecutive_limit_up_stocks(self):
        sectors = collect_akshare_sector_evidence(
            "2026-07-06",
            ak_client=FakeSectorAkshareClient(),
        )["sectors"]

        raw_data = collect_akshare_stock_evidence(
            "2026-07-06",
            sectors=sectors,
            ak_client=FakeStockAkshareClient(),
        )
        snapshot = build_evidence_snapshot("2026-07-06", raw_data)

        self.assertEqual(raw_data["stocks"][0]["name"], "机器人")
        self.assertEqual(raw_data["stocks"][0]["code"], "待确认")
        self.assertEqual(raw_data["stocks"][0]["sector"], "机器人")
        self.assertEqual(raw_data["stocks"][0]["role_source"], "concept 板块领涨股")
        self.assertTrue(any(stock["code"] == "300024" for stock in raw_data["stocks"]))
        self.assertFalse(any(stock["code"] == "000001" for stock in raw_data["stocks"]))
        self.assertEqual(
            next(stock["change_percent"] for stock in raw_data["stocks"] if stock["code"] == "300024"),
            20,
        )
        self.assertNotIn("missing_stocks", snapshot.missing_fields)

    def test_collect_akshare_sector_evidence_uses_ths_industry_when_eastmoney_fails(self):
        raw_data = collect_akshare_sector_evidence("2026-07-06", ak_client=EastmoneyBrokenThsSectorAkshareClient())
        snapshot = build_evidence_snapshot("2026-07-06", raw_data)

        self.assertEqual(raw_data["sectors"][0]["name"], "油气开采及服务")
        self.assertEqual(raw_data["sectors"][0]["source_type"], "industry_ths")
        self.assertEqual(raw_data["sectors"][0]["turnover"], 85.41)
        self.assertEqual(raw_data["sectors"][0]["net_inflow"], 5.38)
        self.assertEqual(raw_data["sectors"][0]["leading_stock"], "科力股份")
        self.assertNotIn("missing_sectors", snapshot.missing_fields)

    def test_eastmoney_sector_fallback_maps_board_fields(self):
        original_request_json = akshare_source.request_json_without_environment_proxy

        def fake_request_json(url):
            return {
                "data": {
                    "diff": [
                        {
                            "f14": "机器人",
                            "f12": "BK0001",
                            "f3": 3.8,
                            "f20": 120000000000,
                            "f8": 4.2,
                            "f104": 20,
                            "f105": 3,
                            "f128": "机器人",
                            "f136": 12.4,
                        }
                    ]
                }
            }

        akshare_source.request_json_without_environment_proxy = fake_request_json
        try:
            rows = akshare_source.request_eastmoney_sector_rows("stock_board_concept_name_em")
        finally:
            akshare_source.request_json_without_environment_proxy = original_request_json

        self.assertEqual(rows[0]["板块名称"], "机器人")
        self.assertEqual(rows[0]["涨跌幅"], 3.8)
        self.assertEqual(rows[0]["领涨股票"], "机器人")

    def test_collect_akshare_market_evidence_uses_eastmoney_fallback_after_client_failure(self):
        original_request = akshare_source.request_eastmoney_index_rows

        def fake_request(symbol):
            return [
                {
                    "date": "2026-07-03",
                    "close": "100",
                    "amount": "10",
                    "__amount_source": "eastmoney_index_kline_amount",
                },
                {
                    "date": "2026-07-06",
                    "close": "105",
                    "amount": "12",
                    "__amount_source": "eastmoney_index_kline_amount",
                },
            ]

        akshare_source.request_eastmoney_index_rows = fake_request
        try:
            raw_data = collect_akshare_market_evidence("2026-07-06", ak_client=BrokenAkshareClient())
        finally:
            akshare_source.request_eastmoney_index_rows = original_request

        self.assertEqual(len(raw_data["market"]["indices"]), 3)
        self.assertEqual(raw_data["market"]["indices"][0]["change_percent"], 5)
        self.assertEqual(raw_data["market"]["total_amount"], 36)
        self.assertEqual(raw_data["market"]["total_amount_source"], "eastmoney_index_kline_amount")
        self.assertTrue(any(event["source"] == "eastmoney" for event in raw_data["events"]))

    def test_collect_akshare_market_evidence_uses_tencent_fallback_after_eastmoney_failure(self):
        original_eastmoney_request = akshare_source.request_eastmoney_index_rows
        original_tencent_request = akshare_source.request_tencent_index_rows

        def broken_eastmoney_request(symbol):
            raise RuntimeError(f"{symbol} eastmoney blocked")

        def fake_tencent_request(symbol):
            return [
                {
                    "date": "2026-07-03",
                    "close": "100",
                    "amount": "10",
                    "__amount_source": "tencent_index_kline_amount",
                },
                {
                    "date": "2026-07-06",
                    "close": "103",
                    "amount": "11",
                    "__amount_source": "tencent_index_kline_amount",
                },
            ]

        akshare_source.request_eastmoney_index_rows = broken_eastmoney_request
        akshare_source.request_tencent_index_rows = fake_tencent_request
        try:
            raw_data = collect_akshare_market_evidence("2026-07-06", ak_client=BrokenAkshareClient())
        finally:
            akshare_source.request_eastmoney_index_rows = original_eastmoney_request
            akshare_source.request_tencent_index_rows = original_tencent_request

        self.assertEqual(len(raw_data["market"]["indices"]), 3)
        self.assertEqual(raw_data["market"]["indices"][0]["change_percent"], 3)
        self.assertEqual(raw_data["market"]["total_amount"], 33)
        self.assertEqual(raw_data["market"]["total_amount_source"], "tencent_index_kline_amount")
        self.assertTrue(any(event["source"] == "tencent" for event in raw_data["events"]))

    def test_tencent_fallback_treats_sixth_kline_field_as_amount(self):
        original_request_json = akshare_source.request_json_without_environment_proxy

        def fake_request_json(url):
            return {
                "data": {
                    "sh000001": {
                        "day": [
                            ["2026-07-03", "100", "100", "101", "99", "10"],
                            ["2026-07-06", "104", "105", "106", "103", "12"],
                        ]
                    }
                }
            }

        akshare_source.request_json_without_environment_proxy = fake_request_json
        try:
            rows = akshare_source.request_tencent_index_rows("sh000001")
        finally:
            akshare_source.request_json_without_environment_proxy = original_request_json

        self.assertEqual(rows[-1]["amount"], "12")
        self.assertEqual(rows[-1]["__amount_source"], "tencent_index_kline_amount")

    def test_eastmoney_fallback_ignores_environment_proxy(self):
        original_build_opener = akshare_source.urllib.request.build_opener
        captured_handlers = []

        def fake_build_opener(*handlers):
            captured_handlers.extend(handlers)
            return FakeOpener()

        akshare_source.urllib.request.build_opener = fake_build_opener
        try:
            rows = akshare_source.request_eastmoney_index_rows("sh000001")
        finally:
            akshare_source.urllib.request.build_opener = original_build_opener

        self.assertEqual(rows[-1]["close"], "105")
        self.assertIsNotNone(FakeOpener.last_opener)
        self.assertEqual(len(captured_handlers), 1)
        self.assertIn("fields1=f1,f2,f3,f4,f5", FakeOpener.last_opener.request.full_url)

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

    def test_collect_akshare_sentiment_writes_standard_snapshot_path(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)

            from stock_review.evidence import manage_evidence_snapshot

            original_collect = manage_evidence_snapshot.collect_akshare_sentiment_evidence
            manage_evidence_snapshot.collect_akshare_sentiment_evidence = (
                lambda trade_date: collect_akshare_sentiment_evidence(trade_date, ak_client=FakeSentimentAkshareClient())
            )
            try:
                output_path = collect_akshare_evidence_snapshot(
                    "2026-07-06",
                    scope="sentiment",
                    output_dir=root,
                    database_path=root / "stock_review.sqlite",
                )
            finally:
                manage_evidence_snapshot.collect_akshare_sentiment_evidence = original_collect

            self.assertEqual(output_path, root / "2026-07-06_snapshot.json")
            self.assertTrue(output_path.exists())

    def test_collect_akshare_sentiment_keeps_existing_market_snapshot(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            snapshot_path = root / "2026-07-06_snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "trade_date": "2026-07-06",
                        "source": "akshare",
                        "sample_date": "2026-07-06",
                        "market": {"indices": [{"name": "上证指数"}], "total_amount": 1},
                        "sentiment": {},
                        "sectors": [],
                        "stocks": [],
                        "events": [{"title": "已有市场证据", "source": "akshare", "note": "market"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            from stock_review.evidence import manage_evidence_snapshot

            original_collect = manage_evidence_snapshot.collect_akshare_sentiment_evidence
            manage_evidence_snapshot.collect_akshare_sentiment_evidence = (
                lambda trade_date: collect_akshare_sentiment_evidence(trade_date, ak_client=FakeSentimentAkshareClient())
            )
            try:
                collect_akshare_evidence_snapshot(
                    "2026-07-06",
                    scope="sentiment",
                    output_dir=root,
                    database_path=root / "stock_review.sqlite",
                )
            finally:
                manage_evidence_snapshot.collect_akshare_sentiment_evidence = original_collect

            snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot_data["market"]["total_amount"], 1)
            self.assertEqual(snapshot_data["sentiment"]["limit_up_count"], 2)
            self.assertEqual(len(snapshot_data["events"]), 2)

    def test_collect_akshare_sectors_keeps_existing_market_and_sentiment_snapshot(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            snapshot_path = root / "2026-07-06_snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "trade_date": "2026-07-06",
                        "source": "akshare",
                        "sample_date": "2026-07-06",
                        "market": {"indices": [{"name": "上证指数"}], "total_amount": 1},
                        "sentiment": {
                            "limit_up_count": 2,
                            "limit_down_count": 1,
                            "highest_board": 3,
                            "broken_board_rate": 0.3333,
                        },
                        "sectors": [],
                        "stocks": [],
                        "events": [{"title": "已有证据", "source": "akshare", "note": "market_sentiment"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            from stock_review.evidence import manage_evidence_snapshot

            original_collect = manage_evidence_snapshot.collect_akshare_sector_evidence
            manage_evidence_snapshot.collect_akshare_sector_evidence = (
                lambda trade_date: collect_akshare_sector_evidence(trade_date, ak_client=FakeSectorAkshareClient())
            )
            try:
                collect_akshare_evidence_snapshot(
                    "2026-07-06",
                    scope="sectors",
                    output_dir=root,
                    database_path=root / "stock_review.sqlite",
                )
            finally:
                manage_evidence_snapshot.collect_akshare_sector_evidence = original_collect

            snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot_data["market"]["total_amount"], 1)
            self.assertEqual(snapshot_data["sentiment"]["limit_up_count"], 2)
            self.assertEqual(snapshot_data["sectors"][0]["name"], "机器人")
            self.assertEqual(len(snapshot_data["events"]), 2)

    def test_collect_akshare_stocks_keeps_existing_evidence_and_removes_stock_gap(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            snapshot_path = root / "2026-07-06_snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "trade_date": "2026-07-06",
                        "source": "akshare",
                        "sample_date": "2026-07-06",
                        "market": {"indices": [{"name": "上证指数"}], "total_amount": 1},
                        "sentiment": {
                            "limit_up_count": 2,
                            "limit_down_count": 1,
                            "highest_board": 3,
                            "broken_board_rate": 0.3333,
                        },
                        "sectors": [
                            {
                                "name": "机器人",
                                "source_type": "concept",
                                "leading_stock": "机器人",
                                "leading_stock_change_percent": 12.4,
                            }
                        ],
                        "stocks": [],
                        "events": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            from stock_review.evidence import manage_evidence_snapshot

            original_collect = manage_evidence_snapshot.collect_akshare_stock_evidence
            manage_evidence_snapshot.collect_akshare_stock_evidence = (
                lambda trade_date, sectors: collect_akshare_stock_evidence(
                    trade_date,
                    sectors=sectors,
                    ak_client=FakeStockAkshareClient(),
                )
            )
            try:
                collect_akshare_evidence_snapshot(
                    "2026-07-06",
                    scope="stocks",
                    output_dir=root,
                    database_path=root / "stock_review.sqlite",
                )
            finally:
                manage_evidence_snapshot.collect_akshare_stock_evidence = original_collect

            snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot_data["market"]["total_amount"], 1)
            self.assertEqual(snapshot_data["sectors"][0]["name"], "机器人")
            self.assertNotIn("missing_stocks", snapshot_data["missing_fields"])


if __name__ == "__main__":
    unittest.main()
