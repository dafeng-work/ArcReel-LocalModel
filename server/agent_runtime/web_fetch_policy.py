"""WebFetch 域名安全预检的开关。

claude-code 的 WebFetch 在抓取前先问 ``api.anthropic.com``（``/api/web/domain_info``）
该域名是否在 Anthropic 的内容黑名单里；问不到就拒绝抓取，工具报
``Unable to verify if domain ... is safe to fetch``。该端点写死在 CLI 内部，**不跟随**
``ANTHROPIC_BASE_URL``：推理指向第三方网关时，预检仍然打 Anthropic 官方域名。

把推理指向 MiniMax 网关的部署里，``api.anthropic.com`` 在境内网络不可达，于是每一次
WebFetch 都止步于预检。CLI 自带的 ``skipWebFetchPreflight`` 开关正是为「第三方部署
路由到别处、``api.anthropic.com`` 被防火墙挡掉」准备的：打开后 WebFetch 直接抓取，
不再联系 Anthropic。

跳过预检移除的是 Anthropic 的内容黑名单与「预检不可达即拒」的 fail-closed，不是宿主
网络边界——Agent 早已通过 Bash 沙箱拿到全域与 loopback 出网（``allowedDomains: ["*"]``
加 ``allowLocalBinding: true``，见 ADR 0069），沙箱内 curl 本就能直连宿主任意端口。

需要恢复预检时设 ``ARCREEL_SKIP_WEB_FETCH_PREFLIGHT=0``，代价是 ``api.anthropic.com``
必须可达，否则 WebFetch 整体不可用。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: 服务进程级 env 覆盖。``1/true/yes/on`` = 跳过预检；``0/false/no/off`` = 保留预检。
SKIP_WEB_FETCH_PREFLIGHT_ENV_VAR = "ARCREEL_SKIP_WEB_FETCH_PREFLIGHT"

#: 默认跳过预检：非 Anthropic 网关部署下预检恒失败，留着等于把 WebFetch 整体关掉。
SKIP_WEB_FETCH_PREFLIGHT_DEFAULT = True

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _coerce(raw: str) -> bool | None:
    """把原始字符串解析成 bool；无法判定时返回 ``None``（交由调用方回退默认）。"""
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def skip_web_fetch_preflight(raw: str | None = None) -> bool:
    """是否跳过 WebFetch 的域名安全预检。

    ``raw`` 为 ``None`` 时读服务进程的 :data:`SKIP_WEB_FETCH_PREFLIGHT_ENV_VAR`。
    env 不可判定时回退 :data:`SKIP_WEB_FETCH_PREFLIGHT_DEFAULT` 并告警——拼错的 env
    不该悄悄改变 WebFetch 的可用性。
    """
    if raw is None:
        raw = os.environ.get(SKIP_WEB_FETCH_PREFLIGHT_ENV_VAR)
    if raw is None:
        return SKIP_WEB_FETCH_PREFLIGHT_DEFAULT
    coerced = _coerce(raw)
    if coerced is None:
        logger.warning(
            "%s=%r is not a boolean; falling back to default %s",
            SKIP_WEB_FETCH_PREFLIGHT_ENV_VAR,
            raw,
            SKIP_WEB_FETCH_PREFLIGHT_DEFAULT,
        )
        return SKIP_WEB_FETCH_PREFLIGHT_DEFAULT
    return coerced


def web_fetch_preflight_settings() -> dict[str, bool]:
    """产出注入 CLI settings JSON 的片段。

    保留预检时不写该键：CLI 的默认值就是「跑预检」，写 ``false`` 与不写等价，但多一个
    键就多一处将来要同步的语义。
    """
    if skip_web_fetch_preflight():
        return {"skipWebFetchPreflight": True}
    return {}


__all__ = [
    "SKIP_WEB_FETCH_PREFLIGHT_DEFAULT",
    "SKIP_WEB_FETCH_PREFLIGHT_ENV_VAR",
    "skip_web_fetch_preflight",
    "web_fetch_preflight_settings",
]
