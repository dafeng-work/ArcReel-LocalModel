"""混合模式首帧图定桶与投影（ADR 0099 Step 2）单测：定桶矩阵/首帧水合/能力闸分流/载荷键。"""

import json
from pathlib import Path

import pytest

from lib.generation.video_request_facts import VideoRequestFacts, VideoRequestFactsFailure
from lib.project.project_schema import CURRENT_PROJECT_SCHEMA_VERSION
from lib.script.reference_video.artifact_selection import CurrentReferenceAssets
from lib.script.reference_video.request_projection import (
    FilesystemReferenceAssets,
    ReferenceUnitRequestProjector,
    ResolvedReferenceAsset,
    VideoRequestFactsResult,
)
from lib.config.resolver import video_bucket_for_generation_mode
from lib.script.reference_video.units import reference_video_bucket, resolve_unit_video_bucket
from lib.script.reference_video.unit_capabilities import evaluate_reference_unit_capabilities
from lib.script.script_models import ReferenceResource
from tests.factories import make_video_request_facts


def _bucket_facts(generation_type: str) -> VideoRequestFacts:
    """r2v 事实无首帧能力，i2v 事实带首帧能力，用于验证能力闸按 has_image 分流。"""
    return make_video_request_facts(
        route="reference_video",
        generation_type=generation_type,
        provider_id=f"{generation_type}-provider",
        model_id=f"{generation_type}-model",
        first_frame=generation_type == "i2v",
        text_to_video=generation_type == "text_to_video",
        max_reference_images=2 if generation_type == "r2v" else 0,
        supported_durations=(8,),
        allowed_durations=(8,),
    )


class _FixedFacts:
    def __init__(self, facts: VideoRequestFacts) -> None:
        self._facts = facts

    async def __call__(self, generation_type: str) -> VideoRequestFactsResult:
        return self._facts


def _asset(kind: str, name: str, path: Path, *, image_kind: str = "original") -> ResolvedReferenceAsset:
    return ResolvedReferenceAsset(
        reference=ReferenceResource(type=kind, name=name),
        path=path,
        kind=image_kind,
    )


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"img")
    return path


def _project(generation_mode: str) -> dict:
    return {
        "generation_mode": generation_mode,
        "products": {"手袋": {}},
        "scenes": {},
        "characters": {},
        "props": {},
    }


async def _project_unit(project_path: Path, project: dict, unit: dict, facts: VideoRequestFacts):
    projector = ReferenceUnitRequestProjector(
        _FixedFacts(facts),
        FilesystemReferenceAssets(project_path),
        project_path=project_path,
    )
    _write(project_path / "assets/refs/手袋.png")
    return await projector.project_current(
        project=project,
        script={"video_units": [unit]},
        unit=unit,
        resolved_assets=[_asset("product", "手袋", project_path / "assets/refs/手袋.png")],
    )


@pytest.mark.parametrize(
    ("with_references", "first_frame_available", "expected"),
    [
        (True, True, "i2v"),
        (True, False, "r2v"),
        (False, True, "i2v"),
        (False, False, "i2v"),
    ],
)
def test_reference_video_bucket_matrix(with_references: bool, first_frame_available: bool, expected: str) -> None:
    assert reference_video_bucket(with_references=with_references, first_frame_available=first_frame_available) == expected


def test_reference_video_bucket_requires_keyword_arguments() -> None:
    with pytest.raises(TypeError):
        reference_video_bucket(True)


@pytest.mark.asyncio
async def test_mixed_first_frame_present_selects_i2v_and_ignores_reference_assets(tmp_path: Path) -> None:
    _write(tmp_path / "assets/first/U13_first.png")
    projection = await _project_unit(
        tmp_path,
        _project("mixed"),
        {
            "unit_id": "U13",
            "text": "@[手袋] 特写。",
            "duration_seconds": 8,
            "first_frame_asset": "assets/first/U13_first.png",
        },
        _bucket_facts("i2v"),
    )

    assert projection.declared_generation_type == "i2v"
    assert projection.hydrated_generation_type == "i2v"
    assert projection.request_assets == ()
    assert projection.first_frame_asset == "assets/first/U13_first.png"
    # 唯一问题是非阻断的参考图忽略提示（fl2va 端点无参考槽）。
    assert [(p.code, p.blocking) for p in projection.problems] == [("references_ignored_in_first_frame_mode", False)]
    assert projection.problems[0].parameters()["count"] == 1
    assert projection.to_advisory_payload()["first_frame"] == "assets/first/U13_first.png"


@pytest.mark.asyncio
async def test_mixed_first_frame_missing_falls_back_to_reference_bucket(tmp_path: Path) -> None:
    projection = await _project_unit(
        tmp_path,
        _project("mixed"),
        {
            "unit_id": "U13",
            "text": "@[手袋] 特写。",
            "duration_seconds": 8,
            "first_frame_asset": "assets/first/missing.png",
        },
        _bucket_facts("r2v"),
    )

    assert projection.declared_generation_type == "i2v"
    assert projection.hydrated_generation_type == "r2v"
    assert projection.request_assets == (_asset("product", "手袋", tmp_path / "assets/refs/手袋.png"),)
    assert projection.first_frame_asset is None
    assert {p.code for p in projection.problems} == {"first_frame_asset_missing", "reference_capability_changed"}
    assert projection.to_advisory_payload()["first_frame"] is None


@pytest.mark.asyncio
async def test_mixed_without_first_frame_keeps_reference_bucket(tmp_path: Path) -> None:
    projection = await _project_unit(
        tmp_path,
        _project("mixed"),
        {"unit_id": "U13", "text": "@[手袋] 特写。", "duration_seconds": 8},
        _bucket_facts("r2v"),
    )

    assert projection.declared_generation_type == "r2v"
    assert projection.hydrated_generation_type == "r2v"
    assert projection.first_frame_asset is None
    assert projection.problems == ()
    assert projection.to_advisory_payload()["first_frame"] is None


@pytest.mark.asyncio
async def test_reference_video_project_ignores_first_frame_asset(tmp_path: Path) -> None:
    _write(tmp_path / "assets/first/U13_first.png")
    projection = await _project_unit(
        tmp_path,
        _project("reference_video"),
        {
            "unit_id": "U13",
            "text": "@[手袋] 特写。",
            "duration_seconds": 8,
            "first_frame_asset": "assets/first/U13_first.png",
        },
        _bucket_facts("r2v"),
    )

    assert projection.declared_generation_type == "r2v"
    assert projection.hydrated_generation_type == "r2v"
    assert projection.first_frame_asset is None
    assert projection.to_advisory_payload()["first_frame"] is None


@pytest.mark.asyncio
async def test_capability_gate_uses_first_frame_facts_for_first_frame_units(tmp_path: Path) -> None:
    _write(tmp_path / "assets/first/U13_first.png")
    projection = await _project_unit(
        tmp_path,
        _project("mixed"),
        {
            "unit_id": "U13",
            "text": "@[手袋] 特写。",
            "duration_seconds": 8,
            "first_frame_asset": "assets/first/U13_first.png",
        },
        _bucket_facts("r2v"),  # r2v 事实无首帧能力，首帧单元必须被能力闸拦住
    )

    assert "video_capability_missing_t2v" in {p.code for p in projection.problems}


def _registry_project(tmp_path: Path, generation_mode: str) -> dict:
    """带角色登记表的项目落盘（能力评估按登记表解析资产，availability 用 fake 判定）。"""
    project = {
        "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
        "generation_mode": generation_mode,
        "characters": {"阿离": {"description": "少女", "character_sheet": "characters/阿离.png"}},
        "scenes": {},
        "props": {},
        "products": {},
    }
    (tmp_path / "characters").mkdir(exist_ok=True)
    (tmp_path / "characters" / "阿离.png").write_bytes(b"png")
    (tmp_path / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    return project


class _AvailableAssets:
    def is_available(self, asset: ResolvedReferenceAsset) -> bool:
        return True


@pytest.mark.asyncio
async def test_unit_capability_payload_exposes_first_frame_for_mixed_only(tmp_path: Path) -> None:
    _write(tmp_path / "assets/first/U13_first.png")
    unit = {
        "unit_id": "U13",
        "text": "@[阿离] 抬头。",
        "duration_seconds": 8,
        "first_frame_asset": "assets/first/U13_first.png",
    }

    (mixed,) = await evaluate_reference_unit_capabilities(
        _registry_project(tmp_path, "mixed"),
        tmp_path,
        [unit],
        request_facts=_FixedFacts(_bucket_facts("i2v")),
        availability=_AvailableAssets(),
    )
    assert mixed.to_payload()["first_frame"] == "assets/first/U13_first.png"

    (reference,) = await evaluate_reference_unit_capabilities(
        _registry_project(tmp_path, "reference_video"),
        tmp_path,
        [unit],
        request_facts=_FixedFacts(_bucket_facts("r2v")),
        availability=_AvailableAssets(),
    )
    assert reference.to_payload()["first_frame"] is None


def test_first_frame_assets_are_filesystem_gated_not_manifest_claimed(tmp_path: Path) -> None:
    project = _registry_project(tmp_path, "mixed")
    assets = CurrentReferenceAssets(tmp_path, project)
    present = ResolvedReferenceAsset(
        reference=ReferenceResource(type="product", name="手袋"),
        path=_write(tmp_path / "assets/first/U13_first.png"),
        kind="first_frame",
    )
    missing = ResolvedReferenceAsset(
        reference=ReferenceResource(type="product", name="手袋"),
        path=tmp_path / "assets/first/missing.png",
        kind="first_frame",
    )

    assert assets._claim_for(present) is None
    assert assets.is_available(present) is True
    assert assets.is_available(missing) is False


# ---------------------------------------------------------------------------
# Step 5：模式→桶消费方 mixed 回退（代表桶映射 + 单元级定桶入口）
# ---------------------------------------------------------------------------


def test_mode_bucket_mapping_mixed_falls_back_to_r2v_representative():
    """模式级标量读侧（caps/计价主模型/自报身份）：mixed 落参考家族代表桶 r2v。"""
    assert video_bucket_for_generation_mode("mixed") == "r2v"
    assert video_bucket_for_generation_mode("reference_video") == "r2v"
    assert video_bucket_for_generation_mode("storyboard") == "i2v"
    assert video_bucket_for_generation_mode(None) == "i2v"
    assert video_bucket_for_generation_mode("bogus") == "i2v"


def test_resolve_unit_video_bucket_matrix(tmp_path):
    project = tmp_path
    (project / "images").mkdir(parents=True)
    (project / "images" / "first.png").write_bytes(b"x")
    mixed = {"generation_mode": "mixed"}
    unit_ff = {"unit_id": "E1U01", "first_frame_asset": "images/first.png"}
    unit_ff_missing = {"unit_id": "E1U02", "first_frame_asset": "images/nope.png"}
    unit_plain = {"unit_id": "E1U03"}
    assert resolve_unit_video_bucket(mixed, unit_ff, project) == "i2v"
    assert resolve_unit_video_bucket(mixed, unit_ff_missing, project) == "r2v"
    assert resolve_unit_video_bucket(mixed, unit_plain, project) == "r2v"
    assert resolve_unit_video_bucket({"generation_mode": "reference_video"}, unit_ff, project) == "r2v"
    assert resolve_unit_video_bucket({"generation_mode": "storyboard"}, unit_ff, project) == "i2v"
    assert resolve_unit_video_bucket({}, unit_ff, project) == "i2v"
