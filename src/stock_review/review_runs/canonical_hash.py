# 本文件负责领域对象的规范 JSON 与内容哈希，确保跨入口得到同一字节表示。

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any, Mapping

from stock_review.review_runs.domain_error import ReviewRunDomainError


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


# 规范化只接受可稳定序列化的值；集合等无序容器必须先由调用方显式排序。
def canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReviewRunDomainError("规范 JSON 不允许 NaN 或无穷数值。")
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return canonicalize(value.value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReviewRunDomainError("规范 JSON 的对象键必须是字符串。")
            normalized[key] = canonicalize(item)
        return normalized
    if isinstance(value, (tuple, list)):
        return [canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        raise ReviewRunDomainError("规范 JSON 不接受无序集合。")
    if is_dataclass(value):
        return {
            field.name: canonicalize(getattr(value, field.name))
            for field in fields(value)
        }
    raise ReviewRunDomainError(f"无法规范序列化类型：{type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def is_sha256(value: str) -> bool:
    return bool(SHA256_PATTERN.fullmatch(value))


def require_sha256(value: str, label: str, *, allow_empty: bool = False) -> None:
    if allow_empty and not value:
        return
    if not is_sha256(value):
        raise ReviewRunDomainError(f"{label}必须是 64 位小写 SHA-256。")
