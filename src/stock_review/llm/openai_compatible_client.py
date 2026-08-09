# 本文件负责调用 OpenAI-compatible 文本接口，不包含复盘业务判断或本地持久化。

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LLM_REQUEST_TIMEOUT_SECONDS = 300
DEEPSEEK_HYBRID_MODEL_PREFIXES = (
    "deepseek-v3.1",
    "deepseek-v3.2",
    "deepseek-v4-",
)


class LLMClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAICompatibleSettings:
    base_url: str
    api_key: str
    model: str

    @classmethod
    def from_environment(cls, env_path: Path = Path(".env")) -> "OpenAICompatibleSettings":
        file_values = read_local_env_file(env_path)
        values = {
            "LLM_BASE_URL": os.environ.get("LLM_BASE_URL", file_values.get("LLM_BASE_URL", "")).strip(),
            "LLM_API_KEY": os.environ.get("LLM_API_KEY", file_values.get("LLM_API_KEY", "")).strip(),
            "LLM_MODEL": os.environ.get("LLM_MODEL", file_values.get("LLM_MODEL", "")).strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise LLMClientError(f"缺少 LLM 配置环境变量：{', '.join(missing)}。")
        return cls(base_url=values["LLM_BASE_URL"], api_key=values["LLM_API_KEY"], model=values["LLM_MODEL"])


# 仅解析当前项目本地配置需要的三项，不修改进程环境变量，也不输出密钥内容。
def read_local_env_file(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", maxsplit=1)
        if name.strip() in {"LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"}:
            values[name.strip()] = value.strip()
    return values


# 兼容接口按模型能力选择响应模式；无论上游模式如何，业务层仍需本地校验全部事实字段。
def request_json_completion(
    settings: OpenAICompatibleSettings,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    use_deepseek_stream = settings.model.lower().startswith("deepseek-")
    request_body = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
    }
    if use_deepseek_stream:
        # 详细结构化归纳不需要长思考；混合思考模型关闭思考以避免首包和完整响应长时间等待。
        request_body.update(
            {
                "stream": True,
                "stream_options": {"include_usage": True},
                "enable_thinking": should_enable_deepseek_thinking(settings.model),
            }
        )
    else:
        request_body["response_format"] = {"type": "json_object"}
    endpoint = f"{settings.base_url.rstrip('/')}/chat/completions"
    request = Request(
        endpoint,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=LLM_REQUEST_TIMEOUT_SECONDS) as response:
            if use_deepseek_stream:
                content = read_streamed_completion_content(response)
            else:
                content = read_completion_content(response)
    except HTTPError as error:
        raise LLMClientError(f"LLM 接口返回 HTTP {error.code}。") from error
    except TimeoutError as error:
        raise LLMClientError(
            f"LLM 接口响应超时（{LLM_REQUEST_TIMEOUT_SECONDS} 秒）。"
        ) from error
    except URLError as error:
        raise LLMClientError(f"LLM 接口连接失败：{error.reason}。") from error
    try:
        result = json.loads(content)
    except json.JSONDecodeError as error:
        raise LLMClientError("LLM 草案不是有效 JSON。") from error
    if not isinstance(result, dict):
        raise LLMClientError("LLM 草案根节点必须是 JSON 对象。")
    return result


# R1 等仅思考模型继续保持原有行为；当前已确认支持思考开关的混合模型用于结构化任务时关闭思考。
def should_enable_deepseek_thinking(model: str) -> bool:
    normalized_model = model.lower()
    return not normalized_model.startswith(DEEPSEEK_HYBRID_MODEL_PREFIXES)


# 非流式响应沿用 OpenAI Chat Completions 的 message.content 结构。
def read_completion_content(response: Any) -> str:
    try:
        raw_response = json.loads(response.read().decode("utf-8"))
    except json.JSONDecodeError as error:
        raise LLMClientError("LLM 接口响应不是有效 JSON。") from error
    try:
        content = raw_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise LLMClientError("LLM 接口响应缺少 choices[0].message.content。") from error
    if not isinstance(content, str):
        raise LLMClientError("LLM 接口返回的草案内容不是文本。")
    return content


# DeepSeek 流式响应可能同时包含思维链和最终内容；报告只能读取最终 content。
def read_streamed_completion_content(response: Any) -> str:
    content_parts: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        event_data = line.removeprefix("data:").strip()
        if event_data == "[DONE]":
            break
        try:
            event = json.loads(event_data)
        except json.JSONDecodeError as error:
            raise LLMClientError("LLM 流式响应包含无效 JSON 事件。") from error
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
        content = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(content, str) and content:
            content_parts.append(content)
    if not content_parts:
        raise LLMClientError("LLM 流式响应未返回最终 content。")
    return "".join(content_parts)
