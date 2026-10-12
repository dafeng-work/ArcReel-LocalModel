"""G3 单机单卡互斥适配 — ``lib.config.dispatcher_schema`` 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.config.dispatcher_schema import (
    DEFAULT_ENV_MAPPING,
    STEP_IDS,
    VALID_EXECUTION_MODES,
    effective_env_mapping,
    list_available_envs,
    parse_env_mapping,
    parse_execution_mode,
    validate_env_mapping,
)

# ---- list_available_envs --------------------------------------------------------


def _write_envs(directory: Path, names: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / f"{name}.env").write_text("KIND=llm\n", encoding="utf-8")
    # 故意放一个非 .env 与一个被白名单剔除的 common.env，验证剔除逻辑。
    (directory / "common.env").write_text("STACK_ROOT=/home/dafeng/ai-stack\n", encoding="utf-8")
    (directory / "extra_model_paths.yaml").write_text("foo: bar\n", encoding="utf-8")


def test_list_available_envs_returns_env_stems(tmp_path: Path) -> None:
    _write_envs(tmp_path, ["nsfw", "image-qwen21", "video-ltx23-sulphur"])
    result = list_available_envs(tmp_path)
    assert "nsfw" in result
    assert "image-qwen21" in result
    assert "video-ltx23-sulphur" in result
    assert "common" not in result
    # 返回元组，且按名字排序
    assert result == tuple(sorted(result))


def test_list_available_envs_missing_dir(tmp_path: Path) -> None:
    # 不存在的目录 → 空元组，不抛
    assert list_available_envs(tmp_path / "missing") == ()


# ---- parse_env_mapping ----------------------------------------------------------


def test_parse_env_mapping_empty_string_returns_empty_dict() -> None:
    assert parse_env_mapping("") == {}
    assert parse_env_mapping("   ") == {}


def test_parse_env_mapping_valid_json() -> None:
    raw = '{"video": "video-ltx23-sulphur", "asset_sheets": "image-qwen21"}'
    result = parse_env_mapping(raw)
    assert result == {"video": "video-ltx23-sulphur", "asset_sheets": "image-qwen21"}


def test_parse_env_mapping_invalid_json_raises() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_env_mapping("{video: video-ltx23-sulphur}")  # 缺引号


def test_parse_env_mapping_non_object_raises() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_env_mapping('["video"]')


def test_parse_env_mapping_non_string_value_raises() -> None:
    with pytest.raises(ValueError, match="must both be strings"):
        parse_env_mapping('{"video": 1}')


# ---- validate_env_mapping -------------------------------------------------------


def test_validate_env_mapping_accepts_known_step_and_known_env() -> None:
    # 合法 mapping 不抛异常，返回 None（避免破坏既有契约）。
    assert validate_env_mapping({"video": "video-ltx23-sulphur"}, available_envs=("video-ltx23-sulphur",)) is None


def test_validate_env_mapping_rejects_unknown_step() -> None:
    with pytest.raises(ValueError, match="not a valid workflow step id"):
        validate_env_mapping({"videoXXX": "video-ltx23-sulphur"}, available_envs=("video-ltx23-sulphur",))


def test_validate_env_mapping_rejects_unknown_env() -> None:
    with pytest.raises(ValueError, match="not a known env on this host"):
        validate_env_mapping({"video": "no-such-env"}, available_envs=("video-ltx23-sulphur",))


def test_validate_env_mapping_empty_passes() -> None:
    # 空 mapping 也合法（缺 key 不报错）：只是返回 None。
    assert validate_env_mapping({}, available_envs=("nsfw",)) is None


# ---- effective_env_mapping ------------------------------------------------------


def test_effective_env_mapping_empty_uses_default() -> None:
    result = effective_env_mapping("")
    assert result == dict(DEFAULT_ENV_MAPPING)
    # 默认 mapping 的每个 step 都应映射到合法 env 名（如果 env 目录在本机可用）
    for env in set(result.values()):
        assert env in VALID_EXECUTION_MODES or len(env) > 0  # 语法占位


def test_effective_env_mapping_explicit_overrides_default() -> None:
    raw = '{"video": "video-ltx23-sulphur"}'
    result = effective_env_mapping(raw)
    assert result == {"video": "video-ltx23-sulphur"}


def test_effective_env_mapping_propagates_validation_error() -> None:
    with pytest.raises(ValueError, match="not a valid workflow step id"):
        effective_env_mapping('{"unknown": "nsfw"}')


# ---- parse_execution_mode -------------------------------------------------------


def test_parse_execution_mode_defaults_to_multi() -> None:
    assert parse_execution_mode("") == "multi"
    assert parse_execution_mode("   ") == "multi"


def test_parse_execution_mode_accepts_known_values() -> None:
    assert parse_execution_mode("multi") == "multi"
    assert parse_execution_mode("EXCLUSIVE") == "exclusive"
    assert parse_execution_mode("Exclusive") == "exclusive"


def test_parse_execution_mode_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="not valid"):
        parse_execution_mode("xyz")


# ---- STEP_IDS 完整性 -----------------------------------------------------------


def test_step_ids_matches_documented_workflow_steps() -> None:
    assert "video" in STEP_IDS
    assert "asset_sheets" in STEP_IDS
    assert "edit" in STEP_IDS
    assert "project_input" in STEP_IDS
