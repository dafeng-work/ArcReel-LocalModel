"""mixed（混合模式）消费方逐点核对（ADR 0099 后果条款）。

ADR 0099 写明：「所有 ``is_reference_family_project`` 消费方须逐一核对 mixed 行为
（本次实施已列清单核对，漏一处 = 混合项目某路径静默走错管线）」。

本文件把散在多个模块里的消费方行为钉在一起。任一处退回
``generation_mode == "reference_video"`` 的严格判，混合项目就会在某条路径上静默走
错管线——这类缺陷不报错、只出错结果，正单元/集成测试也未必覆盖得到，故在此逐点断言。
"""

from __future__ import annotations

import pytest

from lib.artifacts.artifact_provenance import _script_plan_prompt_variant
from lib.config.resolver import derive_voice_consistency
from lib.project.generation_modes import is_reference_family_generation_mode
from lib.project.project_manager import is_reference_family_project
from lib.prompts.prompt_builders_ad import _shot_duration_constraint
from lib.script.script_skeleton import SkeletonRouteMismatchError
from server.services.project import project_archive


def test_generation_mode_predicate_covers_reference_family() -> None:
    """参考家族 = reference_video + mixed；storyboard 与非字符串都不是。"""
    assert is_reference_family_generation_mode("reference_video") is True
    assert is_reference_family_generation_mode("mixed") is True
    assert is_reference_family_generation_mode("storyboard") is False
    assert is_reference_family_generation_mode("grid") is False
    assert is_reference_family_generation_mode(None) is False
    assert is_reference_family_generation_mode(3) is False


def test_project_predicate_reads_generation_mode() -> None:
    """项目级判据按 generation_mode 取值判；缺字段判否（模式不明不默认落参考管线）。"""
    assert is_reference_family_project({"generation_mode": "mixed"}) is True
    assert is_reference_family_project({"generation_mode": "reference_video"}) is True
    assert is_reference_family_project({"generation_mode": "storyboard"}) is False
    assert is_reference_family_project({}) is False


@pytest.mark.parametrize("generation_mode", ["reference_video", "mixed"])
def test_ad_prompt_builder_refuses_reference_family_generation_modes(generation_mode: str) -> None:
    """广告脚本的分镜时长约束只属分镜路线：参考家族（含 mixed）须走自包含 unit 构建器。"""
    with pytest.raises(ValueError, match="build_ad_reference_prompt"):
        _shot_duration_constraint(generation_mode, [5, 8])


@pytest.mark.parametrize(
    ("generation_mode", "expected_route"),
    [
        ("reference_video", "val_route_reference_video"),
        ("mixed", "val_route_reference_video"),
        ("storyboard", "val_route_storyboard"),
        (None, "val_route_storyboard"),
    ],
)
def test_skeleton_mismatch_route_ref_follows_reference_family(generation_mode: str | None, expected_route: str) -> None:
    """骨架失配的提示词指路文案按参考家族判：mixed 不得被指去分镜路线。"""
    error = SkeletonRouteMismatchError(expected="video_units", actual="segments", generation_mode=generation_mode)
    params = error.to_validation_message().params
    assert params["route"].key == expected_route


def test_voice_consistency_is_native_for_mixed_reference_audio_binding() -> None:
    """参考音频直挂 + 角色绑定 reference_audio 时，参考家族（含 mixed）派生出 native。"""
    for generation_mode in ("reference_video", "mixed"):
        assert (
            derive_voice_consistency(
                reference_audio_mode="direct",
                generation_mode=generation_mode,
                has_audio=True,
                character_voice_binding="reference_audio",
            )
            == "native"
        )


def test_script_plan_prompt_variant_is_reference_for_mixed() -> None:
    """混合模式的脚本规划走参考变体：提示词冻结基线必须与渲染出口同族。"""
    assert _script_plan_prompt_variant("narration", "mixed") == "reference_video"
    assert _script_plan_prompt_variant("drama", "mixed") == "reference_video"
    assert _script_plan_prompt_variant("narration", "reference_video") == "reference_video"
    assert _script_plan_prompt_variant("narration", "storyboard") == "narration"
    assert _script_plan_prompt_variant("drama", "storyboard") == "drama"


def test_registry_supported_durations_unions_both_buckets_for_mixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档自报时长：参考家族（含 mixed）取 r2v 与 i2v 两桶声明全集的并集。"""

    class _ModelInfo:
        def __init__(self, durations: list[int]) -> None:
            self.supported_durations = durations

    def _fake_model_info_for(*ids: str) -> _ModelInfo:
        return _ModelInfo([5, 8] if ids[-1] == "m-r2v" else [6, 10])

    monkeypatch.setattr(project_archive, "model_info_for", _fake_model_info_for)

    project = {
        "generation_mode": "mixed",
        "video_provider_r2v": "p/m-r2v",
        "video_provider_i2v": "p/m-i2v",
    }
    assert project_archive._registry_supported_durations(project) == [5, 6, 8, 10]
