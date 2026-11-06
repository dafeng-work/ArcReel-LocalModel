"""projects 路由的生成模式转换闸：mixed 为单向汇点，转换走 PATCH 结构化拒绝。"""

import pytest

from tests.integration.server.routers.projects_router_support import _FakePM, build_projects_client


class TestGenerationModeTransition:
    def test_create_accepts_mixed(self, tmp_path, monkeypatch):
        client = build_projects_client(monkeypatch, _FakePM(tmp_path))
        with client:
            resp = client.post(
                "/api/v1/projects",
                json={"name": "mix-1", "title": "混合", "generation_mode": "mixed"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["project"]["generation_mode"] == "mixed"

    def test_legacy_grid_still_rejected(self, tmp_path, monkeypatch):
        client = build_projects_client(monkeypatch, _FakePM(tmp_path))
        with client:
            resp = client.post(
                "/api/v1/projects",
                json={"name": "grid-1", "title": "X", "generation_mode": "grid"},
            )
            assert resp.status_code == 422

    def test_storyboard_to_mixed_allowed_and_clears_grid_storyboard(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        fake_pm.project_data["ready"]["grid_storyboard"] = True
        client = build_projects_client(monkeypatch, fake_pm)
        with client:
            resp = client.patch("/api/v1/projects/ready", json={"generation_mode": "mixed"})
            assert resp.status_code == 200, resp.text
            data = fake_pm.project_data["ready"]
            assert data["generation_mode"] == "mixed"
            assert data["grid_storyboard"] is False

    def test_reference_video_to_mixed_allowed(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        fake_pm.project_data["ready"]["generation_mode"] = "reference_video"
        client = build_projects_client(monkeypatch, fake_pm)
        with client:
            resp = client.patch("/api/v1/projects/ready", json={"generation_mode": "mixed"})
            assert resp.status_code == 200, resp.text
            assert fake_pm.project_data["ready"]["generation_mode"] == "mixed"

    @pytest.mark.parametrize("target", ["storyboard", "reference_video"])
    def test_mixed_is_terminal_sink(self, tmp_path, monkeypatch, target):
        fake_pm = _FakePM(tmp_path)
        fake_pm.project_data["ready"]["generation_mode"] = "mixed"
        client = build_projects_client(monkeypatch, fake_pm)
        with client:
            resp = client.patch("/api/v1/projects/ready", json={"generation_mode": target})
            assert resp.status_code == 400
            assert "generation_mode_transition_not_allowed" in resp.text
            assert fake_pm.project_data["ready"]["generation_mode"] == "mixed"

    @pytest.mark.parametrize("from_mode", ["storyboard", "reference_video"])
    def test_original_modes_cannot_swap(self, tmp_path, monkeypatch, from_mode):
        fake_pm = _FakePM(tmp_path)
        fake_pm.project_data["ready"]["generation_mode"] = from_mode
        client = build_projects_client(monkeypatch, fake_pm)
        with client:
            resp = client.patch("/api/v1/projects/ready", json={"generation_mode": "mixed"})
            assert resp.status_code == 200
            resp = client.patch("/api/v1/projects/ready", json={"generation_mode": from_mode})
            assert resp.status_code == 400
            assert "generation_mode_transition_not_allowed" in resp.text

    def test_explicit_null_mode_rejected(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)
        with client:
            resp = client.patch("/api/v1/projects/ready", json={"generation_mode": None})
            assert resp.status_code == 400
            assert "generation_mode_cannot_clear" in resp.text
            assert fake_pm.project_data["ready"]["generation_mode"] == "storyboard"

    def test_same_mode_patch_is_idempotent(self, tmp_path, monkeypatch):
        fake_pm = _FakePM(tmp_path)
        client = build_projects_client(monkeypatch, fake_pm)
        with client:
            resp = client.patch("/api/v1/projects/ready", json={"generation_mode": "storyboard"})
            assert resp.status_code == 200
            assert fake_pm.project_data["ready"]["generation_mode"] == "storyboard"

    def test_mixed_conversion_clears_grid_even_when_same_request_sets_it(self, tmp_path, monkeypatch):
        """同一请求里既转 mixed 又开宫格：宫格不得被写回（mixed 项目无宫格语义）。"""
        fake_pm = _FakePM(tmp_path)
        fake_pm.project_data["ready"]["grid_storyboard"] = True
        client = build_projects_client(monkeypatch, fake_pm)
        with client:
            resp = client.patch(
                "/api/v1/projects/ready",
                json={"generation_mode": "mixed", "grid_storyboard": True},
            )
            assert resp.status_code == 200, resp.text
            data = fake_pm.project_data["ready"]
            assert data["generation_mode"] == "mixed"
            assert data["grid_storyboard"] is False

    def test_grid_toggle_still_works_without_mode_change(self, tmp_path, monkeypatch):
        """不含模式变更的请求里，宫格开关照常生效（本次修复不影响既有行为）。"""
        fake_pm = _FakePM(tmp_path)
        fake_pm.project_data["ready"]["grid_storyboard"] = False
        client = build_projects_client(monkeypatch, fake_pm)
        with client:
            resp = client.patch("/api/v1/projects/ready", json={"grid_storyboard": True})
            assert resp.status_code == 200, resp.text
            assert fake_pm.project_data["ready"]["grid_storyboard"] is True
