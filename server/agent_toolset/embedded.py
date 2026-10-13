"""ArcReel Agent（内嵌 Claude Agent SDK）adapter。

项目由会话决定，schema 不含 ``project``；无 scope 声明不接会话项目。结构化结果以 JSON 文本块写进 content、
排在摘要之后，不依赖 ``structuredContent`` 是否会被转给模型；图片块（如有）排在最后。

内嵌 server 关闭 MCP 层的 inputSchema 预校验：已声明工具的参数一律由请求模型校验，
坏参数与远程宿主一样得到 ``invalid_request`` problem，而不是 MCP 的纯文本校验错误。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from claude_agent_sdk import McpSdkServerConfig
from mcp import types
from mcp.server import Server

from lib.config.dispatcher_schema import effective_env_mapping, parse_execution_mode
from lib.infra.env_preflight import (
    DEFAULT_CURRENT_ENV_PATH,
    EnvSwitchError,
    ensure_env,
    read_current_host_env,
)
from lib.workflow.tool_step_env import step_for_tool
from server.agent_toolset.declaration import (
    AgentToolDeclaration,
    UnscopedToolDeclaration,
    invoke_declaration,
    invoke_unscoped_declaration,
    tool_description,
)
from server.agent_toolset.envelope import encode_outcome
from server.tool_runtime import CallerContext, ProjectScope, Services, ToolOutcome, ToolProblem

logger = logging.getLogger(__name__)


async def _preflight_for_tool(tool_name: str) -> str | None:
    """G3 单机单卡互斥：MCP 工具调用前确保宿主 env 已切到目标；返回错误码或 ``None``。

    不需要 preflight 的工具（``step_for_tool`` 返回 ``None``）直接放行，零切换开销。
    ``execution_mode != "exclusive"`` 也直接放行（与上游 ArcReel 行为一致，零回归）。
    失败时返回稳定的错误码（``env_switch_failed`` / ``env_switch_timeout`` 等），
    由调用方包装成 ``ToolOutcome(problem=ToolProblem(code, ...))`` 返回给 Agent。
    """

    step_id = step_for_tool(tool_name)
    if step_id is None:
        return None

    # 读 system_settings（execution_mode / env_mapping）。失败视为 multi，旁路。
    try:
        from lib.config.service import ConfigService
        from lib.db import async_session_factory

        async with async_session_factory() as session:
            all_settings = await ConfigService(session).get_all_settings()
    except Exception as exc:
        logger.warning("preflight: failed to read system_settings for %s, skipping: %s", tool_name, exc)
        return None

    execution_mode = parse_execution_mode(all_settings.get("execution_mode", ""))
    if execution_mode != "exclusive":
        return None

    env_mapping = effective_env_mapping(all_settings.get("env_mapping", ""))
    target_env = env_mapping.get(step_id)
    if not target_env:
        return None

    current_env = read_current_host_env(DEFAULT_CURRENT_ENV_PATH)
    if current_env == target_env:
        return None

    logger.info(
        "preflight: %s needs env %s (step=%s, current=%s), switching",
        tool_name,
        target_env,
        step_id,
        current_env,
    )
    try:
        await ensure_env(target_env)
    except EnvSwitchError as exc:
        logger.warning("preflight: ensure_env failed for %s: %s", tool_name, exc)
        return "env_switch_failed"
    except Exception:
        logger.exception("preflight: unexpected error in ensure_env for %s", tool_name)
        return "env_switch_failed"
    return None


def embedded_result(declaration: AgentToolDeclaration, outcome: ToolOutcome[Any]) -> types.CallToolResult:
    envelope = encode_outcome(declaration, outcome)
    return types.CallToolResult(
        content=[
            *(types.TextContent(type="text", text=text) for text in envelope.texts),
            *(
                types.ImageContent(type="image", data=image.base64_data, mimeType=image.mime_type)
                for image in envelope.images
            ),
        ],
        isError=envelope.is_error,
    )


def embedded_server(
    declarations: Iterable[AgentToolDeclaration],
    *,
    name: str,
    version: str,
    scope: ProjectScope,
    caller: CallerContext,
    services: Services,
) -> McpSdkServerConfig:
    """以会话项目暴露已声明工具的 in-process MCP server。"""
    declared = {declaration.name: declaration for declaration in declarations}
    listed = [
        types.Tool(
            name=declaration.name,
            description=tool_description(declaration),
            inputSchema=declaration.input_schema,
        )
        for declaration in declared.values()
    ]
    server = Server(name, version=version)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return listed

    @server.call_tool(validate_input=False)
    async def call_tool(tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        declaration = declared.get(tool_name)
        if declaration is None:
            raise ValueError(f"Tool '{tool_name}' not found")

        # G3 单机单卡互斥：调用前确保宿主 env 已切到该工具所属 step 需要的 env。
        # 仅 ``execution_mode == "exclusive"`` 且当前 env 不匹配时触发；否则零开销。
        preflight_error = await _preflight_for_tool(tool_name)
        if preflight_error is not None:
            problem = ToolProblem(
                preflight_error,
                f"tool '{tool_name}' requires a host env switch that failed; "
                f"see /home/dafeng/ai-stack/logs/env_switch_*.log for details, "
                f"or set system_settings.execution_mode='multi' to disable preflight",
            )
            return embedded_result(declaration, ToolOutcome(problem=problem))

        if isinstance(declaration, UnscopedToolDeclaration):
            outcome = await invoke_unscoped_declaration(declaration, arguments, caller, services)
        else:
            outcome = await invoke_declaration(declaration, arguments, scope, caller, services)
        return embedded_result(declaration, outcome)

    return McpSdkServerConfig(type="sdk", name=name, instance=server)


__all__ = ["embedded_result", "embedded_server"]
