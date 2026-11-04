"""出站代理模块单元测试：来源解析、端点探测、env 覆盖的产出与回退。

端点探测是本模块唯一的 I/O，测试分两路覆盖：真实 socket（真监听端口 vs 真关闭端口）
验证探测本身；注入 probe 替身验证「可达 / 不可达」两条分支的产出。断言落在返回的
env 覆盖字典上，不落在替身的调用记录上。
"""

from __future__ import annotations

import socket
from collections.abc import Generator
from contextlib import contextmanager

import pytest

from server.agent_runtime.outbound_proxy import (
    AGENT_PROXY_NO_PROXY_ENV_VAR,
    AGENT_PROXY_URL_ENV_VAR,
    DEFAULT_NO_PROXY_HOSTS,
    PROXY_ENV_KEYS,
    build_proxy_env_overrides,
    is_endpoint_reachable,
    merge_no_proxy,
    proxy_endpoint,
    resolve_proxy_env_overrides,
    resolve_proxy_url,
)


@contextmanager
def _listening_port() -> Generator[tuple[str, int]]:
    """借一个真在监听的本地端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield sock.getsockname()


@contextmanager
def _bound_not_listening_port() -> Generator[tuple[str, int]]:
    """借一个已绑定但未监听的本地端口。

    连接被内核立即拒绝（``ECONNREFUSED``），且端口被这个 socket 占住——不会被别的
    进程抢走，探测结果不依赖时序。用例只连自己进程内的端口，不依赖任何外部服务。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        yield sock.getsockname()


def test_resolve_proxy_url_prefers_agent_specific_env() -> None:
    """``ARCREEL_AGENT_PROXY_URL`` 压过标准代理 env。"""
    env = {
        AGENT_PROXY_URL_ENV_VAR: "http://127.0.0.1:18080",
        "HTTPS_PROXY": "http://127.0.0.1:28080",
    }
    assert resolve_proxy_url(env) == "http://127.0.0.1:18080"


def test_resolve_proxy_url_falls_back_to_standard_env() -> None:
    """没有 agent 专用值时按标准 env 顺序取。"""
    assert resolve_proxy_url({"HTTPS_PROXY": "http://127.0.0.1:28080"}) == "http://127.0.0.1:28080"
    assert resolve_proxy_url({"http_proxy": "http://127.0.0.1:38080"}) == "http://127.0.0.1:38080"
    assert resolve_proxy_url({"ALL_PROXY": "socks5://127.0.0.1:48080"}) == "socks5://127.0.0.1:48080"


def test_resolve_proxy_url_returns_none_without_candidate() -> None:
    """宿主没有任何代理配置时返回 ``None``——与「配了个空代理」不同。"""
    assert resolve_proxy_url({}) is None
    assert resolve_proxy_url({"HTTPS_PROXY": "  "}) is None


def test_proxy_endpoint_uses_scheme_default_port() -> None:
    """URL 不带端口时按 scheme 取默认端口。"""
    assert proxy_endpoint("http://proxy.example") == ("proxy.example", 80)
    assert proxy_endpoint("https://proxy.example") == ("proxy.example", 443)
    assert proxy_endpoint("socks5://127.0.0.1:1081") == ("127.0.0.1", 1081)


def test_proxy_endpoint_rejects_unparsable_url() -> None:
    """拿不出 host:port 的 URL 判为不可用。"""
    assert proxy_endpoint("not-a-url") is None
    assert proxy_endpoint("http://") is None
    assert proxy_endpoint("http://proxy.example:not-a-port") is None


def test_is_endpoint_reachable_detects_listening_and_closed_ports() -> None:
    """探测只看端口有没有人接。"""
    with _listening_port() as endpoint:
        assert is_endpoint_reachable(endpoint) is True
    with _bound_not_listening_port() as endpoint:
        assert is_endpoint_reachable(endpoint) is False


def test_build_proxy_env_overrides_returns_empty_without_candidate() -> None:
    """没有候选代理时不产出任何覆盖，宿主 env 原样保留。"""
    assert build_proxy_env_overrides(env={}) == {}


def test_build_proxy_env_overrides_injects_proxy_when_reachable() -> None:
    """端点可达：代理变量统一指向该 URL，no_proxy 带上本地地址默认项。"""
    overrides = build_proxy_env_overrides(
        env={AGENT_PROXY_URL_ENV_VAR: "http://127.0.0.1:18080"},
        probe=lambda _endpoint: True,
    )

    assert overrides["HTTP_PROXY"] == "http://127.0.0.1:18080"
    assert overrides["HTTPS_PROXY"] == "http://127.0.0.1:18080"
    assert overrides["http_proxy"] == "http://127.0.0.1:18080"
    assert overrides["https_proxy"] == "http://127.0.0.1:18080"
    assert overrides["ALL_PROXY"] == "http://127.0.0.1:18080"
    for host in DEFAULT_NO_PROXY_HOSTS:
        assert host in overrides["no_proxy"].split(",")
        assert host in overrides["NO_PROXY"].split(",")


def test_build_proxy_env_overrides_falls_back_to_direct_when_unreachable() -> None:
    """端点不可达：代理变量置空，CLI 侧按无代理处理，回退直连。"""
    overrides = build_proxy_env_overrides(
        env={AGENT_PROXY_URL_ENV_VAR: "http://127.0.0.1:18080"},
        probe=lambda _endpoint: False,
    )

    assert set(overrides) == set(PROXY_ENV_KEYS)
    assert all(value == "" for value in overrides.values())


def test_build_proxy_env_overrides_falls_back_to_direct_for_unparsable_url() -> None:
    """URL 拿不出 host:port 时同样回退直连，不去探测。"""
    overrides = build_proxy_env_overrides(
        env={AGENT_PROXY_URL_ENV_VAR: "not-a-url"},
        probe=lambda _endpoint: pytest.fail("不合法 URL 不该进入探测"),
    )

    assert all(value == "" for value in overrides.values())


def test_build_proxy_env_overrides_keeps_socks_proxy_out_of_http_vars() -> None:
    """socks 代理只进 ``ALL_PROXY``：CLI 的 HTTP 客户端不会说 socks 协议。"""
    overrides = build_proxy_env_overrides(
        env={AGENT_PROXY_URL_ENV_VAR: "socks5://127.0.0.1:1081"},
        probe=lambda _endpoint: True,
    )

    assert overrides["ALL_PROXY"] == "socks5://127.0.0.1:1081"
    assert overrides["all_proxy"] == "socks5://127.0.0.1:1081"
    assert overrides["HTTP_PROXY"] == ""
    assert overrides["HTTPS_PROXY"] == ""


def test_build_proxy_env_overrides_merges_inherited_and_extra_no_proxy() -> None:
    """no_proxy 合并宿主既有条目、额外条目与本地地址默认项，且不重复。"""
    overrides = build_proxy_env_overrides(
        env={
            AGENT_PROXY_URL_ENV_VAR: "http://127.0.0.1:18080",
            "no_proxy": "corp.example,127.0.0.1",
            "NO_PROXY": "CORP.example",
            AGENT_PROXY_NO_PROXY_ENV_VAR: "internal.example",
        },
        probe=lambda _endpoint: True,
    )

    entries = overrides["no_proxy"].split(",")
    assert entries[0] == "corp.example"
    assert "internal.example" in entries
    assert "localhost" in entries
    assert len([item for item in entries if item.lower() == "corp.example"]) == 1
    assert len([item for item in entries if item.lower() == "127.0.0.1"]) == 1


def test_merge_no_proxy_keeps_local_hosts_even_when_env_overrides() -> None:
    """宿主把 no_proxy 写死成别的条目时，本地地址默认项仍在。"""
    merged = merge_no_proxy({"no_proxy": "only.example"}).split(",")
    assert merged[0] == "only.example"
    for host in DEFAULT_NO_PROXY_HOSTS:
        assert host in merged


def test_build_proxy_env_overrides_probes_parsed_endpoint() -> None:
    """探测拿到的是解析后的 host:port，不是 URL 原文。"""
    seen: list[tuple[str, int]] = []

    def _probe(endpoint: tuple[str, int]) -> bool:
        seen.append(endpoint)
        return True

    build_proxy_env_overrides(
        env={AGENT_PROXY_URL_ENV_VAR: "http://127.0.0.1:18080"},
        probe=_probe,
    )

    assert seen == [("127.0.0.1", 18080)]


async def test_resolve_proxy_env_overrides_matches_sync_builder() -> None:
    """异步封装只做线程搬运，产出的覆盖与同步构造一致。"""
    overrides = await resolve_proxy_env_overrides(
        env={AGENT_PROXY_URL_ENV_VAR: "http://127.0.0.1:18080"},
        probe=lambda _endpoint: True,
    )

    assert overrides["HTTPS_PROXY"] == "http://127.0.0.1:18080"
    assert "127.0.0.1" in overrides["no_proxy"].split(",")


def test_build_proxy_env_overrides_accepts_real_reachable_endpoint() -> None:
    """走真实探测：真监听的端口判为可达，绑定未监听的端口判为不可达。"""
    with _listening_port() as endpoint:
        overrides = build_proxy_env_overrides(
            env={AGENT_PROXY_URL_ENV_VAR: f"http://127.0.0.1:{endpoint[1]}"},
        )
    assert overrides["HTTPS_PROXY"] == f"http://127.0.0.1:{endpoint[1]}"

    with _bound_not_listening_port() as endpoint:
        overrides = build_proxy_env_overrides(
            env={AGENT_PROXY_URL_ENV_VAR: f"http://127.0.0.1:{endpoint[1]}"},
        )
    assert overrides["HTTPS_PROXY"] == ""
