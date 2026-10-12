"""G3 v2 — Agent 会话级 LLM 可达性 preflight。

**问题**：Agent 思考阶段要调 LLM（`host.docker.internal:8080/v1/messages`），但当前
env 可能是图片 / 视频 env（如 `image-qwen21`），此时 llama-server 与 r9700-gateway
都没在跑 → ECONNREFUSED → Agent 永远走不到调 MCP 工具那一步，工具级 preflight
(``server.agent_toolset.embedded._preflight_for_tool``) 根本没机会触发。

**解决**：在 Agent 会话发消息**之前**（``session_manager.send_message`` 入口）检测 LLM
端点是否可达；不可达则切到 ``text_backend`` / ``episode_plan`` 步骤对应的 env（默认
``nsfw``），等就绪后再继续。

**TTL 缓存**：可达状态缓存 60 秒，避免每条消息都跑一次 HTTP 探测。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from lib.config.dispatcher_schema import effective_env_mapping, parse_execution_mode
from lib.infra.httpx_shared import get_http_client
from server.agent_runtime.preflight_env import (
    DEFAULT_CURRENT_ENV_PATH,
    EnvSwitchError,
    ensure_env,
    read_current_host_env,
)

logger = logging.getLogger(__name__)


async def _read_settings_async() -> dict[str, str]:
    """读 system_settings 全量映射；失败返回空 dict（视同 multi 模式）。"""

    try:
        from lib.config.service import ConfigService
        from lib.db import async_session_factory

        async with async_session_factory() as session:
            return await ConfigService(session).get_all_settings()
    except Exception as exc:
        logger.warning("llm_preflight: failed to read system_settings: %s", exc)
        return {}


#: LLM 端点探测 URL。`r9700-gateway:8080` 是 LLM 统一入口，llama-server 经它暴露
#: `/v1/messages`（Anthropic 兼容）与 `/v1/models`。只探 `/v1/models`（轻量，无副作用）。
DEFAULT_LLM_PROBE_URL = "http://127.0.0.1:8080/v1/models"

#: 探测单次 HTTP 超时（秒）。本地 gateway 响应应在亚秒级。
LLM_PROBE_TIMEOUT_SECONDS = 3.0

#: 可达状态缓存 TTL（秒）。已确认可达后 60 秒内不再探测。
REACHABLE_TTL_SECONDS = 60.0

#: 兜底文本 env —— `env_mapping` 未配置或不含 `episode_plan` 时使用。
FALLBACK_TEXT_ENV = "nsfw"


# ---------------------------------------------------------------------------
# 状态缓存（模块级；进程内单实例）
# ---------------------------------------------------------------------------

_reachable_until: float = 0.0


def _is_cached_reachable() -> bool:
    return time.monotonic() < _reachable_until


def _mark_reachable() -> None:
    global _reachable_until
    _reachable_until = time.monotonic() + REACHABLE_TTL_SECONDS


def invalidate_cache() -> None:
    """强制下次探测。测试或运维在手动切 env 后调用。"""

    global _reachable_until
    _reachable_until = 0.0


# ---------------------------------------------------------------------------
# LLM 端点探测
# ---------------------------------------------------------------------------


async def probe_llm_endpoint(url: str = DEFAULT_LLM_PROBE_URL) -> bool:
    """HTTP GET 探测 LLM 端点；2xx 视为可达。任何异常返回 ``False``。

    ``get_http_client`` 未初始化时（独立脚本 / 测试场景）回退到 ``httpx.AsyncClient``。
    """

    try:
        client = get_http_client()
    except RuntimeError:
        # 独立脚本 / 测试场景：get_http_client 需要 startup_http_client() 先调用。
        # 回退到一次性 AsyncClient —— 仅探测用，不进共享池。
        async with httpx.AsyncClient() as fallback:
            return await _probe_with_client(fallback, url)
    return await _probe_with_client(client, url)


async def _probe_with_client(client: Any, url: str) -> bool:
    """用给定 httpx client 探测；2xx 视为可达。"""

    try:
        response = await client.get(url, timeout=LLM_PROBE_TIMEOUT_SECONDS)
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("llm probe %s failed: %s", url, exc)
        return False
    return 200 <= response.status_code < 300


# ---------------------------------------------------------------------------
# 目标 env 解析
# ---------------------------------------------------------------------------


async def resolve_text_env_async() -> str:
    """异步解析文本 env（从 system_settings 读 env_mapping）。"""

    all_settings = await _read_settings_async()
    try:
        env_mapping = effective_env_mapping(all_settings.get("env_mapping", ""))
    except Exception as exc:
        logger.warning("resolve_text_env_async: env_mapping invalid, fallback to %s: %s", FALLBACK_TEXT_ENV, exc)
        return FALLBACK_TEXT_ENV
    return env_mapping.get("episode_plan") or env_mapping.get("script_plan_content") or FALLBACK_TEXT_ENV


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def ensure_llm_ready(
    *,
    probe_url: str = DEFAULT_LLM_PROBE_URL,
    use_ttl_cache: bool = True,
) -> bool:
    """确保 LLM 端点可达；不可达则切到文本 env 后再探一次。

    返回 ``True`` = LLM 可达（含切换后变可达）；``False`` = 切换后仍不可达。

    仅 ``execution_mode == "exclusive"`` 时启用；``multi`` 模式直接返回 ``True``
    （零开销 —— 多模型模式下假设 LLM 始终在跑）。

    TTL 缓存：已确认可达后 ``REACHABLE_TTL_SECONDS`` 秒内跳过探测。
    """

    # 读 execution_mode：非 exclusive 直接放行（multi 模式零开销）
    all_settings = await _read_settings_async()
    try:
        execution_mode = parse_execution_mode(all_settings.get("execution_mode", ""))
    except Exception as exc:
        logger.warning("ensure_llm_ready: execution_mode invalid, skipping: %s", exc)
        return True

    if execution_mode != "exclusive":
        return True

    if use_ttl_cache and _is_cached_reachable():
        return True

    # 1) 先探一次
    if await probe_llm_endpoint(probe_url):
        _mark_reachable()
        return True

    # 2) 不可达 → 解析文本 env → 切换
    target_env = await resolve_text_env_async()
    current_env = read_current_host_env(DEFAULT_CURRENT_ENV_PATH)

    if current_env == target_env:
        # 已经在文本 env 但 LLM 仍不可达 —— 不切了，返回 False 让上层报错
        logger.warning(
            "ensure_llm_ready: LLM unreachable but already at target env %s; not switching",
            target_env,
        )
        return False

    logger.info(
        "ensure_llm_ready: LLM unreachable (current=%s), switching to %s",
        current_env,
        target_env,
    )
    try:
        await ensure_env(target_env)
    except EnvSwitchError as exc:
        logger.warning("ensure_llm_ready: ensure_env failed: %s", exc)
        return False

    # 3) 切完再探一次
    if await probe_llm_endpoint(probe_url):
        _mark_reachable()
        return True

    logger.warning("ensure_llm_ready: LLM still unreachable after switching to %s", target_env)
    return False


__all__ = [
    "DEFAULT_LLM_PROBE_URL",
    "FALLBACK_TEXT_ENV",
    "LLM_PROBE_TIMEOUT_SECONDS",
    "REACHABLE_TTL_SECONDS",
    "ensure_llm_ready",
    "invalidate_cache",
    "probe_llm_endpoint",
    "resolve_text_env_async",
]
