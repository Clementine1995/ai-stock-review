# 本文件负责从用户维护的 stock-review.md 中识别 STEP 结构，不依赖数据源、存储或 LLM。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


STEP_HEADING_PATTERN = re.compile(
    r"^#{1,6}\s+STEP\s+(\d+)\s*:\s*(.+?)\s*$",
    re.MULTILINE,
)


class FrameworkParseError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewStep:
    number: int
    title: str
    body: str


@dataclass(frozen=True)
class ReviewFramework:
    source_path: Path
    steps: tuple[ReviewStep, ...]


# 文件读取统一使用 UTF-8，避免中文复盘框架在 Windows 终端环境下被错误解码。
def parse_framework_file(file_path: Path) -> ReviewFramework:
    source_path = Path(file_path)
    content = source_path.read_text(encoding="utf-8")
    return parse_framework_text(content, source_path)


# STEP 数量不硬编码；用户后续增删步骤时，报告会跟随 Markdown 标题变化。
def parse_framework_text(content: str, source_path: Path | None = None) -> ReviewFramework:
    matches = list(STEP_HEADING_PATTERN.finditer(content))
    if not matches:
        raise FrameworkParseError(
            "未识别到 STEP 标题，请检查 Markdown 是否包含 '# STEP N: 标题' 或同级 Markdown 标题格式。"
        )

    steps: list[ReviewStep] = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[match.end() : next_start].strip()
        steps.append(
            ReviewStep(
                number=int(match.group(1)),
                title=match.group(2).strip(),
                body=body,
            )
        )

    return ReviewFramework(
        source_path=source_path or Path("<memory>"),
        steps=tuple(steps),
    )
