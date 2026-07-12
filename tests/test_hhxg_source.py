import unittest

from stock_review.evidence.hhxg_source import HhxgSourceError, collect_hhxg_snapshot_evidence


class HhxgSourceTest(unittest.TestCase):
    def test_snapshot_maps_sentiment_and_sector_facts(self):
        payload = {
            "success": True,
            "data": {
                "date": "2026-07-10",
                "market": {"limit_up": 92, "fried": 55, "limit_down": 4, "sentiment_index": 69.6},
                "ladder": {"max_streak": 5},
                "hot_themes": [{"name": "商业航天", "limitup_count": 8, "net_yi": 2.1, "top_stocks": "中信重工"}],
                "sectors": [{"strong": [{"name": "军工", "net_yi": 1.2, "leader": "中信重工", "bias_pct": 3.2}], "weak": []}],
            },
        }

        evidence = collect_hhxg_snapshot_evidence("2026-07-10", request_json=lambda _: payload)

        self.assertEqual(evidence["sentiment"]["limit_up_count"], 92)
        self.assertEqual(evidence["sentiment"]["broken_board_rate"], 0.3741)
        self.assertEqual(evidence["sentiment"]["highest_board"], 5)
        self.assertEqual(evidence["sectors"][0]["name"], "商业航天")
        self.assertNotIn("change_percent", evidence["sectors"][1])

    def test_snapshot_rejects_latest_data_for_requested_history_date(self):
        with self.assertRaisesRegex(HhxgSourceError, "请求日期 2026-07-09，返回日期 2026-07-10"):
            collect_hhxg_snapshot_evidence(
                "2026-07-09",
                request_json=lambda _: {"success": True, "data": {"date": "2026-07-10"}},
            )


if __name__ == "__main__":
    unittest.main()
