"""WebFetch 预检开关单元测试：env 覆盖、默认值与 settings 片段。

开关的取舍写在 ``server.agent_runtime.web_fetch_policy`` 的模块说明里：预检打的是
Anthropic 官方域名，不跟随 ``ANTHROPIC_BASE_URL``，非 Anthropic 网关部署下恒失败。
"""

from __future__ import annotations

import pytest

from server.agent_runtime.web_fetch_policy import (
    SKIP_WEB_FETCH_PREFLIGHT_DEFAULT,
    SKIP_WEB_FETCH_PREFLIGHT_ENV_VAR,
    skip_web_fetch_preflight,
    web_fetch_preflight_settings,
)


def test_skip_web_fetch_preflight_defaults_to_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env 未设置时按默认跳过预检。"""
    monkeypatch.delenv(SKIP_WEB_FETCH_PREFLIGHT_ENV_VAR, raising=False)
    assert skip_web_fetch_preflight() == SKIP_WEB_FETCH_PREFLIGHT_DEFAULT
    assert skip_web_fetch_preflight() is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("No", False),
        ("off", False),
    ],
)
def test_skip_web_fetch_preflight_reads_env_override(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    """env 用宽松口径解析真值与假值。"""
    monkeypatch.setenv(SKIP_WEB_FETCH_PREFLIGHT_ENV_VAR, raw)
    assert skip_web_fetch_preflight() is expected


def test_skip_web_fetch_preflight_falls_back_on_unparsable_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拼错的 env 回退默认值，不悄悄改变 WebFetch 的可用性。"""
    monkeypatch.setenv(SKIP_WEB_FETCH_PREFLIGHT_ENV_VAR, "maybe")
    assert skip_web_fetch_preflight() is SKIP_WEB_FETCH_PREFLIGHT_DEFAULT


def test_web_fetch_preflight_settings_sets_key_when_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跳过预检时写 CLI 的 flatKey。"""
    monkeypatch.setenv(SKIP_WEB_FETCH_PREFLIGHT_ENV_VAR, "1")
    assert web_fetch_preflight_settings() == {"skipWebFetchPreflight": True}


def test_web_fetch_preflight_settings_omits_key_when_preflight_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保留预检时不写该键：CLI 的默认值就是「跑预检」。"""
    monkeypatch.setenv(SKIP_WEB_FETCH_PREFLIGHT_ENV_VAR, "0")
    assert web_fetch_preflight_settings() == {}
