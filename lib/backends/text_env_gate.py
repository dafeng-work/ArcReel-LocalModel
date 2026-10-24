"""文本模型的 env 前置闸门与上游不可达分类（G4）。

解决的是本机单卡多模型互斥部署下的一类具体失败：文本供应商经 ``r9700-gateway``
（:8080）代理到写死的 LLM 端口（:18080），而该端口只由**某个** env 提供。当前跑的是
别的 env 时端口无监听 → 网关 502 → 调用方等满重试预算 → 异常被路由兜底压成通用 500。

这里提供两件事：

* :func:`ensure_text_provider_env` —— 发文本请求**之前**确认宿主 env 与该 provider 绑定的
  env 一致。空闲时自动切（30–60s），有在跑 GPU 作业时快速失败并给出可执行提示，绝不打断
  别人的作业。``custom_provider.env_name`` 为 NULL 时整段旁路 —— 那是所有现存 provider 的
  默认值，因此对未启用本特性的部署零回归。
* :func:`classify_upstream_error` —— 把「上游明确不可达」与普通失败区分开。对前者不再
  重试（重发同一个必然复现的错误只是白等），并翻译成带排查指引的问题票。

**为什么住在 lib**：``lib/backends/text_generator.py`` 是全部 8 处文本任务的唯一收敛点，
而 ``lib`` 对 ``server`` 零导入，故闸门实现与它同层。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Final
from urllib.parse import urlparse

from lib.custom_provider import is_custom_provider, parse_provider_id
from lib.db.engine import async_session_factory
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.db.repositories.task_repo import TaskRepository
from lib.infra import env_preflight
from lib.infra.env_preflight import ensure_env, read_current_host_env

logger = logging.getLogger(__name__)


#: 上游「明确不可达」的 HTTP 状态码：网关 / 反代层说的「我这没有可转发的后端」，
#: 不是模型侧的业务错误，重发同一份请求不会改变结果。
UPSTREAM_UNAVAILABLE_STATUSES: Final[frozenset[int]] = frozenset({502, 503, 504})


class TextEnvSwitchRequired(RuntimeError):
    """目标文本 env 未激活，且当前有在跑的 GPU 作业，不能自动切换。

    不打断既有作业是硬约束：``arcreel-env-gate.sh`` 背后的 ``r9700-env switch`` 是
    ``stop_all + start``，切一次就把当前 env 整个停掉，等于把别人的生成任务打断。
    """

    def __init__(self, *, current_env: str, target_env: str, active_task_count: int) -> None:
        self.current_env = current_env
        self.target_env = target_env
        self.active_task_count = active_task_count
        super().__init__(
            f"text env {target_env!r} is not active (host is on {current_env!r}) and "
            f"{active_task_count} GPU task(s) are still running; refusing to switch env"
        )


class TextProviderUnavailableError(RuntimeError):
    """文本供应商上游明确不可达（网关 502/503/504 或连不上）。

    携带供应商与模型，供路由层翻译成带排查指引的问题票。
    """

    def __init__(self, *, provider_id: str, model: str, upstream_status: int | None, reason: str) -> None:
        self.provider_id = provider_id
        self.model = model
        self.upstream_status = upstream_status
        self.reason = reason
        super().__init__(f"{provider_id}/{model} upstream unavailable (status={upstream_status}): {reason}")


#: 测试注入 seam —— 替换 DB session 工厂，避免单测依赖真实库。
SessionFactory = Callable[[], Any]


def _default_session_factory() -> Any:
    return async_session_factory()


async def ensure_text_provider_env(
    provider_id: str,
    *,
    session_factory: SessionFactory | None = None,
) -> None:
    """发文本请求前确认宿主 env 与该 provider 绑定的 env 一致。

    四种情形：

    1. 内置供应商（provider id 非 ``custom-`` 前缀）→ 旁路，没有 env 绑定概念；
    2. ``env_name`` 为 NULL（默认）→ 旁路，行为与本特性引入前完全一致；
    3. 当前 env 已等于目标 → 旁路，零开销；
    4. 否则：先看有没有在跑的 GPU 作业，有则抛 :class:`TextEnvSwitchRequired`，
       没有才 :func:`~lib.infra.env_preflight.ensure_env` 切过去。
    """
    if not is_custom_provider(provider_id):
        return
    try:
        db_id = parse_provider_id(provider_id)
    except (TypeError, ValueError):
        logger.debug("unparseable custom provider id %r; skipping env preflight", provider_id)
        return

    factory = session_factory if session_factory is not None else _default_session_factory
    async with factory() as session:
        target_env = await CustomProviderRepository(session).get_env_name(db_id)
        if not target_env:
            return
        active_gpu_tasks = await TaskRepository(session).count_active_gpu_tasks()

    # 用 globals() 读模块常量而非写死路径 —— 与 env_preflight.ensure_env 同一约定：
    # 测试可 monkeypatch DEFAULT_CURRENT_ENV_PATH，不必 patch 函数签名。
    current_env = read_current_host_env(env_preflight.DEFAULT_CURRENT_ENV_PATH)
    if current_env == target_env:
        return

    if active_gpu_tasks > 0:
        logger.warning(
            "text env preflight: need %s but host is on %s with %d active GPU task(s); refusing to switch",
            target_env,
            current_env,
            active_gpu_tasks,
        )
        raise TextEnvSwitchRequired(
            current_env=current_env,
            target_env=target_env,
            active_task_count=active_gpu_tasks,
        )

    logger.info("text env preflight: switching %s -> %s for %s", current_env, target_env, provider_id)
    await ensure_env(target_env)


def is_loopback_base_url(base_url: str | None) -> bool:
    """base_url 是否指向本机回环（本地网关 / 本地推理服务）。

    只对回环端点判定「上游不可达不可重试」：远端供应商的 5xx/连接失败可能是瞬时抖动，
    重试有意义；本地端口没监听则是确定性事实，重发只会把失败预算（默认 4 次、退避 2+4+8
    秒）白白耗掉，最后仍以同一个错误收场。

    刻意做 ``isinstance`` 类型守卫：调用方可能传入测试替身（``AsyncMock`` 的属性是 Mock
    对象而非字符串），直接 ``urlparse`` 会在其上抛非 ``ValueError`` 的异常。
    """
    if not isinstance(base_url, str) or not base_url:
        return False
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def classify_upstream_error(exc: BaseException) -> int | None:
    """判定「上游明确不可达」，返回其 HTTP 状态码；不属于该类则返回 None。

    覆盖两类来源：

    * OpenAI 兼容 SDK 的 ``InternalServerError`` / ``ServiceUnavailableError`` 等带
      ``status_code`` 的异常；
    * ``httpx`` 的连接类异常（连不上端口），此时没有状态码，返回 None 但仍被
      :func:`is_definite_upstream_unavailable` 判为不可达。
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in UPSTREAM_UNAVAILABLE_STATUSES:
        return status
    return None


def is_read_timeout(exc: BaseException) -> bool:
    """判断异常是否为「读超时」——上游还活着，只是没在墙钟预算内答完。

    openai SDK 把 httpx 的超时统一包成 ``APITimeoutError``，而它是 ``APIConnectionError``
    的子类：按连接类异常一刀切，会把「上游活着、只是慢」误判成「上游不可达」，于是
    重试被短路、问题票被翻译成「上游没在跑」，真正病因（输出体量太大 / 思考吃光预算）
    被盖住。httpx 的原始异常保留在 ``__cause__`` 里，据此把读超时与连不上区分开：
    ``httpx.ConnectTimeout`` 是真连不上（不可达），``httpx.ReadTimeout`` 只是没答完。
    """
    import httpx

    if isinstance(exc, httpx.ReadTimeout):
        return True
    return isinstance(exc.__cause__, httpx.ReadTimeout)


def is_definite_upstream_unavailable(exc: BaseException) -> bool:
    """本地端点上，这是一次「重发也不会变」的失败。

    判定口径：回环 base_url +（网关级状态码 502/503/504 或连接类异常）。连接类异常按
    类型判定而非消息文本，避免误伤真正携带 5xx 文本的业务错误。

    读超时（上游活着、只是慢）不算不可达：重发有意义，且按不可达上报会把病因说错。
    """
    import httpx
    import openai

    if classify_upstream_error(exc) is not None:
        return True
    if is_read_timeout(exc):
        return False
    connection_types: tuple[type[BaseException], ...] = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        openai.APIConnectionError,
    )
    return isinstance(exc, connection_types)


__all__ = [
    "UPSTREAM_UNAVAILABLE_STATUSES",
    "TextEnvSwitchRequired",
    "TextProviderUnavailableError",
    "classify_upstream_error",
    "ensure_text_provider_env",
    "is_definite_upstream_unavailable",
    "is_loopback_base_url",
    "is_read_timeout",
]
