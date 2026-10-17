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

from lib.config.dispatcher_schema import (
    POST_TOOL_ENV_RESTORE_SETTING,
    effective_env_mapping,
    parse_execution_mode,
    parse_post_tool_env_restore,
)
from lib.infra.env_preflight import (
    DEFAULT_CURRENT_ENV_PATH,
    EnvSwitchBusy,
    EnvSwitchError,
    ensure_env,
    read_current_host_env,
)
from lib.workflow.tool_step_env import step_for_tool
from server.agent_runtime.llm_preflight import resolve_text_env_async
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


#: 当前进程内**正在执行**的工具调用数（含已返回、尚未做 post-tool 恢复的）。
#:
#: **为什么需要**：claude-code 允许同一回合并发发出多个 tool_use。embedded 路径下
#: GPU 工具是「入队 + 阻塞等批次终态」（``server/tool_runtime.py:265``），所以
#: ``generate_videos`` 与 ``generate_assets`` 可能同时在跑、分属不同 env。若先完成
#: 的那个立刻恢复 brain env，会把另一个**正在生成中**的任务的 env 换掉。
#:
#: 故只在计数归零（确实没有任何工具在跑）时才恢复。保守方向的偏差是「有时不恢复」，
#: 由 ``ensure_llm_ready`` 在下一轮兜底；反向偏差（误杀在途生成）无法兜底。
_INFLIGHT_TOOLS = 0


async def _load_execution_settings() -> dict[str, str] | None:
    """读 ``system_settings`` 全量映射；失败返回 ``None``（调用方据此旁路，不阻断工具调用）。"""

    try:
        from lib.config.service import ConfigService
        from lib.db import async_session_factory

        async with async_session_factory() as session:
            return await ConfigService(session).get_all_settings()
    except Exception as exc:
        logger.warning("env gate: failed to read system_settings: %s", exc)
        return None


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

    all_settings = await _load_execution_settings()
    if all_settings is None:
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


async def _restore_text_env_after_tool(tool_name: str) -> str | None:
    """工具返回后把宿主 env 切回 Agent brain 的文本 env。返回诊断码或 ``None``。

    **为什么需要**：``_preflight_for_tool`` 只在**调用前**切 env。GPU 类工具执行期间
    Agent 的 brain（``ClaudeSDKClient`` 子进程）不发起 LLM 请求，所以切换窗口内不需要
    LLM；但工具一返回，CLI 立刻要发下一次请求 —— 此时承载 brain 的文本 env 已被
    ``stop_all`` 杀掉，必然 ECONNREFUSED。本函数在**返回路径**上把 env 补回来。

    **失败绝不上抛成 tool error**：Agent 见 tool error 会重试，而重试意味着重跑整批
    GPU 生成（视频数分钟、显存 23 GB）。用「端点暂时不可达」换「整批重跑」是最差的交易。
    故此处只记日志并返回诊断码，由下一次 ``send_message`` 入口的
    ``server.agent_runtime.llm_preflight.ensure_llm_ready`` 兜底。

    四条短路条件（缺一条就会引入新 bug）：

    1. 开关 ``post_tool_env_restore`` 关闭（默认关，与上游零回归）；
    2. 本工具不需要 preflight（``step_for_tool`` 返回 ``None``）；
    3. 本工具与 brain 同 env（连续文本阶段，切了也白切 30–90s）；
    4. 当前 env 已是 brain 文本 env（没切走过）。

    另有**并发闸**：:data:`_INFLIGHT_TOOLS` 非零时不恢复——同一回合并发发出的
    工具里，先完成的那个若立刻切回 brain env，会把另一个正在生成中的任务连同
    它的 env 一起换掉。
    """

    if _INFLIGHT_TOOLS != 0:
        logger.info("post-tool restore: skipped for %s — %d tool call(s) still in flight", tool_name, _INFLIGHT_TOOLS)
        return None

    step_id = step_for_tool(tool_name)
    if step_id is None:
        return None

    all_settings = await _load_execution_settings()
    if all_settings is None:
        return None

    if parse_execution_mode(all_settings.get("execution_mode", "")) != "exclusive":
        return None
    if not parse_post_tool_env_restore(all_settings.get(POST_TOOL_ENV_RESTORE_SETTING)):
        return None

    env_mapping = effective_env_mapping(all_settings.get("env_mapping", ""))
    tool_env = env_mapping.get(step_id)

    async def _read_settings_async() -> dict[str, str]:
        return all_settings

    brain_env = await resolve_text_env_async(read_settings_async=_read_settings_async)

    if not tool_env or tool_env == brain_env:
        return None

    current_env = read_current_host_env(DEFAULT_CURRENT_ENV_PATH)
    if current_env == brain_env:
        return None

    logger.info(
        "post-tool restore: switching back to brain env %s (tool=%s, step=%s, current=%s)",
        brain_env,
        tool_name,
        step_id,
        current_env,
    )
    try:
        await ensure_env(brain_env)
        return None
    except EnvSwitchBusy as exc:
        # 锁忙 = 另一个批次正占着 GPU，本进程压根没开始切。立刻重试只会再撞同一把锁，
        # 不如交给 ensure_llm_ready 兜底，并把「忙」的事实留给上层。
        logger.warning("post-tool restore: host GPU busy, deferring to ensure_llm_ready: %s", exc)
        return "post_tool_env_restore_busy"
    except EnvSwitchError as exc:
        # 只重试「切了但没成」的失败。锁忙在上面单独处理：它是「压根没轮到」，
        # 立刻重试只会再撞同一把锁。``Exception`` 兜底放最后（超时的 CancelledError
        # 是 BaseException，不会被这里吞掉）。
        logger.warning("post-tool restore: first attempt failed for %s: %s", tool_name, exc)
        try:
            await ensure_env(brain_env)
            return None
        except EnvSwitchBusy as retry_exc:
            logger.warning("post-tool restore: host GPU busy on retry for %s: %s", tool_name, retry_exc)
            return "post_tool_env_restore_busy"
        except Exception as retry_exc:
            logger.warning("post-tool restore: retry failed for %s: %s", tool_name, retry_exc)
            return "post_tool_env_restore_failed"
    except Exception as exc:
        logger.warning("post-tool restore: unexpected error for %s: %s", tool_name, exc)
        try:
            await ensure_env(brain_env)
            return None
        except EnvSwitchBusy as retry_exc:
            logger.warning("post-tool restore: host GPU busy on retry for %s: %s", tool_name, retry_exc)
            return "post_tool_env_restore_busy"
        except Exception as retry_exc:
            logger.warning("post-tool restore: retry failed for %s: %s", tool_name, retry_exc)
            return "post_tool_env_restore_failed"


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

        # 计数覆盖 invoke 本身（含其抛出）：工具中途异常同样要留下「env 可能被切走」
        # 的现场，否则下一次 LLM 调用必然 ECONNREFUSED，而这里正是修它的地方。
        global _INFLIGHT_TOOLS
        _INFLIGHT_TOOLS += 1
        try:
            if isinstance(declaration, UnscopedToolDeclaration):
                outcome = await invoke_unscoped_declaration(declaration, arguments, caller, services)
            else:
                outcome = await invoke_declaration(declaration, arguments, scope, caller, services)
        finally:
            _INFLIGHT_TOOLS -= 1

        # G3 单机单卡互斥的对称面：preflight 切出去的 env 要在**返回前**补回来，
        # 否则 CLI 紧接着发起的下一次 LLM 请求必然 ECONNREFUSED。
        # 失败只记日志（见 :func:`_restore_text_env_after_tool` 的失败语义），绝不
        # 转成 tool error ——那会让 Agent 重试并重跑整批 GPU 生成。
        restore_code = await _restore_text_env_after_tool(tool_name)
        if restore_code is not None:
            logger.warning(
                "post-tool restore: tool %s completed but env was not restored (%s); "
                "the next LLM call may fail — ensure_llm_ready will retry at the next turn",
                tool_name,
                restore_code,
            )
        return embedded_result(declaration, outcome)

    return McpSdkServerConfig(type="sdk", name=name, instance=server)


__all__ = ["embedded_result", "embedded_server"]
