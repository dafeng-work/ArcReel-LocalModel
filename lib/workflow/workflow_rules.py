"""Data-driven workflow step rules for every content and generation mode pair."""

from __future__ import annotations

from dataclasses import dataclass

from lib.script.script_skeleton import resolve_declared_kind


@dataclass(frozen=True, slots=True)
class WorkflowStepRule:
    """One workflow step; the order is presentational only and never a precondition.

    ``required_env`` 是 G3 单机单卡互斥适配的可选字段：工作流步骤级 env 前置闸门据此知道
    该步骤需要哪个宿主 env（仅在 ``execution_mode == "exclusive"`` 时启用，由
    ``lib.workflow.workflow_plan`` 在 next_action 投影层注入 ``PREFLIGHT_ENV_SWITCH``）。
    默认值 ``None`` 表示该步骤不强制 env 约束（向后兼容现有 11 步逻辑）。
    """

    id: str
    applicable: bool
    required_env: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowRule:
    """The complete workflow shape for one immutable project mode pair."""

    content_mode: str
    generation_mode: str
    skeleton_kind: str
    preprocessor: str | None
    steps: tuple[WorkflowStepRule, ...]


_STEP_IDS: tuple[str, ...] = (
    "project_input",
    "selling_points",
    "episode_plan",
    "script_plan_content",
    "script_plan_review",
    "final_script",
    "asset_sheets",
    "script_structure",
    "storyboard",
    "video",
    "edit",
)

_EPISODIC_STEPS = frozenset(
    {
        "project_input",
        "episode_plan",
        "script_plan_content",
        "script_plan_review",
        "final_script",
        "asset_sheets",
        "script_structure",
        "video",
        "edit",
    }
)

_CONTENT_STEPS: dict[str, frozenset[str]] = {
    "narration": _EPISODIC_STEPS,
    "drama": _EPISODIC_STEPS,
    "ad": frozenset(
        {
            "project_input",
            "selling_points",
            "final_script",
            "asset_sheets",
            "script_structure",
            "video",
            "edit",
        }
    ),
}

_PREPROCESSORS: dict[tuple[str, str], str | None] = {
    ("narration", "storyboard"): "split-narration-segments",
    ("narration", "reference_video"): "split-reference-video-units",
    ("drama", "storyboard"): "normalize-drama-script",
    ("drama", "reference_video"): "split-reference-video-units",
    ("ad", "storyboard"): None,
    ("ad", "reference_video"): None,
}


def _build_rule(
    content_mode: str, generation_mode: str, env_map: dict[str, str] | None = None
) -> WorkflowRule:
    applicable = set(_CONTENT_STEPS[content_mode])
    if generation_mode == "storyboard":
        applicable.add("storyboard")
    mapping = env_map or {}
    return WorkflowRule(
        content_mode=content_mode,
        generation_mode=generation_mode,
        skeleton_kind=resolve_declared_kind(content_mode, generation_mode),
        preprocessor=_PREPROCESSORS[(content_mode, generation_mode)],
        steps=tuple(
            WorkflowStepRule(
                id=step_id,
                applicable=step_id in applicable,
                required_env=mapping.get(step_id),
            )
            for step_id in _STEP_IDS
        ),
    )


WORKFLOW_RULES: dict[tuple[str, str], WorkflowRule] = {
    (content_mode, generation_mode): _build_rule(content_mode, generation_mode)
    for content_mode in ("narration", "drama", "ad")
    for generation_mode in ("storyboard", "reference_video")
}


def workflow_rule(
    content_mode: str, generation_mode: str, *, env_map: dict[str, str] | None = None
) -> WorkflowRule:
    """Return the exhaustive rule for a validated project mode pair.

    ``env_map`` 是 G3 注入：把 step-id → env 名映射写进每一步的 ``required_env``。该参数只
    影响 :class:`WorkflowStepRule` 字段，content_mode / generation_mode → 步骤集与适用性完全
    不变。``env_map=None`` 时退回到 ``required_env=None``，行为与改动前一致（向后兼容）。
    """

    if env_map is None:
        try:
            return WORKFLOW_RULES[(content_mode, generation_mode)]
        except KeyError as exc:
            raise ValueError(f"unsupported workflow mode pair: {content_mode!r}, {generation_mode!r}") from exc
    try:
        base = WORKFLOW_RULES[(content_mode, generation_mode)]
    except KeyError as exc:
        raise ValueError(f"unsupported workflow mode pair: {content_mode!r}, {generation_mode!r}") from exc
    return WorkflowRule(
        content_mode=base.content_mode,
        generation_mode=base.generation_mode,
        skeleton_kind=base.skeleton_kind,
        preprocessor=base.preprocessor,
        steps=tuple(
            WorkflowStepRule(id=step.id, applicable=step.applicable, required_env=env_map.get(step.id))
            for step in base.steps
        ),
    )


__all__ = [
    "WORKFLOW_RULES",
    "WorkflowRule",
    "WorkflowStepRule",
    "workflow_rule",
]
