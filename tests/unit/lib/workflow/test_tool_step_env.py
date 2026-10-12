"""G3 — ``lib.workflow.tool_step_env`` 单元测试：工具名 → workflow step 映射。"""

from __future__ import annotations

from lib.workflow.tool_step_env import TOOL_STEP_ENV, step_for_tool


def test_tools_that_need_preflight_map_to_expected_steps() -> None:
    """文本 LLM / 图像 GPU / 视频 GPU 三类工具的 step 映射。"""

    # 文本 LLM 类
    assert step_for_tool("plan_episodes") == "episode_plan"
    assert step_for_tool("generate_script_plan") == "script_plan_content"
    assert step_for_tool("confirm_script_review") == "script_plan_review"
    assert step_for_tool("generate_episode_script") == "final_script"
    assert step_for_tool("generate_narration_audio") == "final_script"

    # 图像 GPU 类
    assert step_for_tool("generate_assets") == "asset_sheets"
    assert step_for_tool("edit_images") == "asset_sheets"

    # 分镜图
    assert step_for_tool("generate_storyboards") == "storyboard"
    assert step_for_tool("generate_grid") == "storyboard"

    # 视频 GPU 类
    assert step_for_tool("generate_videos") == "video"


def test_read_only_tools_do_not_need_preflight() -> None:
    """读 / 查询 / 剪辑 / 渲染类工具不映射（返回 None）。"""

    for tool in [
        "list_projects",
        "get_project_content",
        "read_project_file",
        "list_bgm",
        "render_final_cut",
        "create_timeline",
        "edit_timeline",
        "read_timeline",
        "upload_source",
        "inspect_video_units",
    ]:
        assert step_for_tool(tool) is None, f"{tool} should not require preflight"


def test_unknown_tool_returns_none() -> None:
    """未知工具（防呆）返回 None —— 调用方按不需要 preflight 处理。"""
    assert step_for_tool("nonexistent_tool_xyz") is None


def test_all_mapped_steps_are_valid_workflow_steps() -> None:
    """TOOL_STEP_ENV 的所有 step 值必须在 lib.config.dispatcher_schema.STEP_IDS 内。"""
    from lib.config.dispatcher_schema import STEP_IDS

    valid = set(STEP_IDS)
    for tool, step in TOOL_STEP_ENV.items():
        assert step in valid, f"{tool} mapped to unknown step {step!r}"


def test_no_duplicate_tool_names_in_mapping() -> None:
    """工具名唯一（dict 本身就是 unique，此测试防呆未来重构误用 list）。"""
    keys = list(TOOL_STEP_ENV.keys())
    assert len(keys) == len(set(keys))
