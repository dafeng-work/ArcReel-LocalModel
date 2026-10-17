"""G4 文本 env 前置闸门与上游不可达分类的单测。

覆盖 ``ensure_text_provider_env`` 的四条分支（内置供应商旁路 / env_name 为空旁路 /
已在目标 env 旁路 / 有任务在跑时拒绝切换），以及
:class:`~lib.backends.text_generator.TextGenerator` 的上游不可达翻译。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import openai
import pytest

from lib.backends import text_env_gate as gate
from lib.backends.text_env_gate import (
    TextEnvSwitchRequired,
    TextProviderUnavailableError,
    ensure_text_provider_env,
    is_definite_upstream_unavailable,
    is_loopback_base_url,
)


@asynccontextmanager
async def _fake_session():
    """最小 session 工厂替身：仓库方法本身被 monkeypatch 掉，这里只提供作用域。"""
    yield object()


def _bind_env(monkeypatch: pytest.MonkeyPatch, env_name: str | None, active_gpu_tasks: int) -> None:
    """把两个仓储方法钉到给定返回值，避免单测依赖真实库。"""
    monkeypatch.setattr(
        gate.CustomProviderRepository,
        "get_env_name",
        lambda self, provider_id: _async_return(env_name),
    )
    monkeypatch.setattr(
        gate.TaskRepository,
        "count_active_gpu_tasks",
        lambda self: _async_return(active_gpu_tasks),
    )


async def _async_return(value):
    return value


# ---- ensure_text_provider_env 的四条分支 --------------------------------------


async def test_builtin_provider_bypasses_without_touching_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """内置供应商（id 非 custom- 前缀）没有 env 绑定概念，直接放行。"""

    def _boom(self, provider_id):
        raise AssertionError("内置供应商不应查库")

    monkeypatch.setattr(gate.CustomProviderRepository, "get_env_name", _boom)
    called = False

    async def _fake_ensure_env(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(gate, "ensure_env", _fake_ensure_env)

    await ensure_text_provider_env("gemini-aistudio", session_factory=_fake_session)
    assert called is False


async def test_null_env_name_bypasses_completely(monkeypatch: pytest.MonkeyPatch) -> None:
    """env_name 为 NULL（默认）→ 零回归开关：行为与本特性引入前完全一致。"""
    _bind_env(monkeypatch, None, active_gpu_tasks=5)
    called = False

    async def _fake_ensure_env(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(gate, "ensure_env", _fake_ensure_env)
    monkeypatch.setattr(gate, "read_current_host_env", lambda *_a: "video-ltx23-dev")

    await ensure_text_provider_env("custom-4", session_factory=_fake_session)
    assert called is False


async def test_already_on_target_env_bypasses_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """已在目标 env → 不查在跑任务、不调 ensure_env。"""
    _bind_env(monkeypatch, "wide", active_gpu_tasks=5)
    called = False

    async def _fake_ensure_env(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(gate, "ensure_env", _fake_ensure_env)
    monkeypatch.setattr(gate, "read_current_host_env", lambda *_a: "wide")

    await ensure_text_provider_env("custom-4", session_factory=_fake_session)
    assert called is False


async def test_switches_env_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """空闲时自动切到目标 env。"""
    _bind_env(monkeypatch, "wide", active_gpu_tasks=0)
    monkeypatch.setattr(gate, "read_current_host_env", lambda *_a: "video-ltx23-dev")
    seen: list[str] = []

    async def _fake_ensure_env(target, **_kwargs):
        seen.append(target)

    monkeypatch.setattr(gate, "ensure_env", _fake_ensure_env)

    await ensure_text_provider_env("custom-4", session_factory=_fake_session)
    assert seen == ["wide"]


async def test_refuses_switch_while_gpu_tasks_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """有在跑 GPU 作业时快速失败，绝不切换（切换会 stop_all 打断别人的任务）。"""
    _bind_env(monkeypatch, "wide", active_gpu_tasks=3)
    monkeypatch.setattr(gate, "read_current_host_env", lambda *_a: "video-ltx23-dev")
    called = False

    async def _fake_ensure_env(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(gate, "ensure_env", _fake_ensure_env)

    with pytest.raises(TextEnvSwitchRequired) as exc_info:
        await ensure_text_provider_env("custom-4", session_factory=_fake_session)

    assert called is False
    assert exc_info.value.current_env == "video-ltx23-dev"
    assert exc_info.value.target_env == "wide"
    assert exc_info.value.active_task_count == 3


async def test_unparseable_provider_id_bypasses(monkeypatch: pytest.MonkeyPatch) -> None:
    """畸形 provider id 不该把请求打挂。"""
    called = False

    async def _fake_ensure_env(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(gate, "ensure_env", _fake_ensure_env)

    await ensure_text_provider_env("custom-not-a-number", session_factory=_fake_session)
    assert called is False


# ---- 回环判定 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8080/v1", "http://localhost:18080", "http://0.0.0.0:8080/v1", "http://[::1]:8080/v1"],
)
def test_loopback_base_urls_detected(url: str) -> None:
    assert is_loopback_base_url(url) is True


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://dashscope.aliyuncs.com", None, ""])
def test_non_loopback_base_urls_rejected(url: str | None) -> None:
    assert is_loopback_base_url(url) is False


def test_non_string_base_url_is_tolerated() -> None:
    """测试替身（Mock）的属性不是字符串，不该在这里炸掉。"""
    assert is_loopback_base_url(object()) is False


# ---- 上游不可达分类 ----------------------------------------------------------


def test_gateway_5xx_counts_as_upstream_unavailable() -> None:
    exc = openai.InternalServerError(
        message="bad gateway",
        response=httpx.Response(502, request=httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions")),
        body=None,
    )
    assert gate.classify_upstream_error(exc) == 502
    assert is_definite_upstream_unavailable(exc) is True


def test_connection_error_counts_as_upstream_unavailable() -> None:
    exc = httpx.ConnectError("connection refused")
    assert gate.classify_upstream_error(exc) is None
    assert is_definite_upstream_unavailable(exc) is True


def test_business_error_is_not_upstream_unavailable() -> None:
    """400 是请求本身的问题，不该被翻译成「上游不可达」。"""
    exc = openai.BadRequestError(
        message="bad request",
        response=httpx.Response(400, request=httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions")),
        body=None,
    )
    assert is_definite_upstream_unavailable(exc) is False


def test_runtime_error_is_not_upstream_unavailable() -> None:
    assert is_definite_upstream_unavailable(RuntimeError("API 超时")) is False


# ---- TextGenerator 的错误翻译 ------------------------------------------------


class _StubBackend:
    def __init__(self, *, base_url: str | None, exc: BaseException | None = None) -> None:
        self._base_url = base_url
        self._exc = exc
        self.model = "qwen-wide"

    @property
    def base_url(self) -> str | None:
        return self._base_url

    async def generate(self, request):
        if self._exc is not None:
            raise self._exc
        raise AssertionError("not used")


class _RecordingLedger:
    """``Ledger.record`` 的最小替身：只要一个 async context manager。"""

    class _Call:
        def success(self, result) -> None:
            pass

    def record(self, **_kwargs):
        ledger = self

        class _Ctx:
            async def __aenter__(self):
                return ledger._Call()

            async def __aexit__(self, *_exc) -> bool:
                return False

        return _Ctx()


def _make_generator(backend: _StubBackend):
    from lib.backends.providers import CallPurpose
    from lib.backends.text_backends.base import TextGenerationRequest
    from lib.backends.text_generator import TextGenerator

    gen = TextGenerator(backend, _RecordingLedger(), "custom-4", purpose=CallPurpose.PROJECT_OVERVIEW)
    return gen, TextGenerationRequest(prompt="x")


async def test_loopback_gateway_failure_becomes_provider_unavailable() -> None:
    exc = openai.InternalServerError(
        message="bad gateway",
        response=httpx.Response(502, request=httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions")),
        body=None,
    )
    gen, request = _make_generator(_StubBackend(base_url="http://127.0.0.1:8080/v1", exc=exc))

    with pytest.raises(TextProviderUnavailableError) as exc_info:
        await gen.generate(request, project_name="demo")

    assert exc_info.value.provider_id == "custom-4"
    assert exc_info.value.model == "qwen-wide"
    assert exc_info.value.upstream_status == 502


async def test_remote_provider_failure_reraises_untouched() -> None:
    """远端供应商的 5xx 可能是瞬时抖动，不归本次改动管 —— 原样上抛。"""
    exc = openai.InternalServerError(
        message="upstream",
        response=httpx.Response(502, request=httpx.Request("POST", "https://api.example.com/v1/chat/completions")),
        body=None,
    )
    gen, request = _make_generator(_StubBackend(base_url="https://api.example.com/v1", exc=exc))

    with pytest.raises(openai.InternalServerError):
        await gen.generate(request, project_name="demo")
