from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from stock_review.pools.build_pool_candidates import build_pool_candidate_summary, render_pool_candidate_summary


class PoolCandidateTest(unittest.TestCase):
    def test_candidates_separate_confirmed_codes_from_unverified_stock_facts(self):
        with TemporaryDirectory() as temp_path:
            root = Path(temp_path)
            self.write_snapshot(root)

            summary = build_pool_candidate_summary("2026-07-10", root)
            content = render_pool_candidate_summary(summary)

            self.assertEqual([(item.code, item.name) for item in summary.verified_stock_candidates], [("002414", "高德红外")])
            self.assertEqual(summary.unverified_stock_facts[0].name, "航天环宇")
            self.assertIn("军工", content)
            self.assertIn("不自动写入关注池/热点池", content)

    @staticmethod
    def write_snapshot(root: Path) -> None:
        payload = {
            "trade_date": "2026-07-10",
            "source": "hhxg",
            "sample_date": "2026-07-10",
            "market": {},
            "sentiment": {},
            "sectors": [{"name": "军工", "source_type": "hhxg_hot_theme", "rank": 1, "limit_up_count": 8, "net_inflow_yi": 2, "leading_stock": "高德红外"}],
            "stocks": [
                {"code": "002414", "name": "高德红外", "exchange": "待确认", "sector": "军工电子", "role": "连板事实候选", "source": "akshare:stock_zt_pool_em"},
                {"code": "待确认", "name": "航天环宇", "sector": "军工", "role": "板块领涨事实候选", "source": "akshare:sector_leading_stock"},
            ],
            "field_sources": {"stocks": "akshare", "sectors": "hhxg"},
            "events": [],
        }
        (root / "2026-07-10_snapshot.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
