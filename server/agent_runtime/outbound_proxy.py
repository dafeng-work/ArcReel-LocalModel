"""内嵌 Agent 的出站代理：来源解析、端点探测、CLI 子进程 env 覆盖。

沙箱内 Bash 的出站默认直连，被墙域名 TCP 超时，Agent 因此拿不到外部资料。claude-code
自带 HTTP 代理 mux：CLI 进程 env 里的 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 是 mux 的上游，
沙箱内的 curl 经 mux 转交给它。sandbox settings 的 ``network.httpProxyPort`` 是另一条
旁路，直接把沙箱流量桥到本机代理端口，不经 mux；本模块走 mux 那条，因为它同时覆盖
CLI 自身的 WebFetch 抓取。

本模块只决定「走不走代理」，不决定「能访问哪些域名」——域名边界在
``agent_access_policy.build_sandbox_settings`` 的 ``network.allowedDomains``。

**为什么先探测再注入**：宿主可能配了代理地址却没有代理进程在跑。死代理会让 WebFetch
与沙箱出站一起失败（``CONNECT tunnel failed, response 502``），比直连超时更难定位。
端点不可达时把代理变量置空，CLI 侧按「无代理」处理，回退直连。

**边界**：代理 URL 的 userinfo 随 CLI 子进程 env 进入沙箱 Bash 可见范围，代理口令不要
写进 ``ARCREEL_AGENT_PROXY_URL``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Agent 专用代理 URL。设置后压过标准代理 env。
AGENT_PROXY_URL_ENV_VAR = "ARCREEL_AGENT_PROXY_URL"

#: 追加到 no_proxy 的条目（逗号分隔）。
AGENT_PROXY_NO_PROXY_ENV_VAR = "ARCREEL_AGENT_NO_PROXY"

#: 标准代理 env 的读取顺序。
PROXY_URL_ENV_VARS: tuple[str, ...] = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)

#: 注入 CLI 子进程的代理变量名。大小写都给：curl 认小写，CLI 的 HTTP 客户端认大写。
PROXY_ENV_KEYS: tuple[str, ...] = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
)

#: 本地地址默认绕过代理：同机的推理服务、ComfyUI 与 ArcReel 自身 API 走代理会被劫持成
#: 不可达。
DEFAULT_NO_PROXY_HOSTS: tuple[str, ...] = (
    "127.0.0.1",
    "localhost",
    "::1",
    "*.local",
    "host.docker.internal",
)

#: 端点探测超时（秒）。本机代理端口未监听时立即拒绝，不耗满超时。
PROXY_PROBE_TIMEOUT_SECONDS = 1.0

#: socks 类 scheme：node 的 HTTP 客户端不会说 socks 协议，只给 curl 一类工具用。
_SOCKS_SCHEMES = frozenset({"socks5", "socks5h", "socks4"})

#: 无端口时按 scheme 取默认端口。
_DEFAULT_PORTS: dict[str, int] = {
    "http": 80,
    "https": 443,
    "socks5": 1080,
    "socks5h": 1080,
    "socks4": 1080,
}


def resolve_proxy_url(env: Mapping[str, str] | None = None) -> str | None:
    """解析代理 URL：``ARCREEL_AGENT_PROXY_URL`` 优先，其次标准代理 env。

    没有候选时返回 ``None``——调用方据此判定「宿主没有配代理」，而不是「配了个空的
    代理」。
    """
    source = os.environ if env is None else env
    explicit = source.get(AGENT_PROXY_URL_ENV_VAR, "").strip()
    if explicit:
        return explicit
    for key in PROXY_URL_ENV_VARS:
        value = source.get(key, "").strip()
        if value:
            return value
    return None


def _parse_proxy_url(url: str) -> tuple[str, tuple[str, int]] | None:
    """解析代理 URL，返回 ``(scheme, host:port)``；不合法时返回 ``None``。

    scheme 与端点一次解析出：判定「能不能用代理」与判定「代理走哪种协议」是同一件事，
    分两次解析会让两者对同一个 URL 的判断各走一遍。
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None
    scheme = parsed.scheme.lower()
    if port is None:
        port = _DEFAULT_PORTS.get(scheme, 0)
    if not port:
        return None
    return scheme, (host, port)


def proxy_endpoint(url: str) -> tuple[str, int] | None:
    """从代理 URL 解析 ``host:port``；无端口时按 scheme 取默认端口。

    URL 不合法时返回 ``None``——调用方据此判定「这个代理用不了」。
    """
    parsed = _parse_proxy_url(url)
    return None if parsed is None else parsed[1]


def is_endpoint_reachable(endpoint: tuple[str, int], *, timeout: float = PROXY_PROBE_TIMEOUT_SECONDS) -> bool:
    """TCP 可连探测：只确认端口有人接，不发任何代理协议字节。

    只判端口可达，不判代理是否可用——代理进程在但转发规则拒绝某域名时，这里仍是通的。
    """
    host, port = endpoint
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _append_no_proxy_entry(entries: list[str], seen: set[str], item: str) -> None:
    """往 no_proxy 列表追加一条，忽略空项与重复项（大小写不敏感）。"""
    item = item.strip()
    if not item:
        return
    key = item.lower()
    if key in seen:
        return
    seen.add(key)
    entries.append(item)


def merge_no_proxy(env: Mapping[str, str], extra_no_proxy: Sequence[str] = ()) -> str:
    """合并 no_proxy 条目。

    宿主 env 的既有条目在前（保留操作者的显式设置），随后是额外条目，最后是本地地址
    默认项——本地地址默认绕过代理这条不能被操作者的条目挤掉。
    """
    entries: list[str] = []
    seen: set[str] = set()
    for raw in (env.get("no_proxy", ""), env.get("NO_PROXY", "")):
        for item in raw.split(","):
            _append_no_proxy_entry(entries, seen, item)
    for item in extra_no_proxy:
        _append_no_proxy_entry(entries, seen, item)
    for item in DEFAULT_NO_PROXY_HOSTS:
        _append_no_proxy_entry(entries, seen, item)
    return ",".join(entries)


def build_proxy_env_overrides(
    *,
    env: Mapping[str, str] | None = None,
    extra_no_proxy: Sequence[str] = (),
    probe: Callable[[tuple[str, int]], bool] = is_endpoint_reachable,
) -> dict[str, str]:
    """产出注入 CLI 子进程的代理 env 覆盖。

    - 没有候选代理：返回空字典，不动宿主 env。
    - 候选端点可达：代理变量统一指向该 URL，no_proxy 给出合并后的条目。
    - 候选端点不可达（或 URL 不合法）：代理变量置空，CLI 侧按无代理处理，回退直连。

    置空而非删除，是因为 SDK 把 ``options.env`` 叠加在继承来的进程 env 之上，删键不会
    撤销继承值；空串则让 CLI 与 curl 都按「未设置」处理。
    """
    source = os.environ if env is None else env
    url = resolve_proxy_url(source)
    if url is None:
        return {}
    parsed = _parse_proxy_url(url)
    if parsed is None or not probe(parsed[1]):
        logger.warning("agent outbound proxy %s is unreachable; falling back to direct connection", url)
        return dict.fromkeys(PROXY_ENV_KEYS, "")

    scheme = parsed[0]
    overrides = dict.fromkeys(PROXY_ENV_KEYS, "")
    if scheme in _SOCKS_SCHEMES:
        # socks 代理只给 curl 一类工具用：CLI 自身的 HTTP 客户端不会说 socks 协议，
        # 写进 HTTP_PROXY 会让它的请求直接失败。
        overrides["ALL_PROXY"] = url
        overrides["all_proxy"] = url
    else:
        overrides["http_proxy"] = url
        overrides["https_proxy"] = url
        overrides["HTTP_PROXY"] = url
        overrides["HTTPS_PROXY"] = url
        overrides["ALL_PROXY"] = url
        overrides["all_proxy"] = url
    extra = [item for item in source.get(AGENT_PROXY_NO_PROXY_ENV_VAR, "").split(",") if item.strip()]
    extra.extend(extra_no_proxy)
    no_proxy = merge_no_proxy(source, extra)
    overrides["no_proxy"] = no_proxy
    overrides["NO_PROXY"] = no_proxy
    return overrides


async def resolve_proxy_env_overrides(
    *,
    env: Mapping[str, str] | None = None,
    extra_no_proxy: Sequence[str] = (),
    probe: Callable[[tuple[str, int]], bool] = is_endpoint_reachable,
) -> dict[str, str]:
    """异步封装：端点探测是阻塞 socket 调用，放 worker 线程，不占事件循环。"""
    return await asyncio.to_thread(
        build_proxy_env_overrides,
        env=env,
        extra_no_proxy=extra_no_proxy,
        probe=probe,
    )


__all__ = [
    "AGENT_PROXY_NO_PROXY_ENV_VAR",
    "AGENT_PROXY_URL_ENV_VAR",
    "DEFAULT_NO_PROXY_HOSTS",
    "PROXY_ENV_KEYS",
    "PROXY_PROBE_TIMEOUT_SECONDS",
    "PROXY_URL_ENV_VARS",
    "build_proxy_env_overrides",
    "is_endpoint_reachable",
    "merge_no_proxy",
    "proxy_endpoint",
    "resolve_proxy_env_overrides",
    "resolve_proxy_url",
]
