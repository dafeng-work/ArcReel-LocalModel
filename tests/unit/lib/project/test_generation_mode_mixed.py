"""混合模式（mixed）的 lib 层契约：模式值集、家族谓词、单向转换闸、骨架判族。"""

from __future__ import annotations

import pytest

from lib.project.project_manager import (
    VALID_GENERATION_MODES,
    is_reference_family_project,
    is_reference_video_project,
    validate_generation_mode_transition,
)
from lib.script.script_skeleton import ensure_route_skeleton, resolve_declared_kind


class TestModeValues:
    def test_valid_modes_exactly_three(self):
        assert VALID_GENERATION_MODES == frozenset({"storyboard", "reference_video", "mixed"})

    def test_data_validator_derives_from_same_set(self):
        from lib.project.data_validator import DataValidator

        assert DataValidator.VALID_GENERATION_MODES == set(VALID_GENERATION_MODES)


class TestReferenceFamilyPredicate:
    @pytest.mark.parametrize(
        ("mode", "family", "strict"),
        [
            ("reference_video", True, True),
            ("mixed", True, False),
            ("storyboard", False, False),
        ],
    )
    def test_family_vs_strict_split(self, mode, family, strict):
        project = {"generation_mode": mode}
        assert is_reference_family_project(project) is family
        assert is_reference_video_project(project) is strict

    def test_missing_mode_neither_family(self):
        assert is_reference_family_project({}) is False
        assert is_reference_video_project({}) is False


class TestGenerationModeTransitionGate:
    @pytest.mark.parametrize(
        ("current", "new"),
        [
            ("storyboard", "storyboard"),
            ("reference_video", "reference_video"),
            ("mixed", "mixed"),
            ("storyboard", "mixed"),
            ("reference_video", "mixed"),
        ],
    )
    def test_allowed_transitions(self, current, new):
        validate_generation_mode_transition(current, new)

    @pytest.mark.parametrize(
        ("current", "new"),
        [
            ("mixed", "storyboard"),
            ("mixed", "reference_video"),
            ("storyboard", "reference_video"),
            ("reference_video", "storyboard"),
            (None, "mixed"),
            ("storyboard", None),
        ],
    )
    def test_rejected_transitions(self, current, new):
        with pytest.raises(ValueError):
            validate_generation_mode_transition(current, new)


class TestSkeletonRouting:
    def test_mixed_declares_video_units_like_reference_video(self):
        assert resolve_declared_kind("ad", "mixed") == "video_units"
        assert resolve_declared_kind("drama", "mixed") == "video_units"
        assert resolve_declared_kind("narration", "mixed") == "video_units"

    def test_storyboard_routing_unchanged(self):
        assert resolve_declared_kind("ad", "storyboard") == "shots"
        assert resolve_declared_kind("drama", "storyboard") == "scenes"

    def test_mixed_skeleton_gate_accepts_video_units(self):
        assert ensure_route_skeleton({"content_mode": "ad", "video_units": []}, "ad", "mixed") == "video_units"
