"""G3 v2 — ``server.agent_runtime.llm_preflight`` 单元测试。"""

from __future__ import annotations

import pytest

from server.agent_runtime import llm_preflight as lp


@pytest.fixture(autouse=True)
def _reset_cache():
    lp.invalidate_cache()
    yield
    lp.invalidate_cache()


def _settings_reader(settings: dict[str, str]):
    """构造 ``ensure_llm_ready`` 的 read_settings_async seam，避免 monkey-patch 私有符号。"""

    async def _read() -> dict[str, str]:
        return settings

    return _read


# ---- probe_llm_endpoint ----------------------------------------------------


async def test_probe_llm_endpoint_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200

    class _FakeClient:
        async def get(self, url, timeout=None):  # noqa: ASYNC109 -- httpx.AsyncClient.get 形参与 stub 形参与生产一致；mock helper 无副作用
            return _Resp()

    monkeypatch.setattr(lp, "get_http_client", lambda: _FakeClient())
    assert await lp.probe_llm_endpoint("http://fake") is True


async def test_probe_llm_endpoint_5xx_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 503

    class _FakeClient:
        async def get(self, url, timeout=None):  # noqa: ASYNC109 -- httpx.AsyncClient.get 形参与 stub 形参与生产一致；mock helper 无副作用
            return _Resp()

    monkeypatch.setattr(lp, "get_http_client", lambda: _FakeClient())
    assert await lp.probe_llm_endpoint("http://fake") is False


async def test_probe_llm_endpoint_connection_error_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class _FakeClient:
        async def get(self, url, timeout=None):  # noqa: ASYNC109 -- httpx.AsyncClient.get 形参与 stub 形参与生产一致；mock helper 无副作用
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(lp, "get_http_client", lambda: _FakeClient())
    assert await lp.probe_llm_endpoint("http://fake") is False


# ---- ensure_llm_ready: multi 模式零开销 ---------------------------------------


async def test_multi_mode_returns_true_without_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    probed = {"n": 0}

    async def _fake_probe(url):
        probed["n"] += 1
        return False

    monkeypatch.setattr(lp, "probe_llm_endpoint", _fake_probe)

    result = await lp.ensure_llm_ready(read_settings_async=_settings_reader({"execution_mode": "multi"}))
    assert result is True
    assert probed["n"] == 0  # 未探测


async def test_invalid_execution_mode_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """execution_mode 字段非法 → 视同 multi，旁路。"""

    async def _fake_probe(url):
        raise AssertionError("should not probe")

    monkeypatch.setattr(lp, "probe_llm_endpoint", _fake_probe)

    result = await lp.ensure_llm_ready(read_settings_async=_settings_reader({"execution_mode": "bogus-value"}))
    assert result is True


# ---- ensure_llm_ready: exclusive + LLM 可达 ------------------------------------


async def test_exclusive_mode_llm_reachable_no_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    switched = {"n": 0}

    async def _fake_probe(url):
        return True

    async def _fake_ensure_env(target, **kwargs):
        switched["n"] += 1

    monkeypatch.setattr(lp, "probe_llm_endpoint", _fake_probe)
    monkeypatch.setattr(lp, "ensure_env", _fake_ensure_env)

    result = await lp.ensure_llm_ready(read_settings_async=_settings_reader({"execution_mode": "exclusive"}))
    assert result is True
    assert switched["n"] == 0  # 不切换


# ---- ensure_llm_ready: exclusive + LLM 不可达 + 不同 env -----------------------


async def test_exclusive_mode_unreachable_triggers_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"probe": 0, "switch": 0}

    async def _fake_probe(url):
        calls["probe"] += 1
        # 第一次 False，切换后 True
        return calls["probe"] >= 2

    async def _fake_ensure_env(target, **kwargs):
        calls["switch"] += 1

    def _fake_resolve(env: str):
        async def _resolve(*args, **kwargs) -> str:
            return env

        return _resolve

    monkeypatch.setattr(lp, "probe_llm_endpoint", _fake_probe)
    monkeypatch.setattr(lp, "ensure_env", _fake_ensure_env)
    monkeypatch.setattr(lp, "read_current_host_env", lambda p: "image-qwen21")

    result = await lp.ensure_llm_ready(
        read_settings_async=_settings_reader({"execution_mode": "exclusive"}),
        resolve_text_env=_fake_resolve("nsfw"),
    )
    assert result is True
    assert calls["probe"] == 2  # 切前探一次，切后探一次
    assert calls["switch"] == 1  # 切换一次


# ---- ensure_llm_ready: 已在目标 env 但 LLM 不可达 → 不切 ----------------------


async def test_exclusive_mode_unreachable_at_target_env_does_not_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switched = {"n": 0}

    async def _fake_probe(url):
        return False

    async def _fake_ensure_env(target, **kwargs):
        switched["n"] += 1

    def _fake_resolve(env: str):
        async def _resolve(*args, **kwargs) -> str:
            return env

        return _resolve

    monkeypatch.setattr(lp, "probe_llm_endpoint", _fake_probe)
    monkeypatch.setattr(lp, "ensure_env", _fake_ensure_env)
    monkeypatch.setattr(lp, "read_current_host_env", lambda p: "nsfw")  # 已在目标 env

    result = await lp.ensure_llm_ready(
        read_settings_async=_settings_reader({"execution_mode": "exclusive"}),
        resolve_text_env=_fake_resolve("nsfw"),
    )
    assert result is False  # 已在目标 env 仍不可达 → 报错
    assert switched["n"] == 0  # 不切


# ---- TTL 缓存 ----------------------------------------------------------------


async def test_ttl_cache_skips_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    probed = {"n": 0}

    async def _fake_probe(url):
        probed["n"] += 1
        return True

    monkeypatch.setattr(lp, "probe_llm_endpoint", _fake_probe)
    settings = _settings_reader({"execution_mode": "exclusive"})

    # 第一次探测 → 标记可达
    assert await lp.ensure_llm_ready(read_settings_async=settings) is True
    assert probed["n"] == 1

    # 第二次走 TTL 缓存 → 不再探测
    assert await lp.ensure_llm_ready(read_settings_async=settings) is True
    assert probed["n"] == 1  # 未增加

    # invalidate_cache 后再次探测
    lp.invalidate_cache()
    assert await lp.ensure_llm_ready(read_settings_async=settings) is True
    assert probed["n"] == 2


# ---- resolve_text_env_async ---------------------------------------------------


async def test_resolve_text_env_prefers_episode_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        await lp.resolve_text_env_async(
            read_settings_async=_settings_reader({"env_mapping": '{"episode_plan": "wide"}'})
        )
        == "wide"
    )


async def test_resolve_text_env_falls_back_to_script_plan_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        await lp.resolve_text_env_async(
            read_settings_async=_settings_reader({"env_mapping": '{"script_plan_content": "novel"}'})
        )
        == "novel"
    )


async def test_resolve_text_env_falls_back_to_default_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 空 env_mapping 走 DEFAULT_ENV_MAPPING，episode_plan=nsfw
    assert await lp.resolve_text_env_async(read_settings_async=_settings_reader({"env_mapping": ""})) == "nsfw"


# ---- LLMUnreachableError --------------------------------------------------------


def test_llm_unreachable_error_message() -> None:
    """LLMUnreachableError 的 message 含 target / current 与修复建议。"""
    from server.agent_runtime.session_manager import LLMUnreachableError

    err = LLMUnreachableError(target_env="nsfw", current_env="image-qwen21")
    assert err.target_env == "nsfw"
    assert err.current_env == "image-qwen21"
    msg = str(err)
    assert "nsfw" in msg
    assert "image-qwen21" in msg
    assert "arcreel-env-gate.sh" in msg  # 给用户明确的修复命令
