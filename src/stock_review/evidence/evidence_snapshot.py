# 本文件定义 Evidence Snapshot 的最小结构和缺口识别规则，作为后续复盘证据输入边界。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


REQUIRED_SENTIMENT_FIELDS = (
    "limit_up_count",
    "limit_down_count",
    "broken_board_rate",
    "highest_board",
)


@dataclass(frozen=True)
class EvidenceSnapshot:
    trade_date: str
    source: str
    sample_date: str
    market: dict[str, Any]
    sentiment: dict[str, Any]
    sectors: list[dict[str, Any]]
    stocks: list[dict[str, Any]]
    events: list[dict[str, Any]]
    missing_fields: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "source": self.source,
            "sample_date": self.sample_date,
            "market": self.market,
            "sentiment": self.sentiment,
            "sectors": self.sectors,
            "stocks": self.stocks,
            "events": self.events,
            "missing_fields": list(self.missing_fields),
        }


# 导入时以交易日期为主键口径，样本日期必须保留，避免把不同日期的数据误当作当天事实。
def build_evidence_snapshot(trade_date: str, raw_data: dict[str, Any]) -> EvidenceSnapshot:
    source = str(raw_data.get("source") or "manual")
    sample_date = str(raw_data.get("sample_date") or trade_date)
    market = as_mapping(raw_data.get("market"))
    sentiment = as_mapping(raw_data.get("sentiment"))
    sectors = as_list_of_mappings(raw_data.get("sectors"))
    stocks = as_list_of_mappings(raw_data.get("stocks"))
    events = as_list_of_mappings(raw_data.get("events"))
    missing_fields = tuple(
        identify_missing_fields(
            market=market,
            sentiment=sentiment,
            sectors=sectors,
            stocks=stocks,
        )
    )

    return EvidenceSnapshot(
        trade_date=trade_date,
        source=source,
        sample_date=sample_date,
        market=market,
        sentiment=sentiment,
        sectors=sectors,
        stocks=stocks,
        events=events,
        missing_fields=missing_fields,
    )


# 缺口名称稳定输出，后续报告和测试可以直接定位缺失证据类型。
def identify_missing_fields(
    market: dict[str, Any],
    sentiment: dict[str, Any],
    sectors: list[dict[str, Any]],
    stocks: list[dict[str, Any]],
) -> list[str]:
    missing_fields: list[str] = []

    if not market.get("indices"):
        missing_fields.append("missing_indices")
    if market.get("total_amount") in (None, ""):
        missing_fields.append("missing_total_amount")
    if not sentiment or any(sentiment.get(field) in (None, "") for field in REQUIRED_SENTIMENT_FIELDS):
        missing_fields.append("missing_sentiment")
    if not sectors:
        missing_fields.append("missing_sectors")
    if not stocks:
        missing_fields.append("missing_stocks")

    return missing_fields


def as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def as_list_of_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
