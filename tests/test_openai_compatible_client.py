import json
import unittest
from unittest.mock import patch

from stock_review.llm.openai_compatible_client import (
    LLMClientError,
    LLM_REQUEST_TIMEOUT_SECONDS,
    OpenAICompatibleSettings,
    request_json_completion,
)


class FakeResponse:
    def __init__(self, *, body: bytes = b"", lines: tuple[bytes, ...] = ()):
        self.body = body
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, _error_type, _error, _traceback):
        return False

    def read(self) -> bytes:
        return self.body

    def __iter__(self):
        return iter(self.lines)


class OpenAICompatibleClientTest(unittest.TestCase):
    def test_deepseek_hybrid_model_streams_without_thinking_and_returns_final_content(self):
        response = FakeResponse(
            lines=(
                b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"{\\"step_analyses\\":"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"[]}"}}]}\n',
                b'data: {"choices":[],"usage":{"total_tokens":12}}\n',
                b'data: [DONE]\n',
            )
        )

        with patch(
            "stock_review.llm.openai_compatible_client.urlopen",
            return_value=response,
        ) as mocked_urlopen:
            result = request_json_completion(
                OpenAICompatibleSettings(
                    "https://example.invalid/compatible-mode/v1",
                    "test-key",
                    "deepseek-v4-pro",
                ),
                "system",
                "user",
            )

        request, = mocked_urlopen.call_args.args
        request_body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result, {"step_analyses": []})
        self.assertTrue(request_body["stream"])
        self.assertFalse(request_body["enable_thinking"])
        self.assertEqual(request_body["stream_options"], {"include_usage": True})
        self.assertNotIn("response_format", request_body)
        self.assertEqual(
            mocked_urlopen.call_args.kwargs["timeout"],
            LLM_REQUEST_TIMEOUT_SECONDS,
        )

    def test_deepseek_thinking_only_model_keeps_thinking_enabled(self):
        response = FakeResponse(
            lines=(
                b'data: {"choices":[{"delta":{"content":"{\\"result\\": \\"ok\\"}"}}]}\n',
                b'data: [DONE]\n',
            )
        )

        with patch(
            "stock_review.llm.openai_compatible_client.urlopen",
            return_value=response,
        ) as mocked_urlopen:
            result = request_json_completion(
                OpenAICompatibleSettings(
                    "https://example.invalid/compatible-mode/v1",
                    "test-key",
                    "deepseek-r1",
                ),
                "system",
                "user",
            )

        request, = mocked_urlopen.call_args.args
        request_body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result, {"result": "ok"})
        self.assertTrue(request_body["stream"])
        self.assertTrue(request_body["enable_thinking"])

    def test_non_deepseek_model_keeps_json_mode(self):
        response_body = {
            "choices": [{"message": {"content": '{"result": "ok"}'}}]
        }
        response = FakeResponse(body=json.dumps(response_body).encode("utf-8"))

        with patch(
            "stock_review.llm.openai_compatible_client.urlopen",
            return_value=response,
        ) as mocked_urlopen:
            result = request_json_completion(
                OpenAICompatibleSettings(
                    "https://example.invalid/compatible-mode/v1",
                    "test-key",
                    "qwen-plus",
                ),
                "system",
                "user",
            )

        request, = mocked_urlopen.call_args.args
        request_body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result, {"result": "ok"})
        self.assertEqual(request_body["response_format"], {"type": "json_object"})
        self.assertNotIn("stream", request_body)
        self.assertNotIn("enable_thinking", request_body)

    def test_timeout_reports_explicit_request_limit(self):
        with patch(
            "stock_review.llm.openai_compatible_client.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            with self.assertRaisesRegex(LLMClientError, "300 秒"):
                request_json_completion(
                    OpenAICompatibleSettings(
                        "https://example.invalid/compatible-mode/v1",
                        "test-key",
                        "deepseek-v4-pro",
                    ),
                    "system",
                    "user",
                )

    def test_deepseek_stream_without_final_content_is_rejected(self):
        response = FakeResponse(
            lines=(
                b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n',
                b'data: [DONE]\n',
            )
        )

        with patch(
            "stock_review.llm.openai_compatible_client.urlopen",
            return_value=response,
        ):
            with self.assertRaisesRegex(LLMClientError, "未返回最终 content"):
                request_json_completion(
                    OpenAICompatibleSettings(
                        "https://example.invalid/compatible-mode/v1",
                        "test-key",
                        "deepseek-v4-pro",
                    ),
                    "system",
                    "user",
                )


if __name__ == "__main__":
    unittest.main()
