"""Agent 权限模式（auto）的设置解析与优先级 —— 单卡互斥部署下的死锁开关。

**为什么需要这个设置**：claude-code 的 auto 权限模式在**每次工具调用前**会额外发一次
LLM 安全分类请求。该请求与 Agent 主回合共用同一条推理通道（``ANTHROPIC_BASE_URL`` →
网关 → llama-server）。本机 ``env/wide-nsfw.env`` 的 ``PARALLEL=1`` 是单slot，
于是分类请求排在**发起它的那一回合**之后，而那一回合又要等分类结果才继续——
三者闭环，构成自锁死锁。更糟的是 CLI 把超时归类为 ``automode-unavailable``，
其语义原文是「NOT a policy decision; retrying is appropriate」，模型于是无限重试。

**关闭后的权限从哪来**：改用 ``default`` 模式后，工具权限改由 ArcReel 自己的本地链路裁决
——PreToolUse hook（子智能体工具名单 / 文件访问 / JSON 校验）+ sandbox +
``allowed_tools`` allow 规则 + ``can_use_tool`` default-deny 兜底。围栏不变且更确定，
只是把「远端 LLM 分类」换成「本地确定性规则」。

**默认开启的理由**：与上游 ArcReel 行为 100% 一致（上游不设 ``permission_mode``，
CLI 默认即 auto），零回归——与 ``lib.config.dispatcher_schema`` 对 ``execution_mode``
默认 ``multi`` 的取舍同源。本机作为单卡部署需要手动关闭，见设置页警示文字。

**优先级：env > DB 设置 > 默认值**。env 覆盖是给本地 agent 自动化测试用的：功能默认
仍为开启，但测试环境需要在**不修改 DB 设置**的前提下强制关闭 auto，否则测试会因
分类器可达性引入非确定性。该 env 由 **ArcReel 服务进程**读取，**不是**注入 CLI
子进程的 ``options.env``——故不在 ``lib.config.env_keys.OTHER_PROVIDER_ENV_KEYS``。
"""

from __future__ import annotations

import os
from typing import Literal

#: 与 ``claude_agent_sdk.types.PermissionMode`` 结构相同的取值集合。
#:
#: **本地重述而非直接 import SDK 的理由**：配置解析模块被 router / 配置读侧多处 import，
#: 让它硬依赖 ``claude_agent_sdk`` 会把「SDK 是否装好」变成整个设置面能否加载的前提。
#: 结构相同的 Literal 对 basedpyright 足够，且由 ``test_agent_auto_mode.py`` 断言两者
#: 取值集合一致——SDK 升版新增/改名取值时会立刻失败，不会静默漂移。
PermissionModeValue = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]

#: ``system_settings`` 中的键名。
AGENT_AUTO_MODE_SETTING = "agent_auto_mode"

#: 功能默认值。True = 开启 auto 权限模式（与上游一致）；关闭走 ``default`` 模式。
AGENT_AUTO_MODE_DEFAULT = True

#: 服务进程级 env 覆盖。设置它即可压过 DB 设置，仅为本地 agent 自动化测试与部署适配存在。
AGENT_AUTO_MODE_ENV_VAR = "ARCREEL_AGENT_AUTO_MODE"

#: 真值 / 假值的接受写法（照抄既有 bool 设置的宽松口径，:400 `video_generate_audio` 同源）。
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _coerce(raw: str) -> bool | None:
    """把一个原始字符串解析成 bool；无法判定时返回 ``None``（交由调用方决定回退）。"""
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def parse_agent_auto_mode(raw: str | None) -> bool:
    """解析 DB 里的设置值；**缺省或不可判定时回退** :data:`AGENT_AUTO_MODE_DEFAULT`。

    DB 里没有该行 = 用户没配过 = 保持上游行为（auto 开启），不是「关闭」。
    """
    if raw is None:
        return AGENT_AUTO_MODE_DEFAULT
    coerced = _coerce(raw)
    if coerced is None:
        return AGENT_AUTO_MODE_DEFAULT
    return coerced


def env_override() -> bool | None:
    """读 :data:`AGENT_AUTO_MODE_ENV_VAR`；未设置返回 ``None``，不参与裁决。

    不可判定的值也返回 ``None``（而不是当作 False）：一个拼错的 env 不该悄悄
    改变权限语义，最安全的读法是忽略它并保留 DB / 默认行为。
    """
    raw = os.environ.get(AGENT_AUTO_MODE_ENV_VAR)
    if raw is None:
        return None
    return _coerce(raw)


def resolve_agent_auto_mode(db_raw: str | None) -> bool:
    """最终裁决：env 覆盖 > DB 设置 > :data:`AGENT_AUTO_MODE_DEFAULT`。"""
    override = env_override()
    if override is not None:
        return override
    return parse_agent_auto_mode(db_raw)


def permission_mode_for(auto_enabled: bool) -> PermissionModeValue:
    """把 auto 开关映射成 SDK ``permission_mode`` 取值。

    ``auto`` 在 ``claude_agent_sdk.types.PermissionMode`` 的 Literal 名单内；SDK 只对
    ``bypassPermissions`` 警告「can_use_tool 不会被调用」，``default`` 正常落到
    ``can_use_tool`` default-deny 兜底——这正是本机要的：本地确定性裁决，不经 LLM。
    """
    return "auto" if auto_enabled else "default"


__all__ = [
    "AGENT_AUTO_MODE_DEFAULT",
    "AGENT_AUTO_MODE_ENV_VAR",
    "AGENT_AUTO_MODE_SETTING",
    "PermissionModeValue",
    "env_override",
    "parse_agent_auto_mode",
    "permission_mode_for",
    "resolve_agent_auto_mode",
]
