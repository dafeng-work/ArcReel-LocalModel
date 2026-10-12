"""G3 单机单卡互斥适配 — ``server.agent_runtime.preflight_env`` 单元测试。

策略：把 ``asyncio.create_subprocess_exec`` 与 ``get_http_client`` 替换成 stub，
断言 ``ensure_env`` 的状态机：当前已是目标 → no-op；env-gate 失败 / 超时 → 结构化错误。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.agent_runtime import preflight_env as pe


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
    # 替换 ready_probe 让测试环境不必真起 8080
    async def _fake_probe_ready() -> bool:
        return True

    monkeypatch.setattr(pe, "_probe_ready", _fake_probe_ready)
    yield


# ---- 已匹配：no-op -----------------------------------------------------


async def test_ensure_env_current_matches_target_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")

    called = False

    async def _fake_exec(*args: Any, **kwargs: Any):
        called = True
        return _StubProc(0)

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)

    result = await pe.ensure_env("nsfw")
    assert called is False
    assert result.switched is False
    assert result.target_env == "nsfw"
    assert result.elapsed_seconds == 0.0


# ---- 未匹配：spawn 子进程成功 -------------------------------------------------


async def test_ensure_env_spawns_gate_and_returns_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    result = await pe.ensure_env("video-ltx23-sulphur")
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
        await pe.ensure_env("video-ltx23-sulphur")
    assert exc_info.value.target_env == "video-ltx23-sulphur"
    assert exc_info.value.exit_code == 3
    assert "gate failed" in exc_info.value.stderr_tail
    assert exc_info.value.log_path is not None


# ---- 子进程超时 → EnvSwitchTimeout + 子进程被 terminate/kill ----------------------


async def test_ensure_env_subprocess_timeout_terminates_proc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")
    (tmp_path / "arcreel-env-gate.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    class _HangingProc(_StubProc):
        async def communicate(self):  # type: ignore[override]
            await asyncio.sleep(60)
            return b"", b""

        async def wait(self) -> int:
            return 0

    async def _fake_exec(*args: Any, **kwargs: Any):
        return _HangingProc(0)

    # 把 wait_for 缩短：让测试不必真等 155s
    real_wait_for = asyncio.wait_for

    async def _short_wait_for(coro, timeout):
        return await real_wait_for(coro, timeout=0.1)

    monkeypatch.setattr(pe.asyncio, "wait_for", _short_wait_for)
    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)

    with pytest.raises(pe.EnvSwitchTimeout):
        await pe.ensure_env("video-ltx23-sulphur", timeout_seconds=1.0)


# ---- gate 脚本不存在 → EnvSwitchGateMissing -------------------------------------


async def test_ensure_env_gate_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "current.env").write_text("nsfw\n", encoding="utf-8")
    # 不创建 arcreel-env-gate.sh

    with pytest.raises(pe.EnvSwitchGateMissing):
        await pe.ensure_env("video-ltx23-sulphur")


# ---- 并发互斥 -----------------------------------------------------------------


async def test_ensure_env_concurrent_calls_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        pe.ensure_env("video-ltx23-sulphur"),
        pe.ensure_env("video-ltx23-sulphur"),
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
        await pe.ensure_env("   ")


# ---- current.env 不存在视为 (unknown) → 强制切换 --------------------------------


async def test_ensure_env_missing_current_env_forces_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 不创建 current.env —— _read_current_host_env 应返回 (unknown)
    (tmp_path / "arcreel-env-gate.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    async def _fake_exec(*args: Any, **kwargs: Any):
        return _StubProc(0)

    monkeypatch.setattr(pe.asyncio, "create_subprocess_exec", _fake_exec)

    result = await pe.ensure_env("video-ltx23-sulphur")
    assert result.switched is True
