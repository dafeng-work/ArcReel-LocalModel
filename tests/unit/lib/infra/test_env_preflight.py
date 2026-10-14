"""G3 单机单卡互斥适配 — ``lib.infra.env_preflight`` 单元测试。

策略：把 ``asyncio.create_subprocess_exec`` 与 ``get_http_client`` 替换成 stub，
断言 ``ensure_env`` 的状态机：当前已是目标 → no-op；env-gate 失败 / 超时 → 结构化错误。

覆盖两类真实缺陷：
- 探活必须带网关 Bearer（r9700-gateway 自 2026-10-10 起强制鉴权，无头请求恒 401）；
- 探活端点按目标 env 类别二选一（ComfyUI 活着时 ``/system_stats`` 恒 200，「任一 200
  即就绪」会把 LLM 未就绪误判成就绪）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from lib.infra import env_preflight as pe


class _StubProc:
    """最小兼容 ``communicate()`` / ``returncode`` / ``terminate()`` / ``kill()`` / ``wait()`` 的子进程桩。"""

    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode: int | None = None
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.returncode = self._returncode
        return self._stdout, self._stderr

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self._returncode


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试都换一套 fresh 的 current.env + log_dir，避免污染。"""

    monkeypatch.setattr(pe, "DEFAULT_CURRENT_ENV_PATH", tmp_path / "current.env")
    monkeypatch.setattr(pe, "DEFAULT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(pe, "DEFAULT_GATE_SCRIPT", tmp_path / "arcreel-env-gate.sh")
    return


def _always_ready_probe() -> pe.ProbeReadyFn:
    """测试用 ``probe_ready`` seam stub：默认就绪。"""

    async def _stub() -> bool:
        return True

    return _stub


# ---- 已匹配：service 就绪则 no-op；未就绪则重新拉起 ----------------------------


async def test_ensure_env_current_matches_target_and_ready_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """current.env 写着 nsfw 且探活 200 → 不调闸门（正常路径，零子进程）。"""
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")

    called = False

    async def _fake_exec(*args: Any, **kwargs: Any):
        nonlocal called
        called = True
        return _StubProc(0)

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(pe, "gateway_auth_token", lambda **_kw: "tok")
    monkeypatch.setattr(pe, "get_http_client", lambda: _StubClient({pe.LLM_PROBE_URL: _StubResponse(200, b"models")}))

    result = await pe.ensure_env("nsfw", probe_ready=_always_ready_probe())
    assert called is False
    assert result.switched is False
    assert result.target_env == "nsfw"
    assert result.elapsed_seconds == 0.0


async def test_ensure_env_current_matches_target_but_service_down_relaunches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：``current.env`` 只是「上次选了哪个 env」的声明，不代表服务在跑。

    2026-10-13 事故 —— llama-server 已退出而该文件仍写着 nsfw，原实现按文件比对直接
    返回 no-op，env 栈掉线后因此永远没人拉起。现在探活不通就落回闸门分支
    （env-gate 对「已声明但未就绪」是幂等的）。
    """
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")
    (tmp_path / "arcreel-env-gate.sh").touch()
    (tmp_path / "arcreel-env-gate.sh").chmod(0o755)

    called = False

    async def _fake_exec(*args: Any, **kwargs: Any):
        nonlocal called
        called = True
        return _StubProc(0)

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(pe, "gateway_auth_token", lambda **_kw: "tok")
    # 单次快速探活拿不到 200（服务已退出）；闸门拉起后由 probe_ready seam 判就绪。
    monkeypatch.setattr(pe, "get_http_client", lambda: _StubClient({pe.LLM_PROBE_URL: _StubResponse(502, b"")}))

    result = await pe.ensure_env("nsfw", probe_ready=_always_ready_probe())
    assert called is True  # 真的调了闸门
    assert result.switched is True
    assert result.target_env == "nsfw"


async def test_ensure_env_ready_probe_sends_bearer_on_quick_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快速单次探活也必须带 Bearer —— 网关强制鉴权，无头探测恒 401。

    漏掉鉴权头会让「服务在跑」被恒判为不通，从而每次都白跑一次闸门。
    """
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")

    async def _fake_exec(*args: Any, **kwargs: Any):
        raise AssertionError("ready probe must short-circuit before spawning the gate")

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(pe, "gateway_auth_token", lambda **_kw: "secret-token")
    client = _StubClient({pe.LLM_PROBE_URL: _StubResponse(200, b"models")})
    monkeypatch.setattr(pe, "get_http_client", lambda: client)

    result = await pe.ensure_env("nsfw", probe_ready=_always_ready_probe())
    assert result.switched is False
    assert client.calls[0][1] == {"Authorization": "Bearer secret-token"}


# ---- 未匹配：spawn 子进程成功 -------------------------------------------------


async def test_ensure_env_spawns_gate_and_returns_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")
    (tmp_path / "arcreel-env-gate.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tmp_path / "arcreel-env-gate.sh").chmod(0o755)

    async def _fake_exec(*args: Any, **kwargs: Any):
        # 断言传给 env-gate 的参数
        assert args[0].endswith("arcreel-env-gate.sh")
        assert "video-ltx23-sulphur" in args
        assert "--" in args
        assert "true" in args
        return _StubProc(0)

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)

    result = await pe.ensure_env("video-ltx23-sulphur", probe_ready=_always_ready_probe())
    assert result.switched is True
    assert result.target_env == "video-ltx23-sulphur"


# ---- 子进程非零退出 → EnvSwitchFailed ------------------------------------------


async def test_ensure_env_gate_exit_nonzero_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")
    (tmp_path / "arcreel-env-gate.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    async def _fake_exec(*args: Any, **kwargs: Any):
        return _StubProc(3, stderr=b"gate failed: some reason")

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)

    with pytest.raises(pe.EnvSwitchFailed) as exc_info:
        await pe.ensure_env("video-ltx23-sulphur", probe_ready=_always_ready_probe())
    assert exc_info.value.target_env == "video-ltx23-sulphur"
    assert exc_info.value.exit_code == 3
    assert "gate failed" in exc_info.value.stderr_tail
    assert exc_info.value.log_path is not None


# ---- 闸门锁忙（exit 75）→ EnvSwitchBusy，而不是 EnvSwitchFailed --------------------


async def test_ensure_env_gate_lock_busy_raises_busy_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """锁被占时闸门以 75(EX_TEMPFAIL) 退出 —— 必须映射成「忙」而非「切换失败」。

    两者语义完全不同：忙 = 压根没开始切、稍后重试即可；失败 = 切了但炸了，要查 env
    配置。混为一谈会把用户引到错误的排查方向。
    """
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")
    (tmp_path / "arcreel-env-gate.sh").write_text("#!/bin/sh\nexit 75\n", encoding="utf-8")

    async def _fake_exec(*args: Any, **kwargs: Any):
        return _StubProc(pe.GATE_LOCK_BUSY_EXIT_CODE, stderr="[gate] ERROR: 拿不到 env 互斥锁".encode())

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)

    with pytest.raises(pe.EnvSwitchBusy) as exc_info:
        await pe.ensure_env("image-qwen21", probe_ready=_always_ready_probe())
    assert exc_info.value.target_env == "image-qwen21"
    assert "互斥锁" in exc_info.value.stderr_tail
    # 关键：不能被 EnvSwitchFailed 捕获（它是兄弟类，不是父类，故用 not isinstance 断言语义独立）
    assert not isinstance(exc_info.value, pe.EnvSwitchFailed)


async def test_ensure_env_passes_lock_timeout_zero_to_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """闸门必须以 ``--lock-timeout 0``（fail-fast）调用。

    否则排队会吃掉 DEFAULT_TIMEOUT_SECONDS=155s 预算，把「另一个批次在跑」伪装成
    EnvSwitchTimeout —— 而闸门其实一秒都没切。
    """
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")
    gate = tmp_path / "arcreel-env-gate.sh"
    gate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    captured: list[tuple[Any, ...]] = []

    async def _fake_exec(*args: Any, **kwargs: Any):
        captured.append(args)
        return _StubProc(0)

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)

    await pe.ensure_env("image-qwen21", probe_ready=_always_ready_probe())

    assert captured, "gate subprocess was never spawned"
    argv = [str(a) for a in captured[0]]
    idx = argv.index("--lock-timeout")
    assert argv[idx + 1] == "0"
    # 载荷仍是 true：fork 侧只负责切换，生成由 worker 异步跑
    assert argv[-1] == "true"


# ---- 子进程超时 → EnvSwitchTimeout + 子进程被 terminate/kill ----------------------


async def test_ensure_env_subprocess_timeout_terminates_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")
    (tmp_path / "arcreel-env-gate.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    class _HangingProc(_StubProc):
        async def communicate(self):
            await asyncio.sleep(60)
            return b"", b""

        async def wait(self) -> int:
            return 0

    async def _fake_exec(*args: Any, **kwargs: Any):
        return _HangingProc(0)

    # 把 wait_for 缩短：让测试不必真等 155s
    real_wait_for = asyncio.wait_for

    async def _short_wait_for(coro, timeout):  # noqa: ASYNC109 -- monkey-patched wait_for 必须保留 timeout 签名
        return await real_wait_for(coro, timeout=0.1)

    monkeypatch.setattr(pe.asyncio, "wait_for", _short_wait_for)
    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)

    with pytest.raises(pe.EnvSwitchTimeout):
        await pe.ensure_env("video-ltx23-sulphur", timeout_seconds=1.0, probe_ready=_always_ready_probe())


# ---- gate 脚本不存在 → EnvSwitchGateMissing -------------------------------------


async def test_ensure_env_gate_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")
    # 不创建 arcreel-env-gate.sh

    with pytest.raises(pe.EnvSwitchGateMissing):
        await pe.ensure_env("video-ltx23-sulphur", probe_ready=_always_ready_probe())


# ---- 并发互斥 -----------------------------------------------------------------


async def test_ensure_env_concurrent_calls_serialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """两个并发 ensure_env 同 target：第二个一定等第一个完成。"""

    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")
    (tmp_path / "arcreel-env-gate.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    started_at: list[float] = []

    async def _fake_exec(*args: Any, **kwargs: Any):
        import time as _t

        started_at.append(_t.monotonic())
        await asyncio.sleep(0.05)
        return _StubProc(0)

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)

    a, b = await asyncio.gather(
        pe.ensure_env("video-ltx23-sulphur", probe_ready=_always_ready_probe()),
        pe.ensure_env("video-ltx23-sulphur", probe_ready=_always_ready_probe()),
    )
    # 两次都成功（第二次命中 current 已是 target，不应再 spawn）
    # 注：第一次切完会把 current.env 改了，但 create_subprocess_exec 是 mock 不改 current.env。
    # 所以第二次会再次 spawn —— 两个调用被 _SWITCH_LOCK 串行化。
    assert a.switched is True
    assert b.switched is True
    # 检查两次 spawn 的时间有间隔（串行执行）
    if len(started_at) >= 2:
        assert started_at[1] - started_at[0] >= 0.04


# ---- 参数校验 -----------------------------------------------------------------


async def test_ensure_env_rejects_empty_target() -> None:
    with pytest.raises(pe.EnvSwitchError, match="empty"):
        await pe.ensure_env("")
    with pytest.raises(pe.EnvSwitchError, match="empty"):
        await pe.ensure_env("   ", probe_ready=_always_ready_probe())


# ---- current.env 不存在视为 (unknown) → 强制切换 --------------------------------


async def test_ensure_env_missing_current_env_forces_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 不创建 current.env —— _read_current_host_env 应返回 (unknown)
    (tmp_path / "arcreel-env-gate.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    async def _fake_exec(*args: Any, **kwargs: Any):
        return _StubProc(0)

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)

    result = await pe.ensure_env("video-ltx23-sulphur", probe_ready=_always_ready_probe())
    assert result.switched is True


# ---- 探活端点按 env 类别二选一 ------------------------------------------------


def test_probe_urls_for_llm_env_only_probes_llm_endpoint() -> None:
    """LLM 类 env 只探 /v1/models。

    回归「任一 200 即就绪」的误判：视频 env 起来时 /system_stats 恒 200 而 /v1/models 是 502，
    宽松判据会把「LLM 根本没起」报成就绪。
    """
    for env in ("wide", "nsfw", "novel", "story", "fast", "nsfw-moe"):
        assert pe.probe_urls_for_env(env) == (pe.LLM_PROBE_URL,), env


def test_probe_urls_for_comfyui_env_only_probes_comfyui_endpoint() -> None:
    for env in ("video-ltx23-dev", "video-ltx23-sulphur", "image-qwen21", "qwenimage"):
        assert pe.probe_urls_for_env(env) == (pe.COMFYUI_PROBE_URL,), env


def test_probe_urls_for_unknown_env_probes_both() -> None:
    """未登记 env 回退宽松判据，避免对没登记过的 env 误报失败。"""
    assert pe.probe_urls_for_env("some-future-env") == pe.DEFAULT_READY_PROBE_URLS


# ---- 网关鉴权 token ----------------------------------------------------------


def test_gateway_auth_token_prefers_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    common = tmp_path / "common.env"
    common.write_text("R9700_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("R9700_API_KEY", "from-env")

    assert pe.gateway_auth_token(common_env_path=common) == "from-env"


def test_gateway_auth_token_falls_back_to_common_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("R9700_API_KEY", raising=False)
    common = tmp_path / "common.env"
    common.write_text("# comment\nOTHER=1\nR9700_API_KEY=from-file\n", encoding="utf-8")

    assert pe.gateway_auth_token(common_env_path=common) == "from-file"


def test_gateway_auth_token_missing_file_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("R9700_API_KEY", raising=False)

    assert pe.gateway_auth_token(common_env_path=tmp_path / "nope.env") is None


def test_gateway_auth_headers_builds_bearer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """鉴权头的唯一构造点：ready_probe 与 llm_preflight 共用同一份 Bearer 拼法。"""
    monkeypatch.setenv("R9700_API_KEY", "from-env")

    assert pe.gateway_auth_headers(common_env_path=tmp_path / "nope.env") == {"Authorization": "Bearer from-env"}


def test_gateway_auth_headers_without_token_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("R9700_API_KEY", raising=False)

    assert pe.gateway_auth_headers(common_env_path=tmp_path / "nope.env") is None


# ---- ready_probe 实际发请求 --------------------------------------------------


class _StubResponse:
    def __init__(self, status_code: int, content: bytes = b"ok") -> None:
        self.status_code = status_code
        self.content = content


class _StubClient:
    """记录每次 GET 的 URL 与 headers，按 URL→响应字典应答。"""

    def __init__(self, responses: dict[str, _StubResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def get(
        self,
        url: str,
        *,
        timeout: float,  # noqa: ASYNC109 -- httpx 桩须保留 timeout 签名
        headers: dict[str, str] | None = None,
    ) -> _StubResponse:
        self.calls.append((url, headers))
        return self._responses.get(url, _StubResponse(502, b""))


async def test_default_probe_ready_sends_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归缺口 C：网关强制鉴权后，无头探活恒 401、永远判不出就绪。"""
    monkeypatch.setattr(pe, "gateway_auth_token", lambda **_kw: "secret-token")
    monkeypatch.setattr(pe, "READY_PROBE_MAX_ATTEMPTS", 1)
    client = _StubClient({pe.LLM_PROBE_URL: _StubResponse(200, b"models")})
    monkeypatch.setattr(pe, "get_http_client", lambda: client)

    assert await pe._default_probe_ready((pe.LLM_PROBE_URL,)) is True
    assert client.calls[0][1] == {"Authorization": "Bearer secret-token"}


async def test_default_probe_ready_without_token_probes_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    """token 缺失时保持无头请求：兼容远端 / 未启用鉴权的网关。"""
    monkeypatch.setattr(pe, "gateway_auth_token", lambda **_kw: None)
    monkeypatch.setattr(pe, "READY_PROBE_MAX_ATTEMPTS", 1)
    client = _StubClient({pe.LLM_PROBE_URL: _StubResponse(200, b"models")})
    monkeypatch.setattr(pe, "get_http_client", lambda: client)

    assert await pe._default_probe_ready((pe.LLM_PROBE_URL,)) is True
    assert client.calls[0][1] is None


async def test_default_probe_ready_ignores_comfyui_200_when_probing_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """探 /v1/models 时，ComfyUI 端点的 200 不得被当成「LLM 已就绪」。

    这是本次事故的判据缺口：只探 /v1/models，:8080/system_stats 即使 200 也不参与判定。
    """
    monkeypatch.setattr(pe, "gateway_auth_token", lambda **_kw: "tok")
    monkeypatch.setattr(pe, "READY_PROBE_MAX_ATTEMPTS", 1)
    client = _StubClient({pe.COMFYUI_PROBE_URL: _StubResponse(200, b"stats")})
    monkeypatch.setattr(pe, "get_http_client", lambda: client)

    assert await pe._default_probe_ready((pe.LLM_PROBE_URL,)) is False
    assert [url for url, _ in client.calls] == [pe.LLM_PROBE_URL]


async def test_default_probe_ready_requires_nonempty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 但响应体为空不算就绪（与 env-gate 脚本 wait_ready 同语义）。"""
    monkeypatch.setattr(pe, "gateway_auth_token", lambda **_kw: "tok")
    monkeypatch.setattr(pe, "READY_PROBE_MAX_ATTEMPTS", 1)
    client = _StubClient({pe.LLM_PROBE_URL: _StubResponse(200, b"")})
    monkeypatch.setattr(pe, "get_http_client", lambda: client)

    assert await pe._default_probe_ready((pe.LLM_PROBE_URL,)) is False


# ---- ensure_env 的探活按目标 env 选端点 ---------------------------------------


async def test_ensure_env_probes_target_env_specific_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """切到 LLM env 时补探测只打 /v1/models，不打 /system_stats。"""
    (tmp_path / "current.env").write_text("video-ltx23-dev\n", encoding="utf-8")
    (tmp_path / "arcreel-env-gate.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tmp_path / "arcreel-env-gate.sh").chmod(0o755)

    async def _fake_exec(*args: Any, **kwargs: Any):
        return _StubProc(0)

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(pe, "gateway_auth_token", lambda **_kw: "tok")
    monkeypatch.setattr(pe, "READY_PROBE_MAX_ATTEMPTS", 1)
    client = _StubClient({pe.LLM_PROBE_URL: _StubResponse(200, b"models")})
    monkeypatch.setattr(pe, "get_http_client", lambda: client)

    result = await pe.ensure_env("wide")

    assert result.switched is True
    assert {url for url, _ in client.calls} == {pe.LLM_PROBE_URL}
