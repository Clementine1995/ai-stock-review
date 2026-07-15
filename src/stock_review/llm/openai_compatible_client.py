# 本文件负责调用 OpenAI-compatible 文本接口，不包含复盘业务判断或本地持久化。

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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


# 兼容接口使用 Chat Completions JSON 模式；业务层仍需本地校验，不能把模型输出直接当作事实。
def request_json_completion(settings: OpenAICompatibleSettings, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    request_body = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    endpoint = f"{settings.base_url.rstrip('/')}/chat/completions"
    request = Request(
        endpoint,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            raw_response = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise LLMClientError(f"LLM 接口返回 HTTP {error.code}。") from error
    except URLError as error:
        raise LLMClientError(f"LLM 接口连接失败：{error.reason}。") from error
    except json.JSONDecodeError as error:
        raise LLMClientError("LLM 接口响应不是有效 JSON。") from error

    try:
        content = raw_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise LLMClientError("LLM 接口响应缺少 choices[0].message.content。") from error
    if not isinstance(content, str):
        raise LLMClientError("LLM 接口返回的草案内容不是文本。")
    try:
        result = json.loads(content)
    except json.JSONDecodeError as error:
        raise LLMClientError("LLM 草案不是有效 JSON。") from error
    if not isinstance(result, dict):
        raise LLMClientError("LLM 草案根节点必须是 JSON 对象。")
    return result
