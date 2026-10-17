"""``server.agent_toolset.embedded`` 的 G3 单卡互斥 env 闸门测试。

覆盖 post-tool 恢复（:func:`_restore_text_env_after_tool`）的**四条短路条件**与
**失败语义**——后者是本改动最容易做错的地方：切回失败一旦转成 tool error，
Agent 会重试，而重试等于重跑整批 GPU 生成。

全部用 monkeypatch 打掉 ``embedded`` 模块命名空间里的依赖，不触达真实 I/O。
"""

from __future__ import annotations

from typing import Any

import pytest

from lib.infra.env_preflight import EnvSwitchBusy, EnvSwitchFailed
from server.agent_toolset import embedded as em

pytestmark = pytest.mark.unit


def _settings(**overrides: str) -> dict[str, str]:
    """构造一份 system_settings。默认：exclusive + 开关关 + 文本→brain、图片→image。"""
    base = {
        "execution_mode": "exclusive",
        "post_tool_env_restore": "true",
        "env_mapping": (
            '{"episode_plan":"wide-nsfw","script_plan_content":"wide-nsfw",'
            '"asset_sheets":"image-qwen21","video":"video-ltx23-sulphur"}'
        ),
    }
    base.update(overrides)
    return base


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: dict[str, str],
    current: str,
    step: str | None,
) -> list[str]:
    """接好全部 seam，返回记录 ``ensure_env`` 调用目标的列表。"""
    calls: list[str] = []

    async def _load() -> dict[str, str]:
        return settings

    async def _ensure_env(target: str, **_: Any) -> None:
        calls.append(target)

    async def _resolve(*, read_settings_async: Any = None) -> str:
        return "wide-nsfw"

    monkeypatch.setattr(em, "_load_execution_settings", _load)
    monkeypatch.setattr(em, "ensure_env", _ensure_env)
    monkeypatch.setattr(em, "resolve_text_env_async", _resolve)
    monkeypatch.setattr(em, "step_for_tool", lambda _name: step)
    monkeypatch.setattr(em, "read_current_host_env", lambda _path=None: current)
    return calls


class TestShortCircuits:
    """四条短路条件 —— 缺一条就会引入无谓的 30–90s 切换或跨模式回归。"""

    @pytest.mark.asyncio
    async def test_multi_mode_never_switches(self, monkeypatch):
        calls = _install(
            monkeypatch,
            settings=_settings(execution_mode="multi"),
            current="video-ltx23-sulphur",
            step="video",
        )
        assert await em._restore_text_env_after_tool("generate_videos") is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_gate_off_by_default_never_switches(self, monkeypatch):
        """开关缺省即关闭 —— 与上游零回归，DB 没配过不等于开启。"""
        settings = _settings()
        settings.pop("post_tool_env_restore")
        calls = _install(monkeypatch, settings=settings, current="video-ltx23-sulphur", step="video")
        assert await em._restore_text_env_after_tool("generate_videos") is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_tool_without_env_mapping_never_switches(self, monkeypatch):
        """``step_for_tool`` 返回 None = 该工具本来就不切 env，无需恢复。"""
        calls = _install(monkeypatch, settings=_settings(), current="image-qwen21", step=None)
        assert await em._restore_text_env_after_tool("read_source_text") is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_same_env_as_brain_never_switches(self, monkeypatch):
        """连续文本阶段：工具 env 与 brain env 相同，切了也是白切 30–90s。"""
        calls = _install(monkeypatch, settings=_settings(), current="image-qwen21", step="episode_plan")
        assert await em._restore_text_env_after_tool("plan_episodes") is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_already_at_brain_env_never_switches(self, monkeypatch):
        """没切走过就无需恢复。"""
        calls = _install(monkeypatch, settings=_settings(), current="wide-nsfw", step="video")
        assert await em._restore_text_env_after_tool("generate_videos") is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_skipped_while_another_tool_is_in_flight(self, monkeypatch):
        """并发闸：同一回合并发发出的工具里，先完成的那个不得换掉另一个的 env。

        embedded 路径下 GPU 工具是「入队 + 阻塞等批次终态」，两个不同 env 的生成
        可能同时在跑；此时切走 env 等于杀掉在途生成，而这种误杀无法兜底。
        """
        calls = _install(monkeypatch, settings=_settings(), current="image-qwen21", step="video")
        monkeypatch.setattr(em, "_INFLIGHT_TOOLS", 1)
        assert await em._restore_text_env_after_tool("generate_videos") is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_inflight_counter_returns_to_zero_after_call(self, monkeypatch):
        """计数不得泄漏——泄漏会让恢复被永久跳过。"""
        assert em._INFLIGHT_TOOLS == 0
        calls = _install(monkeypatch, settings=_settings(), current="image-qwen21", step="video")
        monkeypatch.setattr(em, "_INFLIGHT_TOOLS", 0)
        await em._restore_text_env_after_tool("generate_videos")
        assert em._INFLIGHT_TOOLS == 0
        assert calls == ["wide-nsfw"]


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_switches_back_to_brain_env_after_gpu_tool(self, monkeypatch):
        calls = _install(monkeypatch, settings=_settings(), current="video-ltx23-sulphur", step="video")
        assert await em._restore_text_env_after_tool("generate_videos") is None
        assert calls == ["wide-nsfw"]

    @pytest.mark.asyncio
    async def test_switches_back_after_image_tool(self, monkeypatch):
        calls = _install(monkeypatch, settings=_settings(), current="image-qwen21", step="asset_sheets")
        assert await em._restore_text_env_after_tool("generate_assets") is None
        assert calls == ["wide-nsfw"]


class TestFailureSemantics:
    """失败语义：本函数**永不抛错**。抛出等于让 Agent 重试并重跑 GPU 生成。"""

    @pytest.mark.asyncio
    async def test_retries_once_then_succeeds(self, monkeypatch):
        calls: list[str] = []
        attempts = {"n": 0}

        async def _flaky(target: str, **_: Any) -> None:
            calls.append(target)
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise EnvSwitchFailed(target, 1, "transient")

        _install(monkeypatch, settings=_settings(), current="image-qwen21", step="video")
        monkeypatch.setattr(em, "ensure_env", _flaky)

        assert await em._restore_text_env_after_tool("generate_videos") is None
        assert calls == ["wide-nsfw", "wide-nsfw"]

    @pytest.mark.asyncio
    async def test_gives_up_after_second_failure_without_raising(self, monkeypatch):
        calls: list[str] = []

        async def _always_fail(target: str, **_: Any) -> None:
            calls.append(target)
            raise EnvSwitchFailed(target, 1, "boom")

        _install(monkeypatch, settings=_settings(), current="image-qwen21", step="video")
        monkeypatch.setattr(em, "ensure_env", _always_fail)

        # 返回诊断码而非抛出：上层只记日志，tool outcome 仍是成功结果。
        assert await em._restore_text_env_after_tool("generate_videos") == "post_tool_env_restore_failed"
        assert calls == ["wide-nsfw", "wide-nsfw"]

    @pytest.mark.asyncio
    async def test_busy_does_not_retry(self, monkeypatch):
        """锁忙 = 另一个批次占着 GPU，立刻重试只会再撞同一把锁。"""
        calls: list[str] = []

        async def _busy(target: str, **_: Any) -> None:
            calls.append(target)
            raise EnvSwitchBusy(target)

        _install(monkeypatch, settings=_settings(), current="image-qwen21", step="video")
        monkeypatch.setattr(em, "ensure_env", _busy)

        assert await em._restore_text_env_after_tool("generate_videos") == "post_tool_env_restore_busy"
        assert calls == ["wide-nsfw"], "忙路径不应重试"

    @pytest.mark.asyncio
    async def test_unexpected_exception_does_not_escape(self, monkeypatch):
        async def _boom(target: str, **_: Any) -> None:
            raise RuntimeError("unexpected")

        _install(monkeypatch, settings=_settings(), current="image-qwen21", step="video")
        monkeypatch.setattr(em, "ensure_env", _boom)

        assert await em._restore_text_env_after_tool("generate_videos") == "post_tool_env_restore_failed"

    @pytest.mark.asyncio
    async def test_settings_read_failure_bypasses(self, monkeypatch):
        """读不到 settings 就旁路，绝不因为闸门把工具调用搞挂。"""

        async def _fail() -> None:
            return None

        monkeypatch.setattr(em, "_load_execution_settings", _fail)
        monkeypatch.setattr(em, "step_for_tool", lambda _name: "video")
        assert await em._restore_text_env_after_tool("generate_videos") is None


class TestPreflightUnchanged:
    """preflight 侧行为不得因本次重构（抽出 _load_execution_settings）而漂移。"""

    @pytest.mark.asyncio
    async def test_preflight_still_switches_for_mismatched_env(self, monkeypatch):
        calls = _install(monkeypatch, settings=_settings(), current="wide-nsfw", step="video")
        assert await em._preflight_for_tool("generate_videos") is None
        assert calls == ["video-ltx23-sulphur"]

    @pytest.mark.asyncio
    async def test_preflight_reports_failure_as_error_code(self, monkeypatch):
        async def _fail(target: str, **_: Any) -> None:
            raise EnvSwitchFailed(target, 1, "boom")

        _install(monkeypatch, settings=_settings(), current="wide-nsfw", step="video")
        monkeypatch.setattr(em, "ensure_env", _fail)
        assert await em._preflight_for_tool("generate_videos") == "env_switch_failed"
