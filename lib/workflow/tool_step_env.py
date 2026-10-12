"""G3 单机单卡互斥适配 — MCP 工具名 → workflow step 映射。

**架构缺口**：Agent 看到 ``next_action = PREFLIGHT_ENV_SWITCH`` 后直接调 MCP 工具（如
``prepare_script_plan``），不经过 ``POST /workflow-dispatch``，preflight 旁路。本模块把
preflight 下沉到 MCP 工具统一入口（``server.agent_toolset.embedded.call_tool``）。

**只对真正需要 GPU / LLM 的工具做 preflight**；读 / 查询 / 剪辑 / ffmpeg 渲染类工具
（``list_*`` / ``get_*`` / ``read_*`` / ``render_final_cut`` / ``create_timeline`` 等）
不映射 —— 保持零切换开销。

每个工具映射到 11 个 workflow step 之一；缺省 step 由 ``lib.config.dispatcher_schema``
的 ``DEFAULT_ENV_MAPPING`` 或用户配置的 ``env_mapping`` 决定 target env。
"""

from __future__ import annotations

from typing import Final

#: 工具名 → workflow step id。未列出的工具**不需要 preflight**（不切 env）。
#:
#: 文本 LLM 类（→ nsfw）：
#:   plan_episodes, generate_script_plan, confirm_script_review,
#:   generate_episode_script, edit_source_text, patch_episode_script,
#:   generate_narration_audio, get_script_plan_content, patch_draft
#:
#: 图像 GPU 类（→ image-qwen21）：
#:   generate_assets, edit_images
#:
#: 分镜图（→ storyboard step，默认 nsfw 提示词生成）：
#:   generate_storyboards, generate_grid, split_grids
#:
#: 视频 GPU 类（→ video-ltx23-sulphur）：
#:   generate_videos
TOOL_STEP_ENV: Final[dict[str, str]] = {
    # ── 文本 LLM 类 ─────────────────────────────────────────────────────
    "plan_episodes": "episode_plan",
    "generate_script_plan": "script_plan_content",
    "get_script_plan_content": "script_plan_content",
    "patch_draft": "script_plan_content",
    "confirm_script_review": "script_plan_review",
    "generate_episode_script": "final_script",
    "edit_source_text": "final_script",
    "patch_episode_script": "final_script",
    "generate_narration_audio": "final_script",
    # ── 图像 GPU 类 ─────────────────────────────────────────────────────
    "generate_assets": "asset_sheets",
    "edit_images": "asset_sheets",
    # ── 分镜图 ─────────────────────────────────────────────────────────
    "generate_storyboards": "storyboard",
    "generate_grid": "storyboard",
    "split_grids": "storyboard",
    # ── 视频 GPU 类 ─────────────────────────────────────────────────────
    "generate_videos": "video",
}


def step_for_tool(tool_name: str) -> str | None:
    """返回工具归属的 workflow step id；不需要 preflight 的工具返回 ``None``。

    调用方据此判断是否走 ``ensure_env``。返回 ``None`` 时调用方直接放行，零切换开销。
    """

    return TOOL_STEP_ENV.get(tool_name)


__all__ = [
    "TOOL_STEP_ENV",
    "step_for_tool",
]
