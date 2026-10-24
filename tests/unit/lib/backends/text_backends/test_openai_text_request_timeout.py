"""OpenAITextBackend 文本请求超时预算与失败可读化的单测。

覆盖三件事：

* 超时预算按本次输出体量伸缩（SDK 默认的固定 600s 与要生成多少 token 无关）；
* 读超时不被当成「上游不可达」，本机网关下直接给出可读原因而非重试放大等待；
* 思考吃光输出预算导致正文为空时，报出有行动指引的专属异常。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest
from pydantic import BaseModel

from lib.backends.text_backends.base import (
    DEFAULT_TEXT_REQUEST_TIMEOUT_SECONDS,
    TextGenerationRequest,
    TextReasoningBudgetExhaustedError,
    text_request_timeout_seconds,
)
from lib.infra.retry import NonRetryableError
from tests.fakes import captured_openai_clients

LOOPBACK_BASE_URL = "http://127.0.0.1:8080/v1"


def _make_mock_response(
    content: str = "Hello",
    input_tokens: int = 10,
    output_tokens: int = 5,
    reasoning_content: str | None = None,
    finish_reason: str = "stop",
):
    """构造 mock ChatCompletion 响应；``reasoning_content`` 显式置 str 或 None。"""
    usage = MagicMock()
    usage.prompt_tokens = input_tokens
    usage.completion_tokens = output_tokens

    message = MagicMock()
    message.content = content
    message.reasoning_content = reasoning_content

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


def _read_timeout_error() -> openai.APITimeoutError:
    """构造 openai SDK 把 httpx 读超时包成 APITimeoutError 的形态。"""
    request = httpx.Request("POST", f"{LOOPBACK_BASE_URL}/chat/completions")
    try:
        raise httpx.ReadTimeout("read timeout")
    except httpx.ReadTimeout as err:
        try:
            raise openai.APITimeoutError(request=request) from err
        except openai.APITimeoutError as exc:
            return exc


class TestTimeoutBudget:
    def test_small_request_keeps_sdk_default(self):
        """输出体量不大时行为不变：仍是 SDK 默认的 600s，不给远端供应商放大等待。"""
        assert text_request_timeout_seconds(None) == float(DEFAULT_TEXT_REQUEST_TIMEOUT_SECONDS)
        assert text_request_timeout_seconds(0) == float(DEFAULT_TEXT_REQUEST_TIMEOUT_SECONDS)
        assert text_request_timeout_seconds(2000) == float(DEFAULT_TEXT_REQUEST_TIMEOUT_SECONDS)

    def test_large_request_scales_beyond_default(self):
        """输出体量大的请求（整章剧本规划）不再被固定墙钟截断。"""
        assert text_request_timeout_seconds(32000) == 1600.0
        assert text_request_timeout_seconds(64000) == 3200.0

    async def test_backend_passes_scaled_timeout_to_sdk(self):
        """超时预算随请求下发到 SDK 调用上，而不是只靠客户端默认值。"""
        mock_client = AsyncMock()
        response = _make_mock_response("ok")
        mock_client.chat.completions.create = AsyncMock(return_value=response)

        with captured_openai_clients(mock_client):
            from lib.backends.text_backends.openai import OpenAITextBackend

            backend = OpenAITextBackend(api_key="k", base_url=LOOPBACK_BASE_URL)
            await backend.generate(TextGenerationRequest(prompt="hi", max_output_tokens=32000))

        assert mock_client.chat.completions.create.await_args.kwargs["timeout"] == 1600.0

    async def test_backend_passes_default_timeout_for_small_request(self):
        mock_client = AsyncMock()
        response = _make_mock_response("ok")
        mock_client.chat.completions.create = AsyncMock(return_value=response)

        with captured_openai_clients(mock_client):
            from lib.backends.text_backends.openai import OpenAITextBackend

            backend = OpenAITextBackend(api_key="k", base_url=LOOPBACK_BASE_URL)
            await backend.generate(TextGenerationRequest(prompt="hi", max_output_tokens=2000))

        assert mock_client.chat.completions.create.await_args.kwargs["timeout"] == 600.0


class TestReadTimeout:
    async def test_loopback_read_timeout_raises_readable_non_retryable(self):
        """本机网关的读超时：给出可读原因，且不重试（重发只会把等待放大数倍）。"""
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=_read_timeout_error())

        with captured_openai_clients(mock_client):
            from lib.backends.text_backends.openai import OpenAITextBackend

            backend = OpenAITextBackend(api_key="k", base_url=LOOPBACK_BASE_URL)
            with pytest.raises(NonRetryableError) as exc_info:
                await backend.generate(TextGenerationRequest(prompt="hi", max_output_tokens=32000))

        message = str(exc_info.value)
        assert "超时" in message
        assert "32000" in message
        # 只试一次：读超时不重试
        assert mock_client.chat.completions.create.await_count == 1

    async def test_remote_read_timeout_keeps_retrying(self):
        """远端端点的读超时更可能是瞬时抖动，保持重试。"""
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=_read_timeout_error())

        with captured_openai_clients(mock_client):
            from lib.backends.text_backends.openai import OpenAITextBackend

            backend = OpenAITextBackend(api_key="k", base_url="https://api.example.com/v1")
            with pytest.raises(openai.APITimeoutError):
                await backend.generate(TextGenerationRequest(prompt="hi", max_output_tokens=2000))

        assert mock_client.chat.completions.create.await_count > 1


class TestReasoningBudgetExhausted:
    async def test_structured_request_with_empty_content_raises_reasoning_error(self):
        """思考吃光输出预算：正文为空 + 有推理内容 → 报有行动指引的专属异常。"""

        class MyModel(BaseModel):
            name: str

        mock_client = AsyncMock()
        response = _make_mock_response(
            content="",
            reasoning_content="思考内容" * 100,
            finish_reason="length",
        )
        mock_client.chat.completions.create = AsyncMock(return_value=response)

        with captured_openai_clients(mock_client):
            from lib.backends.text_backends.openai import OpenAITextBackend

            backend = OpenAITextBackend(api_key="k", base_url=LOOPBACK_BASE_URL)
            with pytest.raises(TextReasoningBudgetExhaustedError) as exc_info:
                await backend.generate(
                    TextGenerationRequest(
                        prompt="hi",
                        response_schema=MyModel,
                        max_output_tokens=32000,
                    )
                )

        message = str(exc_info.value)
        assert "思考" in message
        assert "32000" in message

    async def test_content_present_with_reasoning_is_fine(self):
        """有正文时推理内容不影响结果（思考模型正常形态）。"""
        mock_client = AsyncMock()
        response = _make_mock_response(content='{"name": "x"}', reasoning_content="思考内容" * 100)
        mock_client.chat.completions.create = AsyncMock(return_value=response)

        with captured_openai_clients(mock_client):
            from lib.backends.text_backends.openai import OpenAITextBackend

            class MyModel(BaseModel):
                name: str

            backend = OpenAITextBackend(api_key="k", base_url=LOOPBACK_BASE_URL)
            result = await backend.generate(
                TextGenerationRequest(prompt="hi", response_schema=MyModel, max_output_tokens=32000)
            )

        assert result.text == '{"name": "x"}'
