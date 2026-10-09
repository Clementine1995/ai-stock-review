# 本文件把通达信连接器（MCP）采集到的补充证据读成决策层可用的值对象。
# 采集通道与运行通道相互独立：Agent 先把通达信事实落成 JSON 快照，本文件只负责读取与校验。
# 快照缺失或字段非法时一律返回 None 或跳过该条，绝不用默认值或臆造数据冒充通达信证据。

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from stock_review.decision.decision_inputs import (
    TdxBoardQuote,
    TdxEnrichment,
    TdxNews,
    TdxTheme,
)

DEFAULT_TDX_SNAPSHOT_DIR = Path("data") / "decision" / "tdx"


# 快照入口：文件名按交易日命名，文件不存在即为「本次未接入通达信」，这是合法状态而非错误。
def load_tdx_enrichment(
    trade_date: str, snapshot_dir: Path = DEFAULT_TDX_SNAPSHOT_DIR
) -> TdxEnrichment | None:
    path = snapshot_dir / f"{trade_date}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _build_enrichment(payload)


def _build_enrichment(payload: dict[str, Any]) -> TdxEnrichment:
    return TdxEnrichment(
        provider=_text(payload.get("provider")) or "tdx-connector",
        generated_at=_text(payload.get("generated_at")),
        code_lookup=_parse_code_lookup(payload.get("code_lookup")),
        board_quotes=tuple(_parse_board_quotes(payload.get("board_quotes"))),
        stock_themes=_parse_stock_themes(payload.get("stock_themes")),
        news=tuple(_parse_news(payload.get("news"))),
    )


# 名称到代码的映射；同一名称存在多个候选代码时保留首个并附带 note，供渲染时提示歧义。
def _parse_code_lookup(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    lookup: dict[str, dict[str, Any]] = {}
    for name, entry in value.items():
        name_text = _text(name)
        if not name_text or not isinstance(entry, dict):
            continue
        code = _text(entry.get("code"))
        if not code:
            continue
        record: dict[str, Any] = {"code": code}
        setcode = _text(entry.get("setcode"))
        if setcode:
            record["setcode"] = setcode
        note = _text(entry.get("note"))
        if note:
            record["note"] = note
        lookup[name_text] = record
    return lookup


def _parse_board_quotes(value: Any) -> list[TdxBoardQuote]:
    if not isinstance(value, list):
        return []
    quotes: list[TdxBoardQuote] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        code = _text(entry.get("code"))
        name = _text(entry.get("name"))
        kind = _text(entry.get("kind"))
        if not code or not name or kind not in ("concept", "industry"):
            continue
        quotes.append(
            TdxBoardQuote(
                code=code,
                name=name,
                kind=kind,
                change_percent=_number(entry.get("change_percent")),
                up_count=_integer(entry.get("up_count")),
                down_count=_integer(entry.get("down_count")),
                amount=_number(entry.get("amount")),
            )
        )
    return quotes


def _parse_stock_themes(value: Any) -> dict[str, TdxTheme]:
    if not isinstance(value, dict):
        return {}
    themes: dict[str, TdxTheme] = {}
    for code, entry in value.items():
        code_text = _text(code)
        if not code_text or not isinstance(entry, dict):
            continue
        theme = _text(entry.get("theme"))
        if not theme:
            continue
        themes[code_text] = TdxTheme(
            code=code_text,
            theme=theme,
            reason=_text(entry.get("reason")),
            first_limit_time=_text(entry.get("first_limit_time")),
            seal_amount=_number(entry.get("seal_amount")),
        )
    return themes


def _parse_news(value: Any) -> list[TdxNews]:
    if not isinstance(value, list):
        return []
    news: list[TdxNews] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        title = _text(entry.get("title"))
        if not title:
            continue
        news.append(
            TdxNews(
                title=title,
                source=_text(entry.get("source")),
                published_at=_text(entry.get("published_at")),
                summary=_text(entry.get("summary")),
            )
        )
    return news


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


# 自校验入口：供无人值守采集后自检，回答「快照是否合法、各字段采到几条、有没有条目被静默跳过」。
# ok 只表「文件存在、可解析、无被跳过的非法条目」；字段为空属于覆盖度提示，另行放进 warnings。
def inspect_tdx_snapshot(
    trade_date: str, snapshot_dir: Path = DEFAULT_TDX_SNAPSHOT_DIR
) -> dict[str, Any]:
    path = snapshot_dir / f"{trade_date}.json"
    report: dict[str, Any] = {
        "trade_date": trade_date,
        "path": str(path),
        "exists": path.is_file(),
        "ok": False,
        "provider": None,
        "generated_at": None,
        "counts": _empty_counts(),
        "dropped": [],
        "warnings": [],
    }
    if not path.is_file():
        report["warnings"].append("快照文件不存在，本次未接入通达信。")
        return report
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        report["warnings"].append(f"快照文件无法解析：{type(error).__name__}。")
        return report
    if not isinstance(payload, dict):
        report["warnings"].append("快照根节点必须是 JSON 对象。")
        return report

    enrichment = _build_enrichment(payload)
    report["provider"] = enrichment.provider
    report["generated_at"] = enrichment.generated_at
    parsed = {
        "code_lookup": len(enrichment.code_lookup),
        "board_quotes": len(enrichment.board_quotes),
        "stock_themes": len(enrichment.stock_themes),
        "news": len(enrichment.news),
    }
    report["counts"] = parsed

    raw = _raw_counts(payload)
    for field, parsed_count in parsed.items():
        # 原始条数多于可用条数，说明有非法条目被解析器跳过，必须显式报出而不是静默吞掉。
        if raw[field] > parsed_count:
            report["dropped"].append(
                f"{field}：原始 {raw[field]} 条，仅 {parsed_count} 条可用，{raw[field] - parsed_count} 条被跳过。"
            )
    for field, label in _FIELD_LABELS.items():
        if parsed[field] == 0:
            report["warnings"].append(f"{label}（{field}）为空，该项未接入。")

    report["ok"] = not report["dropped"]
    return report


_FIELD_LABELS = {
    "code_lookup": "名称→代码",
    "board_quotes": "板块行情",
    "stock_themes": "涨停逻辑",
    "news": "当日资讯",
}


def _empty_counts() -> dict[str, int]:
    return {field: 0 for field in _FIELD_LABELS}


def _raw_counts(payload: dict[str, Any]) -> dict[str, int]:
    def _length(value: Any) -> int:
        if isinstance(value, (dict, list)):
            return len(value)
        return 0

    return {field: _length(payload.get(field)) for field in _FIELD_LABELS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m stock_review.decision.tdx_snapshot",
        description="校验通达信快照是否合法、各字段覆盖情况与是否有条目被跳过。",
    )
    parser.add_argument("--date", required=True, help="快照对应的交易日，格式 YYYY-MM-DD。")
    parser.add_argument(
        "--tdx-dir", default=str(DEFAULT_TDX_SNAPSHOT_DIR), help="快照目录。"
    )
    args = parser.parse_args(argv)

    report = inspect_tdx_snapshot(args.date, Path(args.tdx_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
