"""本地单卡多模型互斥模式的 env 映射 schema —— 仅限本机已注册的 env。

提供三件事：

* :data:`STEP_IDS` —— 工作流 11 个 step id 的白名单，作为 env_mapping 的合法 key。
* :func:`list_available_envs` —— 运行期扫描宿主机 ``/home/dafeng/ai-stack/env/*.env``，剥掉
  ``common.env`` / ``extra_model_paths.yaml``，得到合法 env 名集合。
* :func:`parse_env_mapping` / :func:`validate_env_mapping` —— 解析并校验
  ``system_settings.env_mapping`` 的 JSON 字符串。

不做通用化（占位符 / 命令白名单 / 凭据引用）：那部分由 ``server/agent_runtime/preflight_env``
以 ``r9700-env`` 单一命令 + ``arcreel-env-gate.sh`` 单一闸门实现。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

#: 工作流 11 个 step id 的白名单 —— 与 :data:`lib.workflow.workflow_rules._STEP_IDS` 对齐。
#: 这里再列一份，避免 lib.config 反向依赖 lib.workflow（schema 层只声明，不引用）。
STEP_IDS: Final[tuple[str, ...]] = (
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

#: 默认 env_mapping —— 与用户在 plan-review 阶段确认的映射一致；文本走 nsfw，图像走
#: image-qwen21（默认子模型 uc-q8，env 文件已设置），视频走 video-ltx23-sulphur。
DEFAULT_ENV_MAPPING: Final[dict[str, str]] = {
    "project_input": "nsfw",
    "selling_points": "nsfw",
    "episode_plan": "nsfw",
    "script_plan_content": "nsfw",
    "script_plan_review": "nsfw",
    "final_script": "nsfw",
    "asset_sheets": "image-qwen21",
    "script_structure": "nsfw",
    "storyboard": "nsfw",
    "video": "video-ltx23-sulphur",
    "edit": "nsfw",
}

#: execution_mode 取值集合。默认 ``multi`` 与上游 ArcReel 行为 100% 一致 —— 零回归。
VALID_EXECUTION_MODES: Final[frozenset[str]] = frozenset({"multi", "exclusive"})


#: 宿主机 env 文件目录。容器内须经 ``deploy/docker-compose.yml`` 的 bind mount 暴露该路径
#: （AGENTS.md「部署适配约定」）。路径解析失败时降级为空集 + 记录日志，不抛异常 —— schema
#: 层的所有函数均允许独立运行（如单元测试场景）。
_DEFAULT_ENV_DIR = Path("/home/dafeng/ai-stack/env")


def _default_env_dir() -> Path:
    """运行期 env 目录；测试可 monkeypatch。"""
    return _DEFAULT_ENV_DIR


def list_available_envs(env_dir: Path | None = None) -> tuple[str, ...]:
    """扫描 env 目录返回合法 env 名（已剥掉 ``common.env`` / ``*.yaml``）。

    空目录或路径不存在返回空元组 —— 调用方据此提示用户绑定宿主机 env 目录。
    """

    directory = env_dir if env_dir is not None else _default_env_dir()
    if not directory.is_dir():
        logger.warning("env directory %s not available; env_mapping validation will reject all env names", directory)
        return ()
    envs: list[str] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix != ".env":
            continue
        if entry.stem in {"common"}:
            continue
        envs.append(entry.stem)
    return tuple(envs)


def parse_env_mapping(raw: str) -> dict[str, str]:
    """解析 ``system_settings.env_mapping`` 的 JSON 字符串。

    空字符串按空 mapping 返回 —— 由 :func:`effective_env_mapping` 决定是否回退到默认。
    非空字符串必须是合法 JSON object，否则抛 :class:`ValueError`。
    """

    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"env_mapping is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("env_mapping must be a JSON object of step_id → env_name")
    normalized: dict[str, str] = {}
    for key, value in loaded.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("env_mapping keys and values must both be strings")
        normalized[key] = value.strip()
    return normalized


def validate_env_mapping(
    mapping: dict[str, str],
    *,
    available_envs: tuple[str, ...] | None = None,
    valid_step_ids: tuple[str, ...] = STEP_IDS,
) -> None:
    """校验 mapping：key 必须是合法 step id，value 必须是合法 env 名。

    任一不满足抛 :class:`ValueError`，错误消息中带字段名便于定位。
    缺 key 不报错 —— 用户只配了 ``video`` 一条也合法。
    """

    available = available_envs if available_envs is not None else list_available_envs()
    available_set = frozenset(available)
    step_set = frozenset(valid_step_ids)
    for step_id, env_name in mapping.items():
        if step_id not in step_set:
            raise ValueError(
                f"env_mapping key {step_id!r} is not a valid workflow step id; expected one of {sorted(step_set)}"
            )
        if env_name not in available_set:
            raise ValueError(
                f"env_mapping[{step_id!r}] = {env_name!r} is not a known env on this host; "
                f"available: {sorted(available_set) or ['<none — bind-mount /home/dafeng/ai-stack/env?>']}"
            )


def effective_env_mapping(raw: str) -> dict[str, str]:
    """取最终生效的 env_mapping：raw 非空 → 解析校验后用之；raw 空 → 用默认。

    默认值也会经 :func:`validate_env_mapping` 校验（在宿主机有合法 env 时），若全部 env 都
    不在白名单则原样使用并打印 warning —— 让用户至少能在 UI 上看见「默认 mapping 也无法
    生效」的事实。
    """

    parsed = parse_env_mapping(raw)
    if not parsed:
        mapping = dict(DEFAULT_ENV_MAPPING)
        try:
            validate_env_mapping(mapping)
        except ValueError:
            logger.warning(
                "default env_mapping failed validation on this host; "
                "exclusive mode will not be able to run without an explicit env_mapping"
            )
        return mapping
    validate_env_mapping(parsed)
    return parsed


def parse_execution_mode(raw: str) -> str:
    """读 ``system_settings.execution_mode`` 字符串为 ``multi`` / ``exclusive``。"""

    value = (raw or "").strip().lower()
    if not value:
        return "multi"
    if value in VALID_EXECUTION_MODES:
        return value
    raise ValueError(
        f"execution_mode {value!r} is not valid; expected one of {sorted(VALID_EXECUTION_MODES)}"
    )


__all__ = [
    "DEFAULT_ENV_MAPPING",
    "STEP_IDS",
    "VALID_EXECUTION_MODES",
    "effective_env_mapping",
    "list_available_envs",
    "parse_env_mapping",
    "parse_execution_mode",
    "validate_env_mapping",
]
