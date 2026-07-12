# 本文件负责采集真实池个股的有限历史日线事实，不扫描全市场或生成交易结论。

from __future__ import annotations

from datetime import date, timedelta
import json
import logging
from pathlib import Path
from typing import Any

from stock_review.evidence.akshare_source import AkshareRequestPacer, AkshareSourceError, import_akshare_client
from stock_review.pools.manage_pool_item import DEFAULT_DATABASE_PATH, list_pool_items


DEFAULT_EVIDENCE_DIR = Path("data") / "evidence"
DEFAULT_LOG_PATH = Path("logs") / "stock_review.log"


# 真实池个股只请求约 45 个自然日，以覆盖最近 20 个交易日且避免扩大请求范围。
def collect_real_pool_stock_history(
    trade_date: str,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
    ak_client: Any | None = None,
    log_path: Path = DEFAULT_LOG_PATH,
) -> Path:
    target_date = date.fromisoformat(trade_date)
    pool_items = list_pool_items(record_kind="real", database_path=database_path)
    if not pool_items:
        raise AkshareSourceError("当前没有真实池记录，无法采集个股历史事实。")

    client = ak_client or import_akshare_client()
    start_date = (target_date - timedelta(days=45)).strftime("%Y%m%d")
    end_date = target_date.strftime("%Y%m%d")
    pacer = AkshareRequestPacer()
    records: list[dict[str, Any]] = []
    for item in pool_items:
        pacer.wait_before_request()
        try:
            frame = client.stock_zh_a_hist(
                symbol=item.code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="",
            )
        except Exception as error:  # noqa: BLE001
            records.append({"code": item.code, "name": item.name, "error": str(error), "missing_fields": ["history_unavailable"]})
            continue
        records.append(build_history_record(item.code, item.name, item.sector, target_date, frame))

    payload = {
        "trade_date": trade_date,
        "source": "akshare:stock_zh_a_hist",
        "sample_date": trade_date,
        "scope": "real_pool_stocks",
        "records": records,
        "note": "仅覆盖真实池记录；5/20 日字段不足时保留待确认，不认定核心票或买点。",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{trade_date}_real_pool_stock_history.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_collection_log(trade_date, output_path, len(records), log_path)
    return output_path


def build_history_record(code: str, name: str, sector: str, target_date: date, frame: Any) -> dict[str, Any]:
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else frame if isinstance(frame, list) else []
    normalized_rows = [row for row in rows if isinstance(row, dict) and parse_row_date(row) is not None and parse_row_date(row) <= target_date]
    normalized_rows.sort(key=parse_row_date)
    latest = normalized_rows[-1] if normalized_rows else {}
    close = number_value(latest.get("收盘", latest.get("close")))
    record = {
        "code": code,
        "name": name,
        "sector": sector,
        "sample_date": parse_row_date(latest).isoformat() if latest else None,
        "close": close,
        "change_percent": number_value(latest.get("涨跌幅", latest.get("change_percent"))),
        "volume": number_value(latest.get("成交量", latest.get("volume"))),
        "amount": number_value(latest.get("成交额", latest.get("amount"))),
        "history_days": len(normalized_rows),
        "return_5d_percent": window_return(normalized_rows, 5),
        "return_20d_percent": window_return(normalized_rows, 20),
        "missing_fields": [],
    }
    if len(normalized_rows) < 5:
        record["missing_fields"].append("missing_5d_history")
    if len(normalized_rows) < 20:
        record["missing_fields"].append("missing_20d_history")
    if close is None:
        record["missing_fields"].append("missing_latest_close")
    return record


def window_return(rows: list[dict[str, Any]], window: int) -> float | None:
    if len(rows) < window:
        return None
    start = number_value(rows[-window].get("收盘", rows[-window].get("close")))
    end = number_value(rows[-1].get("收盘", rows[-1].get("close")))
    if start in (None, 0) or end is None:
        return None
    return round((end / start - 1) * 100, 4)


def parse_row_date(row: dict[str, Any]) -> date | None:
    value = row.get("日期", row.get("date"))
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def number_value(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def write_collection_log(trade_date: str, output_path: Path, item_count: int, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.pool_stock_history")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info("command=evidence collect-pool-history trade_date=%s output=%s item_count=%s status=created", trade_date, output_path, item_count)
    finally:
        logger.removeHandler(handler)
        handler.close()
