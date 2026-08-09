# 本文件负责采集真实池个股的有限历史日线事实，不扫描全市场或生成交易结论。

from __future__ import annotations

from datetime import date, timedelta
import json
import logging
from pathlib import Path
from typing import Any

from stock_review.evidence.akshare_source import AkshareRequestPacer, AkshareSourceError, import_akshare_client
from stock_review.evidence.evidence_snapshot import build_evidence_snapshot
from stock_review.evidence.manage_evidence_snapshot import read_json_mapping
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
        try:
            rows, source, source_note = fetch_pool_history_rows(client, item.code, start_date, end_date, pacer)
        except AkshareSourceError as error:
            records.append(
                {
                    "code": item.code,
                    "name": item.name,
                    "error": str(error),
                    "missing_fields": ["history_unavailable"],
                }
            )
            continue
        record = build_history_record(item.code, item.name, item.sectors, target_date, rows)
        record["source"] = source
        if source_note:
            record["source_note"] = source_note
        records.append(record)

    payload = {
        "trade_date": trade_date,
        "source": "akshare:pool_stock_history",
        "sample_date": trade_date,
        "scope": "real_pool_stocks",
        "records": records,
        "note": "仅覆盖真实池记录；主日线失败时可降级到 AKShare 的腾讯日线。该备用源的 amount 单位为手，只写入成交量，不伪装成成交额；5/20 日字段不足时保留待确认，不认定核心票或买点。",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{trade_date}_real_pool_stock_history.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_collection_log("evidence collect-pool-history", trade_date, output_path, len(records), log_path)
    return output_path


# 复盘候选历史只合并同日快照的可确认代码和有效真实池；不从全市场额外选股。
def collect_review_candidate_history(
    trade_date: str,
    snapshot_dir: Path = DEFAULT_EVIDENCE_DIR,
    output_dir: Path = DEFAULT_EVIDENCE_DIR,
    database_path: Path = DEFAULT_DATABASE_PATH,
    ak_client: Any | None = None,
    log_path: Path = DEFAULT_LOG_PATH,
) -> Path:
    target_date = date.fromisoformat(trade_date)
    snapshot_path = snapshot_dir / f"{trade_date}_snapshot.json"
    if not snapshot_path.exists():
        raise AkshareSourceError(f"证据快照不存在：{snapshot_path}")
    snapshot = build_evidence_snapshot(trade_date, read_json_mapping(snapshot_path))
    if snapshot.sample_date != trade_date:
        raise AkshareSourceError("证据快照样本日期与复盘日期不一致。")

    candidates: dict[str, dict[str, Any]] = {}
    for stock in snapshot.stocks:
        code = str(stock.get("code") or "").strip()
        name = str(stock.get("name") or "").strip()
        if code.isdigit() and len(code) == 6 and name:
            candidates[code] = {
                "code": code,
                "name": name,
                "sectors": (str(stock.get("sector") or "待确认"),),
                "origin": "system_snapshot",
            }
    for item in list_pool_items(record_kind="real", database_path=database_path):
        existing = candidates.get(item.code)
        candidates[item.code] = {
            "code": item.code,
            "name": item.name,
            "sectors": item.sectors or tuple(existing["sectors"] if existing else ()),
            "origin": "system_snapshot_and_real_pool" if existing else "real_pool",
        }
    if not candidates:
        raise AkshareSourceError("当前快照和真实池都没有可确认代码，无法采集复盘候选历史事实。")

    client = ak_client or import_akshare_client()
    start_date = (target_date - timedelta(days=45)).strftime("%Y%m%d")
    end_date = target_date.strftime("%Y%m%d")
    pacer = AkshareRequestPacer()
    records: list[dict[str, Any]] = []
    for candidate in candidates.values():
        try:
            rows, source, source_note = fetch_pool_history_rows(
                client, candidate["code"], start_date, end_date, pacer
            )
        except AkshareSourceError as error:
            records.append(
                {
                    "code": candidate["code"],
                    "name": candidate["name"],
                    "origin": candidate["origin"],
                    "error": str(error),
                    "missing_fields": ["history_unavailable"],
                }
            )
            continue
        record = build_history_record(
            candidate["code"], candidate["name"], tuple(candidate["sectors"]), target_date, rows
        )
        record["origin"] = candidate["origin"]
        record["source"] = source
        if source_note:
            record["source_note"] = source_note
        records.append(record)

    payload = {
        "trade_date": trade_date,
        "source": "akshare:review_candidate_history",
        "sample_date": trade_date,
        "scope": "snapshot_and_real_pool_candidates",
        "records": records,
        "note": "仅覆盖同日快照与有效真实池的有限候选；流通市值按收盘价乘流通股本计算，成交容量使用候选自身成交额，不扩展为全市场选股。",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{trade_date}_review_candidate_history.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_collection_log("evidence collect-review-history", trade_date, output_path, len(records), log_path)
    return output_path


def fetch_pool_history_rows(
    client: Any,
    code: str,
    start_date: str,
    end_date: str,
    pacer: AkshareRequestPacer,
) -> tuple[list[dict[str, Any]], str, str]:
    try:
        pacer.wait_before_request()
        frame = client.stock_zh_a_daily(
            symbol=to_tencent_symbol(code),
            start_date=start_date,
            end_date=end_date,
            adjust="",
        )
    except Exception:  # noqa: BLE001
        frame = None
    if normalize_history_rows(frame):
        return normalize_history_rows(frame), "akshare:stock_zh_a_daily", ""

    try:
        pacer.wait_before_request()
        frame = client.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="",
        )
    except Exception as primary_error:  # noqa: BLE001
        try:
            pacer.wait_before_request()
            fallback_frame = client.stock_zh_a_hist_tx(
                symbol=to_tencent_symbol(code),
                start_date=start_date,
                end_date=end_date,
                adjust="",
            )
        except Exception as fallback_error:  # noqa: BLE001
            raise AkshareSourceError(
                f"个股日线采集失败：primary={primary_error}；fallback={fallback_error}"
            ) from fallback_error
        return normalize_tencent_history_rows(fallback_frame), "akshare:stock_zh_a_hist_tx", "主日线接口失败，已使用腾讯日线备用路径。"
    return normalize_history_rows(frame), "akshare:stock_zh_a_hist", ""


# 腾讯日线要求带交易所前缀；范围只覆盖沪深京 A 股代码，不接受无法确定交易所的代码。
def to_tencent_symbol(code: str) -> str:
    if code.startswith(("6",)):
        return f"sh{code}"
    if code.startswith(("0", "2", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    raise AkshareSourceError(f"无法确定腾讯日线交易所前缀：{code}")


def normalize_history_rows(frame: Any) -> list[dict[str, Any]]:
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else frame if isinstance(frame, list) else []
    return [row for row in rows if isinstance(row, dict)]


# 腾讯日线的 amount 单位是手，只能映射为成交量；成交额字段必须保留为空并由缺口标识。
def normalize_tencent_history_rows(frame: Any) -> list[dict[str, Any]]:
    normalized_rows: list[dict[str, Any]] = []
    for row in normalize_history_rows(frame):
        normalized_rows.append(
            {
                "date": row.get("date"),
                "open": row.get("open"),
                "close": row.get("close"),
                "high": row.get("high"),
                "low": row.get("low"),
                "volume": row.get("amount"),
            }
        )
    return normalized_rows


def build_history_record(code: str, name: str, sectors: tuple[str, ...], target_date: date, frame: Any) -> dict[str, Any]:
    rows = normalize_history_rows(frame)
    normalized_rows = [row for row in rows if isinstance(row, dict) and parse_row_date(row) is not None and parse_row_date(row) <= target_date]
    normalized_rows.sort(key=parse_row_date)
    latest = normalized_rows[-1] if normalized_rows else {}
    close = number_value(latest.get("收盘", latest.get("close")))
    record = {
        "code": code,
        "name": name,
        "sectors": list(sectors),
        "sample_date": parse_row_date(latest).isoformat() if latest else None,
        "close": close,
        "change_percent": number_value(latest.get("涨跌幅", latest.get("change_percent"))),
        "volume": number_value(latest.get("成交量", latest.get("volume"))),
        "amount": number_value(latest.get("成交额", latest.get("amount"))),
        "average_5d_amount": window_average(normalized_rows, 5, "成交额", "amount"),
        "outstanding_share": number_value(latest.get("outstanding_share")),
        "circulating_market_value": (
            round(close * number_value(latest.get("outstanding_share")), 2)
            if close is not None and number_value(latest.get("outstanding_share")) is not None
            else None
        ),
        "turnover": number_value(latest.get("换手率", latest.get("turnover"))),
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
    if record["volume"] is None:
        record["missing_fields"].append("missing_latest_volume")
    if record["amount"] is None:
        record["missing_fields"].append("missing_latest_amount")
    if record["average_5d_amount"] is None:
        record["missing_fields"].append("missing_5d_average_amount")
    if record["circulating_market_value"] is None:
        record["missing_fields"].append("missing_circulating_market_value")
    return record


def window_return(rows: list[dict[str, Any]], window: int) -> float | None:
    if len(rows) < window:
        return None
    start = number_value(rows[-window].get("收盘", rows[-window].get("close")))
    end = number_value(rows[-1].get("收盘", rows[-1].get("close")))
    if start in (None, 0) or end is None:
        return None
    return round((end / start - 1) * 100, 4)


def window_average(
    rows: list[dict[str, Any]], window: int, primary_field: str, fallback_field: str
) -> float | None:
    if len(rows) < window:
        return None
    values = [number_value(row.get(primary_field, row.get(fallback_field))) for row in rows[-window:]]
    if any(value is None for value in values):
        return None
    return round(sum(value for value in values if value is not None) / window, 2)


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


def write_collection_log(command: str, trade_date: str, output_path: Path, item_count: int, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.pool_stock_history")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info("command=%s trade_date=%s output=%s item_count=%s status=created", command, trade_date, output_path, item_count)
    finally:
        logger.removeHandler(handler)
        handler.close()
