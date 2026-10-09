# 本文件是决策卡的应用入口：采集 → 规则判定 → 渲染 → 落盘，供 CLI 调用。
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
from stock_review.decision.read_market_state import read_decision
from stock_review.decision.render_decision_card import render_decision_card
from stock_review.decision.tdx_snapshot import load_tdx_enrichment

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
    reading = read_decision(inputs)
    markdown = render_decision_card(inputs, reading, generated_at=generated_at)

    result: dict[str, Any] = {
        "trade_date": trade_date,
        "generated_at": generated_at,
        "report_path": None,
        "data_path": None,
        "tdx_enriched": tdx_enrichment is not None,
        "answers": {
            "sentiment": reading.sentiment_stage.get("label"),
            "sentiment_temperature": reading.sentiment_stage.get("temperature"),
            "market_stage": reading.market_stage.get("label"),
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
        report_path = report_dir / f"{trade_date}.md"
        data_path = data_dir / f"{trade_date}.json"
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


def _serialize(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value
