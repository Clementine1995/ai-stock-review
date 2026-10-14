# 本文件是决策卡的应用入口：采集（或读回既有快照）→ 规则判定 → 渲染 → 落盘，供 CLI 调用。
# 它只编排本包内的采集、判定与渲染，不写数据库、不触碰正式 ReviewRun 对象链。

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from stock_review.decision.collect_decision_inputs import (
    DEFAULT_LOOKBACK_DAYS,
    collect_decision_inputs,
)
from stock_review.decision.decision_inputs import (
    DecisionInputs,
    DecisionReading,
    IndexDay,
    SentimentDay,
)
from stock_review.decision.read_market_state import read_decision
from stock_review.decision.render_decision_card import render_decision_card
from stock_review.decision.tdx_snapshot import build_tdx_enrichment, load_tdx_enrichment

DEFAULT_DECISION_REPORT_DIR = Path("reports") / "decision"
DEFAULT_DECISION_DATA_DIR = Path("data") / "decision"


# 单次决策卡运行：产出 Markdown 决策卡与同源 JSON 快照，两者内容必须来自同一次判定。
def run_decision_card(
    trade_date: str,
    *,
    report_dir: Path = DEFAULT_DECISION_REPORT_DIR,
    data_dir: Path = DEFAULT_DECISION_DATA_DIR,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ak_client: Any | None = None,
    tdx_snapshot_dir: Path | None = None,
    write: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    tdx_enrichment = load_tdx_enrichment(
        trade_date, tdx_snapshot_dir or (data_dir / "tdx")
    )
    inputs = collect_decision_inputs(
        trade_date,
        lookback_days=lookback_days,
        ak_client=ak_client,
        tdx_enrichment=tdx_enrichment,
    )
    return _finalize(
        inputs,
        read_decision(inputs),
        generated_at=generated_at,
        report_dir=report_dir,
        data_dir=data_dir,
        write=write,
    )


# 从既有 JSON 快照重渲染：不重新采集任何外部数据，只复用当日已冻结的事实重跑规则与渲染。
# 用途是在规则/渲染变更后回看已归档交易日「按当前规则会长什么样」，不改变该日已采集的事实。
def run_decision_card_from_snapshot(
    snapshot_path: Path,
    *,
    report_dir: Path = DEFAULT_DECISION_REPORT_DIR,
    data_dir: Path = DEFAULT_DECISION_DATA_DIR,
    write: bool = True,
    generated_at: str | None = None,
) -> dict[str, Any]:
    payload = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    inputs = inputs_from_snapshot(payload)
    # 缺省沿用快照记录的生成时间，使重渲染产物与归档卡只在规则相关部分产生差异，便于直接 diff。
    stamp = generated_at or _text(payload.get("generated_at"))
    if not stamp:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    return _finalize(
        inputs,
        read_decision(inputs),
        generated_at=stamp,
        report_dir=report_dir,
        data_dir=data_dir,
        write=write,
    )


# 从决策卡 JSON 快照还原输入事实；只接受本包写出的结构，字段缺失时如实报错而不是补默认值。
def inputs_from_snapshot(payload: dict[str, Any]) -> DecisionInputs:
    raw = payload.get("inputs")
    if not isinstance(raw, dict):
        raise ValueError("快照缺少 inputs 节点，无法从快照重渲染。")
    trade_date = _text(raw.get("trade_date"))
    if not trade_date:
        raise ValueError("快照 inputs 缺少 trade_date。")

    tdx_raw = raw.get("tdx")
    return DecisionInputs(
        trade_date=trade_date,
        indices={str(key): dict(value) for key, value in (raw.get("indices") or {}).items()},
        total_amount=_optional_float(raw.get("total_amount")),
        total_amount_source=_text(raw.get("total_amount_source")),
        breadth=_breadth(raw.get("breadth")),
        sentiment_today=_sentiment_day(raw.get("sentiment_today")),
        sentiment_history=tuple(
            day for day in (_sentiment_day(item) for item in raw.get("sentiment_history") or []) if day
        ),
        amount_history=tuple(
            (str(item[0]), _optional_float(item[1]))
            for item in raw.get("amount_history") or []
            if isinstance(item, (list, tuple)) and len(item) == 2
        ),
        index_history={
            str(symbol): tuple(
                day for day in (_index_day(item) for item in days or []) if day
            )
            for symbol, days in (raw.get("index_history") or {}).items()
        },
        sectors=tuple(dict(item) for item in raw.get("sectors") or []),
        limit_up_stocks=tuple(dict(item) for item in raw.get("limit_up_stocks") or []),
        broken_board_stocks=tuple(dict(item) for item in raw.get("broken_board_stocks") or []),
        sources={str(key): str(value) for key, value in (raw.get("sources") or {}).items()},
        gaps=tuple(str(item) for item in raw.get("gaps") or []),
        tdx=build_tdx_enrichment(tdx_raw) if isinstance(tdx_raw, dict) else None,
    )


# 结果装配与落盘：Markdown 与 JSON 快照必须来自同一次判定，避免两者出现不一致。
def _finalize(
    inputs: DecisionInputs,
    reading: DecisionReading,
    *,
    generated_at: str,
    report_dir: Path,
    data_dir: Path,
    write: bool,
) -> dict[str, Any]:
    markdown = render_decision_card(inputs, reading, generated_at=generated_at)
    result: dict[str, Any] = {
        "trade_date": inputs.trade_date,
        "generated_at": generated_at,
        "report_path": None,
        "data_path": None,
        "tdx_enriched": inputs.tdx is not None,
        "answers": {
            "sentiment": reading.sentiment_stage.get("label"),
            "sentiment_temperature": reading.sentiment_stage.get("temperature"),
            "divergence_quality": reading.divergence_quality.get("label"),
            "market_stage": reading.market_stage.get("label"),
            "stabilization": (reading.market_stage.get("stabilization") or {}).get("label"),
            "amount_band": reading.amount_band.get("label"),
            "participation": reading.participation.get("label"),
            "position_range": list(reading.position_band.get("total_range") or (0, 0)),
            "main_line": reading.sector_leadership.get("label"),
            "candidate_count": len(reading.candidates.get("board_ladder") or []),
        },
        "gaps": list(inputs.gaps),
    }

    if write:
        report_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{inputs.trade_date}.md"
        data_path = data_dir / f"{inputs.trade_date}.json"
        report_path.write_text(markdown, encoding="utf-8")
        # JSON 快照保留值与缺口，便于跨日比较情绪序列和复盘规则命中情况。
        data_path.write_text(
            json.dumps(
                {
                    "inputs": _serialize(inputs),
                    "reading": _serialize(reading),
                    "generated_at": generated_at,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        result["report_path"] = str(report_path)
        result["data_path"] = str(data_path)

    result["markdown"] = markdown
    return result


# 涨跌家数面板字段原样读回；仅把活跃度从历史快照的字符串形态还原为数值。
# 早期快照把活跃度（Decimal）经 JSON 序列化退化成了字符串，读回时若原样保留，渲染会按字符串处理而失真。
def _breadth(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    breadth = dict(raw)
    if isinstance(breadth.get("activity_percent"), str):
        breadth["activity_percent"] = _optional_float(breadth["activity_percent"])
    return breadth


def _sentiment_day(raw: Any) -> SentimentDay | None:
    if not isinstance(raw, dict):
        return None
    trade_date = _text(raw.get("trade_date"))
    limit_up = _int_or_none(raw.get("limit_up_count"))
    limit_down = _int_or_none(raw.get("limit_down_count"))
    broken = _int_or_none(raw.get("broken_board_count"))
    # 三个家数字段是判定必需项：缺失即视为该日不可用，不用 0 冒充。
    if not trade_date or limit_up is None or limit_down is None or broken is None:
        return None
    return SentimentDay(
        trade_date=trade_date,
        limit_up_count=limit_up,
        limit_down_count=limit_down,
        broken_board_count=broken,
        broken_board_rate=_optional_float(raw.get("broken_board_rate")),
        highest_board=_int_or_none(raw.get("highest_board")),
    )


def _index_day(raw: Any) -> IndexDay | None:
    if not isinstance(raw, dict):
        return None
    trade_date = _text(raw.get("trade_date"))
    close = _optional_float(raw.get("close"))
    if not trade_date or close is None:
        return None
    return IndexDay(
        trade_date=trade_date,
        close=close,
        open=_optional_float(raw.get("open")),
        high=_optional_float(raw.get("high")),
        low=_optional_float(raw.get("low")),
        amount=_optional_float(raw.get("amount")),
    )


def _serialize(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
