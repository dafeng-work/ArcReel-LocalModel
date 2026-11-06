"""Unit-level coverage for the mixed-mode first-frame execution layer:
``resolve_unit_first_frame``, the visual-basis digest wiring, the checkpoint
start_image relaxation, and the artifact-basis first_frame branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.artifacts.video_visual_provenance import (
    build_reference_video_visual_basis,
)
from lib.artifacts.visual_artifact_provenance import (
    build_reference_video_artifact_visual_basis,
)
from lib.script.reference_video.execution_checkpoint import (
    ProviderMediaInput,
    ReferenceSubmissionCheckpoint,
    stage_provider_media,
)
from lib.script.reference_video.request_projection import (
    ResolvedReferenceAsset,
    resolve_unit_first_frame,
)

pytestmark = pytest.mark.integration


def _write(directory: Path, name: str, content: bytes) -> Path:
    full = directory / name
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)
    return full


def test_resolve_unit_first_frame_matrix(tmp_path):
    present_asset = "first_frames/present.png"
    _write(tmp_path, "first_frames/present.png", b"frame")
    mixed = {"generation_mode": "mixed"}
    storyboard = {"generation_mode": "storyboard"}

    resolved = resolve_unit_first_frame(
        mixed,
        {"first_frame_asset": present_asset},
        tmp_path,
    )
    assert resolved is not None
    assert resolved.reference is None
    assert resolved.kind == "first_frame"
    assert resolved.path == tmp_path / "first_frames/present.png"

    assert resolve_unit_first_frame(mixed, {"first_frame_asset": "first_frames/missing.png"}, tmp_path) is None
    assert resolve_unit_first_frame(mixed, {}, tmp_path) is None
    assert resolve_unit_first_frame(storyboard, {"first_frame_asset": present_asset}, tmp_path) is None


def test_visual_basis_digest_covers_first_frame(tmp_path):
    def _basis(first_frame: Path | None) -> object:
        return build_reference_video_visual_basis(
            rendered_prompt="p",
            aspect_ratio="16:9",
            reference_images=(),
            first_frame=first_frame,
        )

    assert _basis(None).digest != _basis(_write(tmp_path, "first.png", b"one")).digest

    first = _write(tmp_path, "first.png", b"one")
    other = _write(tmp_path, "other.png", b"two")
    assert _basis(first).digest != _basis(other).digest


def _currency() -> object:
    from lib.artifacts.artifact_manifest import ArtifactBasis, compose_video_artifact_basis
    from lib.artifacts.video_artifact_facts import VideoArtifactCurrencyFacts
    from lib.speech.speech_artifact_provenance import build_video_duration_basis

    visual = build_reference_video_artifact_visual_basis(
        unit={"unit_id": "E1U1", "text": "line\nvisual: scene"},
        request_assets=(),
        style=None,
        aspect_ratio="16:9",
    )
    speech = ArtifactBasis.build(
        "artifact-speech/video",
        kind_version=1,
        inputs={
            "mode": "character_speech",
            "utterances": [{"speaker": "Ali", "text": "Let's go."}],
            "voices": [{"speaker": "Ali", "voice_style": "", "reference_audio_digest": None}],
        },
    )
    duration = build_video_duration_basis(10)
    return VideoArtifactCurrencyFacts(
        episode=1,
        request_duration_seconds=10,
        visual_basis=visual,
        speech_basis=speech,
        duration_basis=duration,
        video_basis=compose_video_artifact_basis(visual=visual, speech=speech, duration=duration),
        voice_style_speakers=(),
        duration_tiers=(10,),
        reference_image_limit=3,
        parent_version=1,
    )


def test_reference_checkpoint_accepts_single_start_image_and_rejects_extra(tmp_path):
    first = _write(tmp_path, "start.png", b"start-bytes")
    extra = _write(tmp_path, "extra.png", b"extra-bytes")
    ref = _write(tmp_path, "ref.png", b"ref-bytes")

    inputs = (
        ProviderMediaInput(
            path=ref,
            role="reference_image",
            logical_type="asset",
            logical_name="assets/refs/ref.png",
            kind="asset",
        ),
        ProviderMediaInput(
            path=first,
            role="start_image",
            logical_type="unit",
            logical_name="E1U1",
            kind="first_frame",
        ),
    )
    checkpoint = ReferenceSubmissionCheckpoint.create(
        task_id="task-1",
        project_name="demo",
        script_file="scripts/episode_1.json",
        unit_id="E1U1",
        generation_type="r2v",
        provider_id="custom-7",
        provider_model_id="cinema-v1",
        backend_model_id="cinema-v1-resolved",
        endpoint_guard="dashscope-async-video",
        prompt="actual execution prompt",
        duration_seconds=10,
        aspect_ratio="16:9",
        resolution="1080p",
        generate_audio=False,
        service_tier="default",
        seed=None,
        reference_audio_targets=None,
        visual_basis_digest="a" * 64,
        artifact_currency=_currency(),
        media=stage_provider_media(tmp_path, "task-1", inputs),
    )
    assert checkpoint.generation_type == "r2v"
    assert checkpoint.visual_basis_digest == "a" * 64

    with pytest.raises(ValueError, match="multiple start images"):
        ReferenceSubmissionCheckpoint.create(
            task_id="task-2",
            project_name="demo",
            script_file="scripts/episode_1.json",
            unit_id="E1U1",
            generation_type="r2v",
            provider_id="custom-7",
            provider_model_id="cinema-v1",
            backend_model_id="cinema-v1-resolved",
            endpoint_guard="dashscope-async-video",
            prompt="actual execution prompt",
            duration_seconds=10,
            aspect_ratio="16:9",
            resolution="1080p",
            generate_audio=False,
            service_tier="default",
            seed=None,
            reference_audio_targets=None,
            visual_basis_digest="a" * 64,
            artifact_currency=_currency(),
            media=stage_provider_media(
                tmp_path,
                "task-2",
                (
                    *inputs,
                    ProviderMediaInput(
                        path=extra, role="start_image", logical_type="unit", logical_name="E1U1", kind="first_frame"
                    ),
                ),
            ),
        )


def test_artifact_basis_records_first_frame_without_reference(tmp_path):
    unit = {"unit_id": "E1U1", "text": "line\nvisual: scene"}
    ref = _write(tmp_path, "ref.png", b"ref-bytes")
    frame = _write(tmp_path, "start.png", b"frame-bytes")
    reference_asset = ResolvedReferenceAsset(
        path=ref,
        reference=_StubReference(),
    )
    first_frame_asset = ResolvedReferenceAsset(path=frame, kind="first_frame")

    without = build_reference_video_artifact_visual_basis(
        unit=unit,
        request_assets=(reference_asset,),
        style=None,
        aspect_ratio="16:9",
    )
    with_frame = build_reference_video_artifact_visual_basis(
        unit=unit,
        request_assets=(first_frame_asset, reference_asset),
        style=None,
        aspect_ratio="16:9",
    )
    assert without.digest != with_frame.digest


class _StubReference:
    type = "image"
    name = "ref.png"
