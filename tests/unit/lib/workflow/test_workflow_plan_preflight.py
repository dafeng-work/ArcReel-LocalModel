"""G3 单机单卡互斥适配 — ``lib.workflow.workflow_plan`` 的 preflight 改写单元测试。"""

from __future__ import annotations

import pytest

from lib.workflow.workflow_plan import (
    PREFLIGHT_ENV_SWITCH_ETA_SECONDS,
    PREFLIGHT_ENV_SWITCH_TIMEOUT_SECONDS,
    build_workflow_plan,
)
from lib.workflow.workflow_state import WorkflowActionType
from tests.unit.lib.workflow.test_workflow_plan import _status


_DEFAULT_ENV_MAP = {
    "video": "video-ltx23-sulphur",
    "asset_sheets": "image-qwen21",
    "script_plan_content": "nsfw",
    "final_script": "nsfw",
}


def _plan(*, execution_mode: str | None, current_host_env: str | None, env_map=None):
    status = _status(action="generate_videos", requested_ids=["E1S01"])
    return build_workflow_plan(
        status,
        env_map=env_map if env_map is not None else _DEFAULT_ENV_MAP,
        execution_mode=execution_mode,
        current_host_env=current_host_env,
    )


# ---- multi 模式不注入 --------------------------------------------------------


def test_multi_mode_does_not_inject_preflight() -> None:
    plan = _plan(execution_mode="multi", current_host_env="nsfw")
    assert plan.next_action.type is WorkflowActionType.GENERATE_VIDEOS


def test_execution_mode_none_does_not_inject_preflight() -> None:
    plan = _plan(execution_mode=None, current_host_env="nsfw")
    assert plan.next_action.type is WorkflowActionType.GENERATE_VIDEOS


# ---- current_host_env 未提供 → 旁路 -----------------------------------------


def test_no_current_host_env_does_not_inject_preflight() -> None:
    plan = _plan(execution_mode="exclusive", current_host_env=None)
    assert plan.next_action.type is WorkflowActionType.GENERATE_VIDEOS


# ---- exclusive + 不匹配 → 注入 ---------------------------------------------


def test_exclusive_mode_unmatched_env_injects_preflight() -> None:
    plan = _plan(execution_mode="exclusive", current_host_env="nsfw")
    assert plan.next_action.type is WorkflowActionType.PREFLIGHT_ENV_SWITCH

    args = plan.next_action.args
    assert args["target_env"] == "video-ltx23-sulphur"
    assert args["current_host_env"] == "nsfw"
    assert args["timeout_seconds"] == PREFLIGHT_ENV_SWITCH_TIMEOUT_SECONDS
    assert args["eta_seconds"] == PREFLIGHT_ENV_SWITCH_ETA_SECONDS
    # resume_action 应回放原 next_action 的 JSON 序列化
    assert isinstance(args["resume_action"], dict)
    assert args["resume_action"]["type"] == "generate_videos"
    assert args["resume_action"]["requested_ids"] == ["E1S01"]


# ---- exclusive + 已匹配 → 不注入 --------------------------------------------


def test_exclusive_mode_matching_env_does_not_inject_preflight() -> None:
    # 模拟已经在 video-ltx23-sulphur 跑
    plan = _plan(execution_mode="exclusive", current_host_env="video-ltx23-sulphur")
    assert plan.next_action.type is WorkflowActionType.GENERATE_VIDEOS


# ---- exclusive + env_map 为空 → required_env 始终 None → 不注入 -----------------


def test_exclusive_mode_with_empty_env_map_does_not_inject() -> None:
    plan = _plan(
        execution_mode="exclusive",
        current_host_env="nsfw",
        env_map={},
    )
    assert plan.next_action.type is WorkflowActionType.GENERATE_VIDEOS


# ---- owner step 不是 video 时走其他分支 ---------------------------------------


def test_exclusive_mode_owner_not_video_does_not_inject() -> None:
    """owner 是其他 step（如 final_script）→ 该 step required_env 默认 nsfw → current 也
    是 nsfw → 不注入。覆盖"required_env 不匹配才注入"语义。"""
    from lib.workflow.workflow_state import WorkflowStatus

    status = _status(action="generate_script")
    plan = build_workflow_plan(
        status,
        env_map=_DEFAULT_ENV_MAP,
        execution_mode="exclusive",
        current_host_env="nsfw",
    )
    assert plan.next_action.type is WorkflowActionType.GENERATE_SCRIPT


# ---- next_action.type == NONE 不注入 ------------------------------------------


def test_exclusive_mode_with_none_next_action_does_not_inject() -> None:
    from lib.workflow.workflow_state import WorkflowActionType, WorkflowNextAction, WorkflowStatus

    status = _status(action="generate_videos")
    status.next_action = WorkflowNextAction(type=WorkflowActionType.NONE, reason="blocked")
    plan = build_workflow_plan(
        status,
        env_map=_DEFAULT_ENV_MAP,
        execution_mode="exclusive",
        current_host_env="nsfw",
    )
    # next_action 已是 NONE —— 不该被改成 PREFLIGHT_ENV_SWITCH
    assert plan.next_action.type is WorkflowActionType.NONE


# ---- 默认 mapping 默认值在 dispatcher_schema 中可校验 --------------------------


def test_default_env_mapping_includes_required_keys() -> None:
    """保护默认 mapping 的完整性：缺 key → preflight 永远不命中。"""
    from lib.config.dispatcher_schema import DEFAULT_ENV_MAPPING, STEP_IDS

    for step_id in STEP_IDS:
        # step 不一定都有默认（按 plan），但 video 必须有 —— 是 G3 的核心覆盖项
        if step_id in {"video", "asset_sheets", "final_script", "script_plan_content"}:
            assert step_id in DEFAULT_ENV_MAPPING
