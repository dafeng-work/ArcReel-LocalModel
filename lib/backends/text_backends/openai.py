"""OpenAITextBackend — OpenAI 文本生成后端。"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI, BadRequestError

from lib.backends.openai_shared import OPENAI_RETRYABLE_ERRORS, create_openai_client
from lib.backends.providers import PROVIDER_DASHSCOPE, PROVIDER_OPENAI
from lib.backends.text_backends.base import (
    TextCapability,
    TextGenerationRequest,
    TextGenerationResult,
    TextReasoningBudgetExhaustedError,
    TokenParam,
    check_truncation,
    merge_billed_tokens,
    resolve_schema,
    strip_leading_think_block,
    structured_fallback_reason,
    text_request_timeout_seconds,
    truncate_for_log,
)
from lib.backends.text_env_gate import (
    is_definite_upstream_unavailable,
    is_loopback_base_url,
    is_read_timeout,
)
from lib.config.url_utils import is_official_openai_base_url
from lib.infra.logging_utils import format_kwargs_for_log
from lib.infra.retry import NonRetryableError, with_retry_async

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5.4-mini"


class OpenAITextBackend:
    """OpenAI 文本生成后端，支持 Chat Completions API。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        provider_name: str = PROVIDER_OPENAI,
    ):
        # 禁用 SDK 内置重试，由本层 generate() 统一管理重试策略
        self._client = create_openai_client(api_key=api_key, base_url=base_url, max_retries=0)
        self._base_url = base_url
        self._model = model or DEFAULT_MODEL
        # 复用 OpenAI 兼容协议的 provider（如 dashscope）须用真实 provider 记账，
        # 否则计费查表会命中 OpenAI 的 USD 费率而非自身定价。
        self._provider_name = provider_name
        # 官方端点已弃用 max_tokens（推理模型直接拒绝），用 max_completion_tokens；
        # 第三方兼容端点（自定义供应商、dashscope 等）不保证支持新参数，保守沿用 max_tokens
        self._max_tokens_param: TokenParam = (
            "max_completion_tokens" if is_official_openai_base_url(base_url) else "max_tokens"
        )
        self._capabilities: set[TextCapability] = self._resolve_capabilities()

    def _resolve_capabilities(self) -> set[TextCapability]:
        """官方 OpenAI 与自定义 provider 保持固定能力；DashScope 按注册表声明判定。"""
        base = {TextCapability.TEXT_GENERATION, TextCapability.VISION}
        if self._provider_name != PROVIDER_DASHSCOPE:
            base.add(TextCapability.STRUCTURED_OUTPUT)
            return base

        from lib.config.registry import PROVIDER_REGISTRY

        provider_meta = PROVIDER_REGISTRY.get(self._provider_name)
        model_info = provider_meta.models.get(self._model) if provider_meta else None
        if model_info and TextCapability.STRUCTURED_OUTPUT in model_info.capabilities:
            base.add(TextCapability.STRUCTURED_OUTPUT)
        return base

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def base_url(self) -> str | None:
        """本 provider 的 base_url。

        上层（``TextGenerator``）据此区分「本机网关上游不可达」与远端瞬时故障：只有回环
        端点上，连接类失败才是确定性的（端口没监听），远端的同类失败仍值得重试。
        """
        return self._base_url

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[TextCapability]:
        return self._capabilities

    async def generate(self, request: TextGenerationRequest) -> TextGenerationResult:
        """生成文本回复。

        本方法不带重试装饰器：瞬态错误重试分别在 :meth:`_generate_native` 与
        :func:`_instructor_fallback` 层完成。若把复验/降级也包进重试范围，降级尝试的
        失败会连带重放已成功的原生调用（重试叠乘），且降级异常消息含模型侧动态文本，
        可能误中重试判定的字符串模式。
        """
        messages = _build_messages(request)
        if request.response_schema and TextCapability.STRUCTURED_OUTPUT not in self._capabilities:
            logger.info("%s/%s 未声明原生结构化输出，直接走 Instructor", self._provider_name, self._model)
            return await _instructor_fallback(
                self._client,
                self._model,
                request,
                messages,
                provider=self._provider_name,
                token_param=self._max_tokens_param,
            )

        native = await self._generate_native(request, messages)
        if native is None:
            # 原生 response_format 通道不兼容（schema 错误），结构化输出整体降级
            return await _instructor_fallback(
                self._client,
                self._model,
                request,
                messages,
                provider=self._provider_name,
                token_param=self._max_tokens_param,
            )

        if request.response_schema:
            fallback_reason = structured_fallback_reason(native.text, request.response_schema)
            if fallback_reason:
                logger.warning(
                    "原生 response_format %s，降级到带校验的 Instructor 路径；模型原始输出：%s",
                    fallback_reason,
                    truncate_for_log(native.text),
                )
                result = await _instructor_fallback(
                    self._client,
                    self._model,
                    request,
                    messages,
                    provider=self._provider_name,
                    token_param=self._max_tokens_param,
                )
                # 这次原生 200 调用已被代理计费，把它的 token 并入降级结果。
                result.input_tokens = merge_billed_tokens(result.input_tokens, native.input_tokens)
                result.output_tokens = merge_billed_tokens(result.output_tokens, native.output_tokens)
                return result

        return native

    @with_retry_async(max_attempts=4, backoff_seconds=(2, 4, 8), retryable_errors=OPENAI_RETRYABLE_ERRORS)
    async def _generate_native(
        self, request: TextGenerationRequest, messages: list[dict]
    ) -> TextGenerationResult | None:
        """单次原生调用：构造 kwargs、发请求、解析与截断告警，瞬态错误重试。

        schema 不兼容时返回 ``None``（降级信号）而不抛异常：代理可能把上游 schema 错误
        包装成 429 等状态码，若以异常形式穿过重试装饰器，会被字符串模式误判为瞬态错误
        白白重试。
        """
        kwargs: dict = {"model": self._model, "messages": messages}
        if request.max_output_tokens is not None:
            kwargs[self._max_tokens_param] = request.max_output_tokens

        # 超时按本次输出体量给：SDK 默认的固定 600s 与要生成多少 token 无关，
        # 输出体量大的任务（整章剧本规划等）在本机慢速模型上会稳定撞线。
        # 只作用于本方法这条原生通道；Instructor 降级通道仍用客户端默认超时——
        # 它只在供应商不支持 response_format 时才走，那类供应商的输出体量本就小。
        timeout = text_request_timeout_seconds(request.max_output_tokens)

        if request.response_schema:
            schema = resolve_schema(request.response_schema)
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "strict": True,
                    "schema": schema,
                },
            }

        logger.info("调用 %s 文本 SDK kwargs=%s", self.name, format_kwargs_for_log(kwargs))
        try:
            response = await self._client.chat.completions.create(**kwargs, timeout=timeout)
        except Exception as exc:
            if request.response_schema and _is_schema_error(exc):
                logger.warning(
                    "原生 response_format 失败 (%s)，降级到 Instructor 路径",
                    exc,
                )
                return None
            # 读超时：上游还活着、只是没在墙钟预算内答完。它不是「上游不可达」
            # （is_definite_upstream_unavailable 已排除读超时），病因通常是「输出体量 ×
            # 解码速度」超出了墙钟，重发同一份请求多半还是超时。
            if is_read_timeout(exc):
                if is_loopback_base_url(self._base_url):
                    # 本机网关背后是本地模型：耗时由体量与解码速度决定，重发不会更快，
                    # 4 次重试只会把等待放大数倍。直接报可读原因，别让「超时」两个字
                    # 盖住真正的病因。
                    raise NonRetryableError(
                        f"{self._provider_name}/{self._model} 文本请求超时："
                        f"{timeout:.0f} 秒预算内上游没有返回"
                        f"（本次请求输出上限 {request.max_output_tokens} token）。"
                        "请改用解码更快或思考更短的文本模型。"
                    ) from exc
                # 远端端点：超时更可能是瞬时抖动，保持重试。
                raise
            # 本机网关的上游没在跑时，重发同一份请求不会改变结果——只会把 4 次尝试、
            # 退避 2+4+8 秒（约 15 秒）的预算耗光，最后仍以同一个错误收场。包成
            # NonRetryableError 让 retry_async 立即短路；远端端点不在此列，保持重试。
            if is_loopback_base_url(self._base_url) and is_definite_upstream_unavailable(exc):
                raise NonRetryableError(str(exc)) from exc
            raise

        usage = response.usage
        choice = response.choices[0]
        output_tokens = usage.completion_tokens if usage else None
        raw_text = choice.message.content or ""
        # 思考模型在部分 OpenAI 兼容服务上把思考写进独立的 reasoning_content，正文留在
        # content。思考吃光输出预算时 content 为空、推理内容却很长——这不是「模型没输出」，
        # 出路是调低思考档位/预算或换更快的模型，单独报出来才有行动指引。
        reasoning_raw = getattr(choice.message, "reasoning_content", None)
        reasoning_text = reasoning_raw if isinstance(reasoning_raw, str) else ""
        if not raw_text.strip() and reasoning_text.strip():
            if request.response_schema:
                raise TextReasoningBudgetExhaustedError(
                    provider=self._provider_name,
                    model=self._model,
                    reasoning_chars=len(reasoning_text),
                    max_output_tokens=request.max_output_tokens,
                )
            logger.warning(
                "%s/%s 正文为空但输出了 %d 字符思考内容，思考可能吃光了输出预算",
                self._provider_name,
                self._model,
                len(reasoning_text),
            )
        # 思考模型默认把思考块内嵌在 content 开头，正文（结构化输出即 JSON）跟在其后；
        # 复验与返回都只看正文，思考块不进结果。
        text = strip_leading_think_block(raw_text)
        if text != raw_text:
            logger.info(
                "%s/%s 输出开头带思考块，已剥离（%d 字符）",
                self._provider_name,
                self._model,
                len(raw_text) - len(text),
            )

        truncated = check_truncation(
            getattr(choice, "finish_reason", None),
            provider=self._provider_name,
            model=self._model,
            output_tokens=output_tokens,
            structured=bool(request.response_schema),
        )
        return TextGenerationResult(
            text=text,
            provider=self._provider_name,
            model=self._model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=output_tokens,
            truncated=truncated,
        )


def _build_messages(request: TextGenerationRequest) -> list[dict]:
    """将 TextGenerationRequest 转为 OpenAI messages 格式。"""
    messages: list[dict] = []

    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})

    # 构建 user message
    if request.images:
        from lib.backends.image_backends.base import image_to_base64_data_uri

        content: list[dict] = []
        for img in request.images:
            if img.path:
                data_uri = image_to_base64_data_uri(img.path)
                content.append({"type": "image_url", "image_url": {"url": data_uri}})
            elif img.url:
                content.append({"type": "image_url", "image_url": {"url": img.url}})
        content.append({"type": "text", "text": request.prompt})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": request.prompt})

    return messages


_SCHEMA_ERROR_KEYWORDS = (
    "response_schema",
    "json_schema",
    "Unknown name",
    "Cannot find field",
    "Invalid JSON payload",
)


def _is_schema_error(exc: BaseException) -> bool:
    """判断异常是否为 JSON Schema 不兼容导致的错误。

    除了标准的 400 BadRequestError，一些 OpenAI 兼容代理（如 Gemini
    兼容端点）会将上游 schema 错误包装成其他状态码（如 429），
    因此也检查错误信息中是否包含 schema 相关关键字。
    """
    if isinstance(exc, BadRequestError):
        return True
    # 代理可能把上游 schema 错误包装成非 400 状态码
    error_str = str(exc)
    return any(kw in error_str for kw in _SCHEMA_ERROR_KEYWORDS)


@with_retry_async(max_attempts=4, backoff_seconds=(2, 4, 8), retryable_errors=OPENAI_RETRYABLE_ERRORS)
async def _instructor_fallback(
    client: AsyncOpenAI,
    model: str,
    request: TextGenerationRequest,
    messages: list[dict],
    *,
    provider: str = PROVIDER_OPENAI,
    token_param: TokenParam = "max_tokens",
) -> TextGenerationResult:
    """Instructor 降级：当原生 response_format 不可用时的备选路径。

    instructor_fallback_async 自身不做瞬态重试，这里补一层与原生调用同配置的重试；
    范围仅覆盖降级自身，失败不会重放已成功的原生调用。
    """
    from lib.backends.text_backends.instructor_support import instructor_fallback_async

    return await instructor_fallback_async(
        client=client,
        model=model,
        messages=messages,
        response_schema=request.response_schema,
        provider=provider,
        max_tokens=request.max_output_tokens,
        token_param=token_param,
    )
