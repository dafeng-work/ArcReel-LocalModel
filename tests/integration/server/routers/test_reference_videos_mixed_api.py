"""混合模式 API 层：批量排队整批结构化拒绝 + patch_unit 首帧入口的模式闸。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.project.project_manager import ProjectManager
from server.auth import CurrentUserInfo, get_current_user
from tests.auth_deps import AUTH_DEPENDENCIES
from tests.fakes import fake_reference_request_projector

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x04\x00\x00\x00\x04"
    b"\x08\x02\x00\x00\x00&\x93\t)\x00\x00\x00\x13IDATx\x9cc<\x91b\xc4\x00"
    b"\x03Lp\x16^\x0e\x00E\xf6\x01f\xac\xf5\x15\xfa\x00\x00\x00\x00IEND\xaeB`\x82"
)

_SCRIPT_FILE = "episode_1.json"
_UNIT_ID = "E1U1"


class _Harness:
    def __init__(self, client: TestClient, pm: ProjectManager, project_dir: Path) -> None:
        self.client = client
        self.pm = pm
        self.project_dir = project_dir

    def unit_on_disk(self) -> dict[str, Any]:
        script = json.loads((self.project_dir / "scripts" / _SCRIPT_FILE).read_text(encoding="utf-8"))
        return next(u for u in script["video_units"] if u["unit_id"] == _UNIT_ID)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    projects_root = tmp_path / "projects"
    pm = ProjectManager(str(projects_root))
    pm.create_project("demo")
    pm.create_project_metadata("demo", "Demo", "写实", "narration")
    project_dir = pm.get_project_path("demo")
    frame_dir = project_dir / "first_frames"
    frame_dir.mkdir()
    (frame_dir / "u1.png").write_bytes(_TINY_PNG)

    def _seed(project: dict[str, Any]) -> None:
        project["generation_mode"] = "reference_video"
        project["episodes"] = [{"episode": 1, "title": "雨夜", "script_file": f"scripts/{_SCRIPT_FILE}"}]

    pm.update_project("demo", _seed)
    (project_dir / "scripts").mkdir(exist_ok=True)
    (project_dir / "scripts" / _SCRIPT_FILE).write_text(
        json.dumps(
            {
                "episode": 1,
                "title": "雨夜",
                "content_mode": "narration",
                "generation_mode": "reference_video",
                "summary": "雨夜相遇",
                "novel": {"title": "N", "chapter": "1"},
                "video_units": [
                    {
                        "unit_id": _UNIT_ID,
                        "duration_seconds": 8,
                        "text": "镜头1：雨落在石板上",
                        "generated_assets": {"status": "pending", "video_clip": None},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from server.routers import reference_videos as router_mod

    monkeypatch.setattr(router_mod, "get_project_manager", lambda: pm)
    monkeypatch.setattr(
        router_mod,
        "project_reference_unit_request",
        fake_reference_request_projector(durations=(4, 8, 12)),
    )

    async def _fake_caps(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"voice_consistency": "soft", "requested_generate_audio": True, "max_reference_audio_count": 2}

    monkeypatch.setattr(router_mod, "project_video_caps", _fake_caps)

    app = FastAPI()
    from server.error_handlers import register_error_handlers

    register_error_handlers(app)
    app.include_router(router_mod.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="u1", sub="test", role="admin")
    return _Harness(client=TestClient(app), pm=pm, project_dir=project_dir)


def test_patch_first_frame_rejected_outside_mixed(harness: _Harness) -> None:
    resp = harness.client.patch(
        f"/api/v1/projects/demo/reference-videos/episodes/1/units/{_UNIT_ID}",
        json={"first_frame_asset": "first_frames/u1.png"},
    )
    assert resp.status_code == 400
    assert "first_frame_asset_requires_mixed_mode" in resp.text
    assert "first_frame_asset" not in harness.unit_on_disk()


def test_patch_first_frame_sets_and_clears_in_mixed(harness: _Harness) -> None:
    harness.pm.update_project("demo", lambda p: p.update(generation_mode="mixed"))

    resp = harness.client.patch(
        f"/api/v1/projects/demo/reference-videos/episodes/1/units/{_UNIT_ID}",
        json={"first_frame_asset": "first_frames/u1.png"},
    )
    assert resp.status_code == 200, resp.text
    assert harness.unit_on_disk()["first_frame_asset"] == "first_frames/u1.png"

    resp = harness.client.patch(
        f"/api/v1/projects/demo/reference-videos/episodes/1/units/{_UNIT_ID}",
        json={"first_frame_asset": ""},
    )
    assert resp.status_code == 200, resp.text
    assert not harness.unit_on_disk().get("first_frame_asset")


def test_generate_batch_refused_wholesale_in_mixed(harness: _Harness) -> None:
    harness.pm.update_project("demo", lambda p: p.update(generation_mode="mixed"))

    resp = harness.client.post(
        "/api/v1/projects/demo/reference-videos/episodes/1/units/generate-batch",
        json={"unit_ids": [_UNIT_ID]},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["task_ids"] == []
    assert "generation_mixed_mode_batch_not_supported" in json.dumps(payload, ensure_ascii=False)
