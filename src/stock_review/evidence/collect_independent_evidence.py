# 本文件负责把有限候选龙虎榜、排行板块日 K 和美股窗口事实写成独立证据文件，不写 Snapshot、SQLite 或报告。

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from stock_review.evidence.akshare_longhubang_source import (
    collect_akshare_daily_longhubang_evidence,
)
from stock_review.evidence.akshare_us_market_source import (
    collect_akshare_us_market_window_evidence,
)
from stock_review.evidence.akshare_sector_kline_source import (
    SectorKlineSourceError,
    collect_akshare_sector_kline_evidence,
    select_ranked_sector_objects,
)
from stock_review.evidence.longhubang_source import (
    LonghubangSourceError,
    normalize_candidates,
)
from stock_review.evidence.us_market_session_source import UsMarketSessionSourceError


DEFAULT_INDEPENDENT_EVIDENCE_DIR = Path("data") / "evidence"
DEFAULT_INDEPENDENT_EVIDENCE_LOG_PATH = Path("logs") / "stock_review.log"
SUPPORTED_INDEPENDENT_SOURCE = "akshare"


class IndependentEvidenceCollectionError(RuntimeError):
    pass


# 龙虎榜只从同目录、同日期的复盘候选历史文件读取有限代码，输出独立 JSON。
def collect_longhubang_evidence_file(
    trade_date: str,
    source: str,
    output_dir: Path = DEFAULT_INDEPENDENT_EVIDENCE_DIR,
    log_path: Path = DEFAULT_INDEPENDENT_EVIDENCE_LOG_PATH,
    *,
    ak_client: Any | None = None,
) -> Path:
    validate_trade_date(trade_date)
    validate_source(source)
    candidate_path = output_dir / f"{trade_date}_review_candidate_history.json"
    output_path = output_dir / f"{trade_date}_longhubang.json"
    try:
        candidates = read_review_candidates(candidate_path, trade_date)
        payload = collect_akshare_daily_longhubang_evidence(
            trade_date,
            candidates,
            ak_client,
        )
    except (IndependentEvidenceCollectionError, LonghubangSourceError) as error:
        write_independent_evidence_log(
            command="evidence collect-longhubang",
            trade_date=trade_date,
            source=source,
            output_path=output_path,
            log_path=log_path,
            details=f"candidate_history={candidate_path} error={error}",
            status="failed",
        )
        if isinstance(error, IndependentEvidenceCollectionError):
            raise
        raise IndependentEvidenceCollectionError(str(error)) from error

    payload["candidate_history_file"] = candidate_path.name
    persist_independent_evidence(
        command="evidence collect-longhubang",
        trade_date=trade_date,
        source=source,
        output_path=output_path,
        log_path=log_path,
        payload=payload,
        details=(
            f"candidate_history={candidate_path} "
            f"candidate_count={payload['candidate_count']} "
            f"listed_count={payload['consumed_listed_count']}"
        ),
    )
    return output_path


# 美股证据要求调用方显式给出窗口和截止时点；下一 A 股交易日不在本模块内推算。
def collect_us_market_evidence_file(
    trade_date: str,
    source: str,
    output_dir: Path = DEFAULT_INDEPENDENT_EVIDENCE_DIR,
    log_path: Path = DEFAULT_INDEPENDENT_EVIDENCE_LOG_PATH,
    *,
    as_of: str,
    a_share_close_at: str,
    next_a_share_open_at: str,
    generated_at: str | datetime | None = None,
    ak_client: Any | None = None,
) -> Path:
    validate_trade_date(trade_date)
    validate_source(source)
    effective_generated_at = generated_at or datetime.now(timezone.utc)
    output_path = output_dir / f"{trade_date}_us_market.json"
    try:
        payload = collect_akshare_us_market_window_evidence(
            trade_date,
            effective_generated_at,
            as_of,
            a_share_close_at,
            next_a_share_open_at,
            ak_client,
        )
    except UsMarketSessionSourceError as error:
        write_independent_evidence_log(
            command="evidence collect-us-market",
            trade_date=trade_date,
            source=source,
            output_path=output_path,
            log_path=log_path,
            details=(
                f"as_of={as_of} a_share_close_at={a_share_close_at} "
                f"next_a_share_open_at={next_a_share_open_at} error={error}"
            ),
            status="failed",
        )
        raise IndependentEvidenceCollectionError(str(error)) from error

    persist_independent_evidence(
        command="evidence collect-us-market",
        trade_date=trade_date,
        source=source,
        output_path=output_path,
        log_path=log_path,
        payload=payload,
        details=(
            f"as_of={payload['as_of']} "
            f"a_share_close_at={payload['window']['a_share_close_at']} "
            f"next_a_share_open_at={payload['window']['next_a_share_open_at']} "
            f"us_session_date={payload['us_session_date'] or 'none'} "
            f"evidence_status={payload['status']}"
        ),
    )
    return output_path


# 板块日 K 只读取同目录同日 Snapshot 的正向排行对象，最多请求三个精确名称。
def collect_sector_kline_evidence_file(
    trade_date: str,
    source: str,
    output_dir: Path = DEFAULT_INDEPENDENT_EVIDENCE_DIR,
    log_path: Path = DEFAULT_INDEPENDENT_EVIDENCE_LOG_PATH,
    *,
    lookback_bars: int,
    ak_client: Any | None = None,
) -> Path:
    validate_trade_date(trade_date)
    validate_source(source)
    snapshot_path = output_dir / f"{trade_date}_snapshot.json"
    output_path = output_dir / f"{trade_date}_sector_kline.json"
    try:
        sectors = read_snapshot_ranked_sectors(snapshot_path, trade_date)
        ranked_objects = select_ranked_sector_objects(sectors)
        if not ranked_objects:
            raise IndependentEvidenceCollectionError(
                "同日 Snapshot 没有同时包含有效 name、rank 和正向来源类型的排行对象。"
            )
        payload = collect_akshare_sector_kline_evidence(
            trade_date,
            ranked_objects,
            lookback_bars,
            ak_client,
        )
    except (IndependentEvidenceCollectionError, SectorKlineSourceError) as error:
        write_independent_evidence_log(
            command="evidence collect-sector-kline",
            trade_date=trade_date,
            source=source,
            output_path=output_path,
            log_path=log_path,
            details=(
                f"snapshot={snapshot_path} lookback_bars={lookback_bars} error={error}"
            ),
            status="failed",
        )
        if isinstance(error, IndependentEvidenceCollectionError):
            raise
        raise IndependentEvidenceCollectionError(str(error)) from error

    payload["snapshot_file"] = snapshot_path.name
    persist_independent_evidence(
        command="evidence collect-sector-kline",
        trade_date=trade_date,
        source=source,
        output_path=output_path,
        log_path=log_path,
        payload=payload,
        details=(
            f"snapshot={snapshot_path} lookback_bars={lookback_bars} "
            f"object_count={len(payload['records'])} available_count={payload['available_count']}"
        ),
    )
    return output_path


def read_review_candidates(candidate_path: Path, trade_date: str) -> list[dict[str, str]]:
    if not candidate_path.exists():
        raise IndependentEvidenceCollectionError(f"复盘候选历史文件不存在：{candidate_path}")
    try:
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IndependentEvidenceCollectionError(
            f"复盘候选历史文件读取失败：{candidate_path}：{error}"
        ) from error
    if not isinstance(payload, dict):
        raise IndependentEvidenceCollectionError("复盘候选历史文件顶层必须是 JSON 对象。")
    if payload.get("trade_date") != trade_date or payload.get("sample_date") != trade_date:
        raise IndependentEvidenceCollectionError("复盘候选历史文件日期与目标日期不一致。")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise IndependentEvidenceCollectionError("复盘候选历史文件必须包含非空 records 数组。")
    try:
        candidates = normalize_candidates(
            [
                {"code": record.get("code"), "name": record.get("name")}
                if isinstance(record, dict)
                else record
                for record in records
            ]
        )
    except LonghubangSourceError as error:
        raise IndependentEvidenceCollectionError(str(error)) from error
    if len(candidates) != len(records):
        raise IndependentEvidenceCollectionError("复盘候选历史文件包含重复候选代码。")
    return candidates


def read_snapshot_ranked_sectors(snapshot_path: Path, trade_date: str) -> list[Any]:
    if not snapshot_path.exists():
        raise IndependentEvidenceCollectionError(f"同日 Snapshot 不存在：{snapshot_path}")
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IndependentEvidenceCollectionError(
            f"同日 Snapshot 读取失败：{snapshot_path}：{error}"
        ) from error
    if not isinstance(payload, dict):
        raise IndependentEvidenceCollectionError("同日 Snapshot 顶层必须是 JSON 对象。")
    if payload.get("trade_date") != trade_date or payload.get("sample_date") != trade_date:
        raise IndependentEvidenceCollectionError("同日 Snapshot 日期与目标日期不一致。")
    sectors = payload.get("sectors")
    if not isinstance(sectors, list) or not sectors:
        raise IndependentEvidenceCollectionError("同日 Snapshot 必须包含非空 sectors 数组。")
    return sectors


def validate_trade_date(trade_date: str) -> None:
    try:
        date.fromisoformat(trade_date)
    except (TypeError, ValueError) as error:
        raise IndependentEvidenceCollectionError("交易日期必须是 YYYY-MM-DD。") from error


def validate_source(source: str) -> None:
    if source != SUPPORTED_INDEPENDENT_SOURCE:
        raise IndependentEvidenceCollectionError("独立证据当前仅支持 source=akshare。")


def write_json_evidence(output_path: Path, payload: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        # 临时文件与目标文件位于同一目录，完整写入后再原子替换，失败时不破坏旧证据。
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        temporary_path.replace(output_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


# 三个独立证据命令共用同一落盘口径；目录创建或 JSON 写入失败也必须留下失败状态。
def persist_independent_evidence(
    *,
    command: str,
    trade_date: str,
    source: str,
    output_path: Path,
    log_path: Path,
    payload: dict[str, Any],
    details: str,
) -> None:
    try:
        write_json_evidence(output_path, payload)
    except OSError as error:
        write_independent_evidence_log(
            command=command,
            trade_date=trade_date,
            source=source,
            output_path=output_path,
            log_path=log_path,
            details=f"{details} error={error}",
            status="failed",
        )
        raise IndependentEvidenceCollectionError(
            f"独立证据文件写入失败：{output_path}：{error}"
        ) from error
    write_independent_evidence_log(
        command=command,
        trade_date=trade_date,
        source=source,
        output_path=output_path,
        log_path=log_path,
        details=details,
    )


def write_independent_evidence_log(
    *,
    command: str,
    trade_date: str,
    source: str,
    output_path: Path,
    log_path: Path,
    details: str,
    status: str = "created",
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stock_review.evidence.independent")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(
            "command=%s trade_date=%s source=%s output=%s %s status=%s",
            command,
            trade_date,
            source,
            output_path,
            details,
            status,
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
