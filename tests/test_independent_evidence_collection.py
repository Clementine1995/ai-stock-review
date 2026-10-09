from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stock_review.cli import main
from stock_review.evidence.collect_independent_evidence import (
    IndependentEvidenceCollectionError,
    collect_longhubang_evidence_file,
    collect_sector_kline_evidence_file,
    collect_us_market_evidence_file,
    write_json_evidence,
)


class FakeFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        if orient != "records":
            raise AssertionError("测试帧只支持 records。")
        return self.rows


class FakeLonghubangClient:
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
                    "龙虎榜买入额": 1200,
                    "龙虎榜卖出额": 700,
                    "龙虎榜净买额": 500,
                }
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
        return FakeFrame(
            [
                {
                    "交易营业部名称": "机构专用",
                    "类型": "机构专用",
                    "买入金额": 300,
                    "卖出金额": 100,
                    "净额": 200,
                }
            ]
        )


class FakeUsMarketClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def index_us_stock_sina(self, *, symbol: str) -> FakeFrame:
        self.calls.append(symbol)
        closes = {
            ".IXIC": (100, 102),
            ".INX": (200, 198),
        }[symbol]
        return FakeFrame(
            [
                {"date": date(2026, 8, 13), "close": closes[0]},
                {"date": date(2026, 8, 14), "close": closes[1]},
            ]
        )


class IndependentEvidenceCollectionTests(unittest.TestCase):
    def test_atomic_json_replace_failure_preserves_existing_evidence(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            output_path = root / "2026-08-14_longhubang.json"
            output_path.write_text("旧证据\n", encoding="utf-8")

            with patch.object(Path, "replace", side_effect=OSError("替换失败")), self.assertRaises(
                OSError
            ):
                write_json_evidence(output_path, {"status": "available"})

            self.assertEqual(output_path.read_text(encoding="utf-8"), "旧证据\n")
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_longhubang_file_uses_only_candidates_from_same_day_history(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            write_candidate_history(root)
            client = FakeLonghubangClient()
            log_path = root / "logs" / "stock_review.log"

            output_path = collect_longhubang_evidence_file(
                "2026-08-14",
                "akshare",
                root,
                log_path,
                ak_client=client,
            )

            self.assertEqual(output_path, root / "2026-08-14_longhubang.json")
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["candidate_history_file"], "2026-08-14_review_candidate_history.json")
            self.assertEqual(payload["candidate_count"], 2)
            self.assertEqual(payload["consumed_listed_count"], 1)
            self.assertEqual([record["code"] for record in payload["records"]], ["300308", "000001"])
            self.assertEqual(
                client.calls,
                [
                    ("daily", "20260814", "20260814"),
                    ("seats", "300308", "20260814", "买入"),
                    ("seats", "300308", "20260814", "卖出"),
                ],
            )
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("command=evidence collect-longhubang", log_text)
            self.assertIn("candidate_count=2", log_text)

    def test_candidate_history_date_mismatch_stops_before_network_client(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            write_candidate_history(root, sample_date="2026-08-13")
            client = FakeLonghubangClient()

            with self.assertRaisesRegex(
                IndependentEvidenceCollectionError,
                "日期与目标日期不一致",
            ):
                collect_longhubang_evidence_file(
                    "2026-08-14",
                    "akshare",
                    root,
                    root / "stock_review.log",
                    ak_client=client,
                )

            self.assertEqual(client.calls, [])
            self.assertFalse((root / "2026-08-14_longhubang.json").exists())
            self.assertIn(
                "status=failed",
                (root / "stock_review.log").read_text(encoding="utf-8"),
            )

    def test_us_market_file_preserves_explicit_window_and_cutoff(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            client = FakeUsMarketClient()
            log_path = root / "stock_review.log"

            output_path = collect_us_market_evidence_file(
                "2026-08-14",
                "akshare",
                root,
                log_path,
                as_of="2026-08-15T04:30:00+08:00",
                a_share_close_at="2026-08-14T15:00:00+08:00",
                next_a_share_open_at="2026-08-17T09:30:00+08:00",
                generated_at="2026-08-15T05:00:00+08:00",
                ak_client=client,
            )

            self.assertEqual(output_path, root / "2026-08-14_us_market.json")
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["review_date"], "2026-08-14")
            self.assertEqual(payload["generated_at"], "2026-08-15T05:00:00+08:00")
            self.assertEqual(payload["as_of"], "2026-08-15T04:30:00+08:00")
            self.assertEqual(payload["us_session_date"], "2026-08-14")
            self.assertEqual(payload["status"], "available")
            self.assertEqual(
                payload["window"],
                {
                    "a_share_close_at": "2026-08-14T15:00:00+08:00",
                    "next_a_share_open_at": "2026-08-17T09:30:00+08:00",
                    "boundary": "open_interval",
                },
            )
            self.assertEqual(client.calls, [".IXIC", ".INX"])
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("command=evidence collect-us-market", log_text)
            self.assertIn("evidence_status=available", log_text)

    def test_longhubang_json_write_failure_is_logged(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            write_candidate_history(root)
            log_path = root / "stock_review.log"

            with patch(
                "stock_review.evidence.collect_independent_evidence.write_json_evidence",
                side_effect=OSError("磁盘已满"),
            ), self.assertRaisesRegex(
                IndependentEvidenceCollectionError,
                "独立证据文件写入失败",
            ):
                collect_longhubang_evidence_file(
                    "2026-08-14",
                    "akshare",
                    root,
                    log_path,
                    ak_client=FakeLonghubangClient(),
                )

            self.assertFalse((root / "2026-08-14_longhubang.json").exists())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("command=evidence collect-longhubang", log_text)
            self.assertIn("status=failed", log_text)
            self.assertIn("error=磁盘已满", log_text)

    def test_us_market_output_directory_failure_is_logged(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            log_path = root / "stock_review.log"
            blocked_output_dir = root / "blocked-output"
            blocked_output_dir.write_text("这是文件，不是目录。", encoding="utf-8")

            with self.assertRaisesRegex(
                IndependentEvidenceCollectionError,
                "独立证据文件写入失败",
            ):
                collect_us_market_evidence_file(
                    "2026-08-14",
                    "akshare",
                    blocked_output_dir,
                    log_path,
                    as_of="2026-08-15T04:30:00+08:00",
                    a_share_close_at="2026-08-14T15:00:00+08:00",
                    next_a_share_open_at="2026-08-17T09:30:00+08:00",
                    generated_at="2026-08-15T05:00:00+08:00",
                    ak_client=FakeUsMarketClient(),
                )

            self.assertTrue(blocked_output_dir.is_file())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("command=evidence collect-us-market", log_text)
            self.assertIn("status=failed", log_text)
            self.assertIn("error=", log_text)

    def test_sector_kline_json_write_failure_is_logged(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            write_sector_snapshot(root)
            log_path = root / "stock_review.log"
            payload = {
                "records": [{"name": "通信设备"}],
                "available_count": 0,
            }

            with patch(
                "stock_review.evidence.collect_independent_evidence.collect_akshare_sector_kline_evidence",
                return_value=payload,
            ), patch(
                "stock_review.evidence.collect_independent_evidence.write_json_evidence",
                side_effect=OSError("磁盘已满"),
            ), self.assertRaisesRegex(
                IndependentEvidenceCollectionError,
                "独立证据文件写入失败",
            ):
                collect_sector_kline_evidence_file(
                    "2026-08-14",
                    "akshare",
                    root,
                    log_path,
                    lookback_bars=5,
                )

            self.assertFalse((root / "2026-08-14_sector_kline.json").exists())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("command=evidence collect-sector-kline", log_text)
            self.assertIn("status=failed", log_text)
            self.assertIn("error=磁盘已满", log_text)

    def test_cli_forwards_longhubang_paths_without_snapshot_or_database(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            output_path = root / "2026-08-14_longhubang.json"
            log_path = root / "stock_review.log"
            with patch(
                "stock_review.cli.collect_longhubang_evidence_file",
                return_value=output_path,
            ) as collector, redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "evidence",
                        "collect-longhubang",
                        "--date",
                        "2026-08-14",
                        "--source",
                        "akshare",
                        "--output-dir",
                        str(root),
                        "--log-path",
                        str(log_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            collector.assert_called_once_with("2026-08-14", "akshare", root, log_path)

    def test_cli_forwards_all_explicit_us_market_window_arguments(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            output_path = root / "2026-08-14_us_market.json"
            log_path = root / "stock_review.log"
            with patch(
                "stock_review.cli.collect_us_market_evidence_file",
                return_value=output_path,
            ) as collector, redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "evidence",
                        "collect-us-market",
                        "--date",
                        "2026-08-14",
                        "--source",
                        "akshare",
                        "--output-dir",
                        str(root),
                        "--log-path",
                        str(log_path),
                        "--as-of",
                        "2026-08-15T04:30:00+08:00",
                        "--a-share-close-at",
                        "2026-08-14T15:00:00+08:00",
                        "--next-a-share-open-at",
                        "2026-08-17T09:30:00+08:00",
                    ]
                )

            self.assertEqual(exit_code, 0)
            collector.assert_called_once_with(
                "2026-08-14",
                "akshare",
                root,
                log_path,
                as_of="2026-08-15T04:30:00+08:00",
                a_share_close_at="2026-08-14T15:00:00+08:00",
                next_a_share_open_at="2026-08-17T09:30:00+08:00",
            )


def write_candidate_history(root: Path, *, sample_date: str = "2026-08-14") -> None:
    payload = {
        "trade_date": "2026-08-14",
        "sample_date": sample_date,
        "source": "akshare:review_candidate_history",
        "records": [
            {"code": "300308", "name": "中际旭创"},
            {"code": "000001", "name": "平安银行"},
        ],
    }
    (root / "2026-08-14_review_candidate_history.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def write_sector_snapshot(root: Path) -> None:
    payload = {
        "trade_date": "2026-08-14",
        "sample_date": "2026-08-14",
        "source": "fixture",
        "sectors": [
            {"name": "通信设备", "source_type": "hhxg_strong_sector", "rank": 1}
        ],
    }
    (root / "2026-08-14_snapshot.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
