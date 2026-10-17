"""``agent_auto_mode`` 解析与优先级：env 覆盖 > DB 设置 > 默认开启。"""

from __future__ import annotations

from typing import get_args

import pytest

from lib.config.agent_auto_mode import (
    AGENT_AUTO_MODE_DEFAULT,
    AGENT_AUTO_MODE_ENV_VAR,
    PermissionModeValue,
    env_override,
    parse_agent_auto_mode,
    permission_mode_for,
    resolve_agent_auto_mode,
)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例的默认前提：env 未设置，避免宿主环境干扰。

    autouse 而非逐例注入——本机部署按 A4 约定常驻 ``ARCREEL_AGENT_AUTO_MODE=0``，
    不隔离的话「默认开启」这批断言在开发机上会全部反向失败。用例内的 ``setenv``
    发生在本 fixture 之后，故仍可覆盖。
    """
    monkeypatch.delenv(AGENT_AUTO_MODE_ENV_VAR, raising=False)


def test_default_is_on_to_match_upstream() -> None:
    """缺省开启：与上游 ArcReel 行为一致，零回归。"""
    assert AGENT_AUTO_MODE_DEFAULT is True
    assert parse_agent_auto_mode(None) is True
    assert parse_agent_auto_mode("") is True
    assert resolve_agent_auto_mode(None) is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "FALSE", " Off "])
def test_db_falsey_values(raw: str) -> None:
    assert parse_agent_auto_mode(raw) is False


@pytest.mark.parametrize("raw", ["true", "1", "yes", "on", "TRUE", " On "])
def test_db_truthy_values(raw: str) -> None:
    assert parse_agent_auto_mode(raw) is True


def test_unparsable_db_value_falls_back_to_default() -> None:
    """拼错的 DB 值按「没配过」处理，保持上游行为，而不是猜一个语义。"""
    assert parse_agent_auto_mode("maybe") is True


def test_db_false_wins_when_env_unset() -> None:
    assert resolve_agent_auto_mode("false") is False


@pytest.mark.parametrize("env_value", ["0", "false", "no", "off", "FALSE"])
def test_env_override_beats_db_true(monkeypatch: pytest.MonkeyPatch, env_value: str) -> None:
    """A4 的核心约定：本地 agent 自动化测试不改 DB 也能强制关闭 auto。"""
    monkeypatch.setenv(AGENT_AUTO_MODE_ENV_VAR, env_value)
    assert resolve_agent_auto_mode("true") is False


@pytest.mark.parametrize("env_value", ["1", "true", "yes", "on"])
def test_env_override_can_also_force_on(monkeypatch: pytest.MonkeyPatch, env_value: str) -> None:
    monkeypatch.setenv(AGENT_AUTO_MODE_ENV_VAR, env_value)
    assert resolve_agent_auto_mode("false") is True


def test_unparsable_env_is_ignored_not_treated_as_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拼错的 env 忽略它、保留 DB 值 —— 一个笔误不该悄悄改变权限语义。"""
    monkeypatch.setenv(AGENT_AUTO_MODE_ENV_VAR, "maybe")
    assert env_override() is None
    assert resolve_agent_auto_mode("true") is True
    assert resolve_agent_auto_mode("false") is False


def test_env_unset_returns_none() -> None:
    assert env_override() is None


@pytest.mark.parametrize(
    ("auto_enabled", "expected"),
    [(True, "auto"), (False, "default")],
)
def test_permission_mode_mapping(auto_enabled: bool, expected: str) -> None:
    """映射到 SDK ``PermissionMode``（``auto`` 在 Literal 名单内）。"""
    assert permission_mode_for(auto_enabled) == expected


def test_local_literal_matches_sdk_permission_mode() -> None:
    """本地重述的取值集合必须与 SDK 的 ``PermissionMode`` 完全一致。

    ``lib.config.agent_auto_mode`` 刻意不 import SDK（免得「SDK 没装好」变成整个
    设置面加载不了），代价是取值名单有两份。这条断言就是那份代价的对冲：SDK 升版
    新增 / 改名取值时立刻失败，不会静默漂移到 basedpyright 也发现不了的角落。
    """
    from claude_agent_sdk.types import PermissionMode

    assert set(get_args(PermissionModeValue)) == set(get_args(PermissionMode))
    # 两个实际取值都必须是合法成员——不然 basedpyright 会拦，这里先把语义钉住。
    assert permission_mode_for(True) in get_args(PermissionMode)
    assert permission_mode_for(False) in get_args(PermissionMode)
