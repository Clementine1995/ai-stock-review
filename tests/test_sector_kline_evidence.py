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
from stock_review.evidence.akshare_sector_kline_source import (
    select_ranked_sector_objects,
)
from stock_review.evidence.collect_independent_evidence import (
    IndependentEvidenceCollectionError,
    collect_sector_kline_evidence_file,
)


DAILY_DATES = (
    "2026-08-05",
    "2026-08-06",
    "2026-08-07",
    "2026-08-10",
    "2026-08-11",
    "2026-08-12",
    "2026-08-13",
    "2026-08-14",
)


class FakeFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        if orient != "records":
            raise AssertionError("测试帧只支持 records。")
        return self.rows


class FakeSectorHistoryClient:
    def __init__(
        self,
        *,
        include_review_date: bool = True,
        concept_map: dict[str, str] | None = None,
        industry_map: dict[str, str] | None = None,
    ) -> None:
        self.include_review_date = include_review_date
        self.concept_map = concept_map or {
            "液冷服务器": "BK1001",
            "芯片概念": "BK1002",
        }
        self.industry_map = industry_map or {
            "通信设备": "BK2001",
            "电子化学品": "BK2002",
        }
        self.calls: list[tuple[str, dict[str, str]]] = []

    def stock_board_concept_name_em(self) -> FakeFrame:
        self.calls.append(("concept_names", {}))
        return FakeFrame(
            [
                {"板块名称": name, "板块代码": code}
                for name, code in self.concept_map.items()
            ]
        )

    def stock_board_industry_name_em(self) -> FakeFrame:
        self.calls.append(("industry_names", {}))
        return FakeFrame(
            [
                {"板块名称": name, "板块代码": code}
                for name, code in self.industry_map.items()
            ]
        )

    def stock_board_concept_hist_em(self, **kwargs: str) -> FakeFrame:
        self.calls.append(("concept", kwargs))
        return FakeFrame(build_daily_rows(self.include_review_date))

    def stock_board_industry_hist_em(self, **kwargs: str) -> FakeFrame:
        self.calls.append(("industry", kwargs))
        return FakeFrame(build_daily_rows(self.include_review_date))


class SectorKlineEvidenceTests(unittest.TestCase):
    def test_collects_top_three_explicit_types_with_exact_names(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            sectors = [
                {
                    "name": "液冷服务器",
                    "source_type": "hhxg_hot_theme",
                    "ranking_group": "hhxg_hot_theme",
                    "rank": 1,
                    "change_percent": 7.8,
                },
                {"name": "通信设备", "source_type": "hhxg_strong_sector", "rank": 1},
                {"name": "弱势电力", "source_type": "hhxg_weak_sector", "rank": 1},
                {"name": "电子化学品", "source_type": "industry", "rank": 2},
                {"name": "液冷服务器", "source_type": "concept", "rank": 3},
                {"name": "芯片概念", "source_type": "concept", "rank": 2},
            ]
            write_snapshot(root, sectors)
            client = FakeSectorHistoryClient()
            log_path = root / "stock_review.log"

            output_path = collect_sector_kline_evidence_file(
                "2026-08-14",
                "akshare",
                root,
                log_path,
                lookback_bars=5,
                ak_client=client,
            )

            self.assertEqual(output_path, root / "2026-08-14_sector_kline.json")
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["snapshot_file"], "2026-08-14_snapshot.json")
            self.assertEqual(payload["lookback_bars"], 5)
            self.assertEqual(payload["max_objects"], 3)
            self.assertEqual(
                [record["name"] for record in payload["records"]],
                ["液冷服务器", "通信设备", "电子化学品"],
            )
            self.assertEqual(
                client.calls,
                [
                    ("concept_names", {}),
                    ("industry_names", {}),
                    (
                        "concept",
                        {
                            "symbol": "BK1001",
                            "period": "daily",
                            "start_date": "20260630",
                            "end_date": "20260814",
                            "adjust": "",
                        },
                    ),
                    (
                        "industry",
                        {
                            "symbol": "BK2001",
                            "start_date": "20260630",
                            "end_date": "20260814",
                            "period": "日k",
                            "adjust": "",
                        },
                    ),
                    (
                        "industry",
                        {
                            "symbol": "BK2002",
                            "start_date": "20260630",
                            "end_date": "20260814",
                            "period": "日k",
                            "adjust": "",
                        },
                    ),
                ],
            )
            first = payload["records"][0]
            self.assertEqual(first["source_type"], "hhxg_hot_theme")
            self.assertEqual(first["status"], "available")
            self.assertEqual(first["resolved_board_type"], "concept")
            self.assertEqual(first["resolved_board_code"], "BK1001")
            self.assertEqual(first["mapping_basis"], "exact_unique_name")
            self.assertEqual(first["original_ranking_object"]["change_percent"], 7.8)
            self.assertEqual(first["bar_count"], 5)
            self.assertEqual(first["bars"][0]["date"], "2026-08-10")
            self.assertEqual(first["bars"][-1]["date"], "2026-08-14")
            self.assertEqual(
                set(first["bars"][0]),
                {"date", "open", "high", "low", "close", "volume", "amount"},
            )
            self.assertIn("object_count=3", log_path.read_text(encoding="utf-8"))

    def test_zero_or_double_type_matches_are_recorded_without_history_requests(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            write_snapshot(
                root,
                [
                    {"name": "双重命中", "source_type": "hhxg_hot_theme", "rank": 1},
                    {"name": "未命中", "source_type": "hhxg_strong_sector", "rank": 1},
                    {"name": "唯一概念", "source_type": "hhxg_hot_theme", "rank": 2},
                ],
            )
            client = FakeSectorHistoryClient(
                concept_map={"双重命中": "BK1001", "唯一概念": "BK1002"},
                industry_map={"双重命中": "BK2001"},
            )
            log_path = root / "stock_review.log"

            output_path = collect_sector_kline_evidence_file(
                "2026-08-14",
                "akshare",
                root,
                log_path,
                lookback_bars=5,
                ak_client=client,
            )

            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [record["status"] for record in payload["records"]],
                ["ambiguous", "unavailable", "available"],
            )
            self.assertEqual(payload["ambiguous_count"], 1)
            self.assertEqual(payload["unavailable_count"], 1)
            self.assertEqual(payload["available_count"], 1)
            self.assertEqual(
                client.calls,
                [
                    ("concept_names", {}),
                    ("industry_names", {}),
                    (
                        "concept",
                        {
                            "symbol": "BK1002",
                            "period": "daily",
                            "start_date": "20260630",
                            "end_date": "20260814",
                            "adjust": "",
                        },
                    ),
                ],
            )

    def test_duplicate_name_keeps_highest_ranked_original_object(self) -> None:
        selected = select_ranked_sector_objects(
            [
                {"name": "同名板块", "source_type": "hhxg_hot_theme", "rank": 1},
                {"name": "同名板块", "source_type": "industry", "rank": 2},
                {"name": "明确行业", "source_type": "industry", "rank": 3},
            ]
        )

        self.assertEqual([item["name"] for item in selected], ["同名板块", "明确行业"])
        self.assertEqual(selected[0]["source_type"], "hhxg_hot_theme")

    def test_selection_excludes_weak_and_non_positive_rankings(self) -> None:
        selected = select_ranked_sector_objects(
            [
                {"name": "通信技术", "source_type": "hhxg_hot_theme", "rank": 1},
                {"name": "通信设备", "source_type": "hhxg_strong_sector", "rank": 1},
                {"name": "电力", "source_type": "hhxg_weak_sector", "rank": 1},
                {"name": "液冷服务器", "source_type": "hhxg_strong_sector", "rank": 1},
                {"name": "未知榜", "source_type": "other", "rank": 1},
                {
                    "name": "伪正向",
                    "source_type": "industry",
                    "ranking_group": "akshare_industry_weak_rank",
                    "rank": 1,
                },
                {"name": "未来强概念", "source_type": "hhxg_strong_concept", "rank": 1},
            ]
        )

        self.assertEqual(
            [item["name"] for item in selected],
            ["通信技术", "通信设备", "液冷服务器"],
        )

    def test_selection_accepts_ths_industry_positive_ranking(self) -> None:
        selected = select_ranked_sector_objects(
            [{"name": "同花顺行业", "source_type": "industry_ths", "rank": 1}]
        )

        self.assertEqual([item["name"] for item in selected], ["同花顺行业"])

    def test_history_must_end_on_review_date(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            write_snapshot(
                root,
                [{"name": "通信设备", "source_type": "industry", "rank": 1}],
            )
            log_path = root / "stock_review.log"

            with self.assertRaisesRegex(
                IndependentEvidenceCollectionError,
                "必须以复盘日期",
            ):
                collect_sector_kline_evidence_file(
                    "2026-08-14",
                    "akshare",
                    root,
                    log_path,
                    lookback_bars=5,
                    ak_client=FakeSectorHistoryClient(include_review_date=False),
                )

            self.assertFalse((root / "2026-08-14_sector_kline.json").exists())

    def test_lookback_outside_allowed_range_stops_before_source_requests(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            write_snapshot(
                root,
                [{"name": "通信设备", "source_type": "industry", "rank": 1}],
            )
            client = FakeSectorHistoryClient()

            with self.assertRaisesRegex(
                IndependentEvidenceCollectionError,
                "5 至 10",
            ):
                collect_sector_kline_evidence_file(
                    "2026-08-14",
                    "akshare",
                    root,
                    root / "stock_review.log",
                    lookback_bars=4,
                    ak_client=client,
                )

            self.assertEqual(client.calls, [])
            self.assertFalse((root / "2026-08-14_sector_kline.json").exists())

    def test_cli_forwards_bounded_lookback_and_paths(self) -> None:
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            output_path = root / "2026-08-14_sector_kline.json"
            log_path = root / "stock_review.log"
            with patch(
                "stock_review.cli.collect_sector_kline_evidence_file",
                return_value=output_path,
            ) as collector, redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "evidence",
                        "collect-sector-kline",
                        "--date",
                        "2026-08-14",
                        "--source",
                        "akshare",
                        "--output-dir",
                        str(root),
                        "--lookback-bars",
                        "8",
                        "--log-path",
                        str(log_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            collector.assert_called_once_with(
                "2026-08-14",
                "akshare",
                root,
                log_path,
                lookback_bars=8,
            )


def build_daily_rows(include_review_date: bool) -> list[dict[str, object]]:
    dates = DAILY_DATES if include_review_date else DAILY_DATES[:-1]
    return [
        {
            "日期": date.fromisoformat(sample_date),
            "开盘": 100 + position,
            "最高": 102 + position,
            "最低": 99 + position,
            "收盘": 101 + position,
            "成交量": 1000 + position,
            "成交额": 1000000 + position,
        }
        for position, sample_date in enumerate(dates)
    ]


def write_snapshot(root: Path, sectors: list[dict[str, object]]) -> None:
    payload = {
        "trade_date": "2026-08-14",
        "sample_date": "2026-08-14",
        "source": "fixture",
        "sectors": sectors,
    }
    (root / "2026-08-14_snapshot.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
