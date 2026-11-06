"""混合模式缺陷修复的回归测试（review 2026-11-06 的 P0-1 / P0-2 / P0-3 / P2）。

四个缺陷各自锁定：

- **P0-1**：执行层与报价层的 projector 未传 ``project_path`` → mixed 带首帧的单元被
  ``first_frame_asset_missing`` 硬阻断（首帧路线根本跑不通）。
- **P0-2**：执行层与投影层对同一 mixed 单元给出不同桶 → lane 事实解析以
  ``reference_capability_unavailable`` 阻断一个本可生成的单元。
- **P0-3**：``finish_script_document`` 未随 mixed 扩展 → mixed 项目的 drama 内容类型
  剧本被静默标成 narration。
- **P2**：同一请求里 ``generation_mode=mixed`` 与 ``grid_storyboard=true`` 并存 → 模式
  转换刚清掉的宫格开关被随后分支写回，落盘出 mixed 项目带 grid=true 的矛盾状态。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from lib.script.reference_video.request_projection import (
    FilesystemReferenceAssets,
    ReferenceUnitRequestProjector,
    ResolvedReferenceAsset,
    VideoRequestFactsResult,
    hydrate_unit_references,
)
from lib.script.reference_video.units import resolve_unit_video_bucket
from lib.script.script_document import finish_script_document
from lib.script.script_models import ReferenceResource, ReferenceVideoScript
from tests.factories import make_video_request_facts


def _project(generation_mode: str = "mixed") -> dict:
    return {
        "generation_mode": generation_mode,
        "content_mode": "drama",
        "products": {},
        "scenes": {},
        "characters": {},
        "props": {},
    }


def _i2v_facts():
    """首帧桶事实：first_frame=True（fl2va 端点），text_to_video=False。"""
    return make_video_request_facts(
        route="reference_video",
        generation_type="i2v",
        first_frame=True,
        text_to_video=False,
        max_reference_images=0,
        supported_durations=(8,),
        allowed_durations=(8,),
    )


class _FixedFacts:
    def __init__(self, facts) -> None:
        self._facts = facts

    async def __call__(self, generation_type: str) -> VideoRequestFactsResult:
        return self._facts


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")
    return path


# ---------------------------------------------------------------------------
# P0-1：首帧水合需要 project_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_1_first_frame_unit_not_blocked_without_project_path(tmp_path: Path) -> None:
    """投影器未传 project_path 时不得把已声明的首帧判为缺失（执行层/报价层同款调用形态）。"""
    _write(tmp_path / "assets/first/U13.png")
    unit = {"unit_id": "U13", "text": "镜头", "duration_seconds": 8, "first_frame_asset": "assets/first/U13.png"}

    projector = ReferenceUnitRequestProjector(
        _FixedFacts(_i2v_facts()),
        FilesystemReferenceAssets(tmp_path),
    )
    projection = await projector.project_current(
        project=_project(),
        script={"video_units": [unit]},
        unit=unit,
        resolved_assets=[],
    )

    assert "first_frame_asset_missing" not in {problem.code for problem in projection.problems}
    assert projection.first_frame_asset == "assets/first/U13.png"
    assert projection.hydrated_generation_type == "i2v"


@pytest.mark.asyncio
async def test_p0_1_first_frame_missing_file_still_blocked(tmp_path: Path) -> None:
    """首帧字段已设但文件缺失 → 仍按 first_frame_asset_missing 阻断（不静默换桶）。"""
    unit = {"unit_id": "U13", "text": "镜头", "duration_seconds": 8, "first_frame_asset": "assets/first/gone.png"}

    projector = ReferenceUnitRequestProjector(
        _FixedFacts(_i2v_facts()),
        FilesystemReferenceAssets(tmp_path),
        project_path=tmp_path,
    )
    projection = await projector.project_current(
        project=_project(),
        script={"video_units": [unit]},
        unit=unit,
        resolved_assets=[],
    )

    assert "first_frame_asset_missing" in {problem.code for problem in projection.problems}
    assert projection.blocking_problems


# ---------------------------------------------------------------------------
# P0-2：投影层与执行层对同一 mixed 单元给出同一个桶
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first_frame_asset", "with_references"),
    [
        ("assets/first/U13.png", False),
        ("assets/first/U13.png", True),
        (None, True),
        (None, False),
    ],
)
def test_p0_2_projection_and_execution_agree_on_mixed_bucket(
    tmp_path: Path,
    first_frame_asset: str | None,
    with_references: bool,
) -> None:
    """投影层 hydrated 桶必须与执行层 resolve_unit_video_bucket 一致，否则 lane 事实解析阻断。"""
    unit = {"unit_id": "U13", "text": "镜头", "duration_seconds": 8}
    if first_frame_asset is not None:
        _write(tmp_path / first_frame_asset)
        unit["first_frame_asset"] = first_frame_asset

    # 投影层的「有参考图」事实来自解析出的可用资产，执行层同一个事实来自水合结果：
    # 两侧喂同一份资产集合，才比的是桶判据本身。
    resolved_assets = []
    if with_references:
        reference_path = _write(tmp_path / "assets/refs/手袋.png")
        resolved_assets = [
            ResolvedReferenceAsset(
                reference=ReferenceResource(type="product", name="手袋"),
                path=reference_path,
            )
        ]

    hydration = hydrate_unit_references(
        _project(),
        unit,
        resolved_assets,
        FilesystemReferenceAssets(tmp_path),
        project_path=tmp_path,
    )
    execution_bucket = resolve_unit_video_bucket(
        _project(),
        unit,
        tmp_path,
        with_references=bool(hydration.available_assets),
    )
    assert hydration.hydrated_generation_type == execution_bucket


def test_p0_2_mixed_without_first_frame_uses_reference_facts(tmp_path: Path) -> None:
    """mixed 单元无首帧时：有参考图 → r2v；无参考图 → 与参考生视频同规则降级 i2v。"""
    unit = {"unit_id": "U13", "text": "镜头", "duration_seconds": 8}
    assert resolve_unit_video_bucket(_project(), unit, tmp_path, with_references=True) == "r2v"
    assert resolve_unit_video_bucket(_project(), unit, tmp_path, with_references=False) == "i2v"


def test_p0_2_mixed_with_first_frame_takes_i2v_even_with_references(tmp_path: Path) -> None:
    """mixed 单元有可用首帧时恒 i2v（首帧优先于参考图）。"""
    _write(tmp_path / "assets/first/U13.png")
    unit = {"unit_id": "U13", "text": "镜头", "duration_seconds": 8, "first_frame_asset": "assets/first/U13.png"}
    assert resolve_unit_video_bucket(_project(), unit, tmp_path, with_references=True) == "i2v"
    assert resolve_unit_video_bucket(_project(), unit, tmp_path, with_references=False) == "i2v"


def test_p0_2_reference_video_bucket_keeps_existing_degradation(tmp_path: Path) -> None:
    """参考生视频模式行为不变：有参考图 r2v、无参考图降级 i2v。"""
    unit = {"unit_id": "U13", "text": "镜头", "duration_seconds": 8}
    project = _project("reference_video")
    assert resolve_unit_video_bucket(project, unit, tmp_path, with_references=True) == "r2v"
    assert resolve_unit_video_bucket(project, unit, tmp_path, with_references=False) == "i2v"


def test_p0_2_first_frame_directory_is_not_available(tmp_path: Path) -> None:
    """首帧路径指向目录时判不可用：执行层与投影层判据须一致（否则桶与阻断结论分裂）。"""
    (tmp_path / "assets" / "first").mkdir(parents=True)
    unit = {"unit_id": "U13", "text": "镜头", "duration_seconds": 8, "first_frame_asset": "assets/first"}
    assert resolve_unit_video_bucket(_project(), unit, tmp_path, with_references=False) == "i2v"

    hydration = hydrate_unit_references(
        _project(),
        unit,
        [],
        FilesystemReferenceAssets(tmp_path),
        project_path=tmp_path,
    )
    assert "first_frame_asset_missing" in {problem.code for problem in hydration.problems}


# ---------------------------------------------------------------------------
# P0-3：剧本 content_mode 随 mixed 强制覆盖
# ---------------------------------------------------------------------------


def test_p0_3_mixed_script_keeps_project_content_mode() -> None:
    """mixed 项目（参考家族）的剧本 content_mode 取项目真值，不被 Pydantic 默认值覆盖成 narration。"""
    for generation_mode in ("reference_video", "mixed"):
        script = ReferenceVideoScript(title="E1", video_units=[]).model_dump(mode="json")
        finished = finish_script_document(
            dict(script),
            project={"generation_mode": generation_mode, "content_mode": "drama"},
            episode=1,
            generator="test",
        )
        assert finished["content_mode"] == "drama", generation_mode


def test_p0_3_storyboard_script_keeps_content_mode() -> None:
    """分镜路线（非参考家族）的剧本 content_mode 不被强制覆盖，行为不受本次修复影响。"""
    script = ReferenceVideoScript(title="E1", video_units=[]).model_dump(mode="json")
    finished = finish_script_document(
        dict(script),
        project={"generation_mode": "storyboard", "content_mode": "drama"},
        episode=1,
        generator="test",
    )
    # 非参考家族走 setdefault 分支：脚本自带值原样保留（参考剧本模型默认 narration）。
    assert finished["content_mode"] == "narration"


# ---------------------------------------------------------------------------
# P0-1 端到端：mixed 带首帧的单元在真实执行路径上可生成
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_1_mixed_first_frame_unit_executes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mixed 项目带首帧的单元走真实 execute_reference_video_task 成功路径（首帧作为 start_image 暂存）。"""
    from server.services.tasks import reference_video_tasks as rvt
    from tests.integration.server.services.tasks.reference_video_tasks_support import (
        _TINY_PNG,
        write_project,
    )
    from tests.integration.server.services.tasks.test_execute_reference_video_task import (
        _wire_context,
        _wire_locked_script,
    )

    proj_dir = write_project(tmp_path)
    project = json.loads((proj_dir / "project.json").read_text(encoding="utf-8"))
    project["generation_mode"] = "mixed"
    (proj_dir / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    script = json.loads((proj_dir / "scripts" / "episode_1.json").read_text(encoding="utf-8"))
    script["generation_mode"] = "mixed"
    script["video_units"][0]["first_frame_asset"] = "first_frames/E1U1.png"
    (proj_dir / "scripts" / "episode_1.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    (proj_dir / "first_frames").mkdir(exist_ok=True)
    (proj_dir / "first_frames" / "E1U1.png").write_bytes(_TINY_PNG)

    fake_pm = MagicMock()
    fake_pm.load_project.return_value = project
    fake_pm.get_project_path.return_value = proj_dir
    fake_pm.load_script.side_effect = lambda _name, _filename: json.loads(
        (proj_dir / "scripts" / "episode_1.json").read_text(encoding="utf-8")
    )
    _wire_locked_script(fake_pm)
    monkeypatch.setattr(rvt, "get_project_manager", lambda: fake_pm)

    seen: dict = {}

    async def _fake_generate_video_async(**kwargs):
        seen.update(kwargs)
        out = proj_dir / "reference_videos" / "E1U1.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00\x00\x00 ftypmp42")
        return out, 1, None, None

    fake_generator = MagicMock()
    fake_generator.generate_video_async = AsyncMock(side_effect=_fake_generate_video_async)
    fake_generator.versions.get_versions.return_value = {"versions": [{"created_at": "2026-04-17T10:00:00"}]}
    _wire_context(
        monkeypatch,
        rvt,
        fake_generator,
        backend_name="ark",
        backend_model="doubao-seedance-2-0-260128",
        max_refs=0,
        text_to_video=False,
        supported_durations=(3,),
    )

    async def _fake_extract(*_args, **_kwargs):
        return True

    monkeypatch.setattr(rvt, "extract_video_thumbnail", _fake_extract)

    await rvt.execute_reference_video_task("demo", "E1U1", {"script_file": "scripts/episode_1.json"}, user_id="u1")

    # 首帧随请求下发（start_image 槽），不占参考图槽。
    assert seen.get("start_image") is not None
    assert not seen.get("reference_images")
