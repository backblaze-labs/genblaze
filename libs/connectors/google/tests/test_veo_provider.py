"""Tests for VeoProvider (mocked — no real API calls)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests


def _make_completed_operation():
    """Create a mock completed operation with a generated video."""
    video = SimpleNamespace(uri="https://storage.googleapis.com/video/out.mp4")
    gv = SimpleNamespace(video=video)
    response = SimpleNamespace(generated_videos=[gv])
    return SimpleNamespace(done=True, name="op-123", error=None, response=response)


def _make_pending_operation():
    return SimpleNamespace(done=False, name="op-123", error=None, response=None)


def _make_completed_gemini_operation(count: int = 1):
    """Gemini Developer API mode: videos come back as Files API references
    (``uri`` set, no inline ``video_bytes``). fetch_output must download each
    to a local file and expose a ``file://`` asset (issue #263)."""
    videos = [
        SimpleNamespace(uri=f"https://storage.googleapis.com/video/out-{i}.mp4", video_bytes=None)
        for i in range(count)
    ]
    response = SimpleNamespace(generated_videos=[SimpleNamespace(video=v) for v in videos])
    return SimpleNamespace(done=True, name="op-123", error=None, response=response)


def _make_completed_vertex_operation(count: int = 1):
    """Vertex AI mode: video bytes come back inline, no Files API ``uri``."""
    videos = [
        SimpleNamespace(uri=None, video_bytes=f"fake-mp4-bytes-{i}".encode()) for i in range(count)
    ]
    response = SimpleNamespace(generated_videos=[SimpleNamespace(video=v) for v in videos])
    return SimpleNamespace(done=True, name="op-123", error=None, response=response)


def _strict_operations_get(operation):
    """Simulate google-genai's real ``Operations.get()`` contract.

    The real SDK does ``operation_name = operation.name`` on its argument —
    it expects an *operation object*, not a bare string. Passing a plain str
    (as ``poll``/``fetch_output`` used to, per issue #136) raises
    ``AttributeError`` here exactly as it does against the live SDK.
    """
    if not hasattr(operation, "name"):
        raise AttributeError("'str' object has no attribute 'name'")
    return _make_completed_operation()


@pytest.fixture
def mock_google():
    """Patch google.genai with a mock client."""
    mock_types = MagicMock()
    mock_types.GenerateVideosConfig = MagicMock

    mock_genai = MagicMock()
    mock_google_mod = MagicMock()
    mock_google_mod.genai = mock_genai

    mock_client = MagicMock()
    mock_client.models.generate_videos.return_value = _make_pending_operation()
    mock_client.operations.get.return_value = _make_completed_operation()

    # Mirror the real SDK's byte-returning ``download(file=video)``: it returns
    # the bytes AND sets ``video.video_bytes`` as a side effect (google/genai/
    # files.py). ``destination=`` is deliberately NOT used — it only exists on
    # google-genai >= 2.21.0, but the connector supports >= 1.0 (issue #263).
    def _download_returns_bytes(*, file, **kwargs):
        assert "destination" not in kwargs, (
            "fetch_output must not pass destination= (needs google-genai>=2.21)"
        )
        data = f"fake-mp4-bytes::{getattr(file, 'uri', '')}".encode()
        file.video_bytes = data  # side effect the real SDK performs
        return data

    mock_client.files.download.side_effect = _download_returns_bytes

    with patch.dict(
        "sys.modules",
        {
            "google": mock_google_mod,
            "google.genai": mock_genai,
            "google.genai.types": mock_types,
        },
    ):
        from genblaze_google import VeoProvider

        provider = VeoProvider(api_key="test-key")
        provider._client = mock_client
        yield provider, mock_client


def test_submit_returns_operation_name(mock_google):
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    # Returns the provider-native operation name, not step_id
    assert pred_id == "op-123"
    client.models.generate_videos.assert_called_once()


def test_poll_returns_false_when_pending(mock_google):
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_pending_operation()
    assert provider.poll(pred_id) is False


def test_poll_returns_true_when_done(mock_google):
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    assert provider.poll(pred_id) is True


def test_fetch_output_attaches_asset(mock_google):
    """Gemini Developer API mode: the credentialed Files API URI must be
    downloaded to a local file and exposed as a file:// asset, so
    ObjectStorageSink/B2 can upload it without Google auth (issue #263).
    It must NOT leak the storage.googleapis.com URI downstream."""
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    # Poll to cache the completed operation
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)

    result = provider.fetch_output(pred_id, step)
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.media_type == "video/mp4"
    assert asset.url.startswith("file://"), (
        "must materialize a local file:// asset, not a Files URI"
    )
    # Bytes were downloaded and written to a real local file. The download must
    # NOT pass destination= — that arg only exists on google-genai>=2.21, but the
    # connector supports >=1.0, so passing it would TypeError on older releases.
    client.files.download.assert_called_once()
    assert "destination" not in client.files.download.call_args.kwargs
    assert Path(urlparse(asset.url).path).read_bytes()


def test_poll_wraps_bare_operation_name_string(mock_google):
    """poll() must not hand a bare str to client.operations.get() — the real
    SDK reads ``.name`` off its argument and raises AttributeError for a
    plain string (issue #136, Vertex AI mode)."""
    provider, client = mock_google
    client.operations.get.side_effect = _strict_operations_get
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    assert provider.poll(pred_id) is True


def test_fetch_output_wraps_bare_operation_name_when_uncached(mock_google):
    """fetch_output()'s uncached fallback (``client.operations.get(prediction_id)``)
    must also wrap the bare operation-name string (issue #136)."""
    provider, client = mock_google
    client.operations.get.side_effect = _strict_operations_get
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    # No poll() call — nothing cached, forces fetch_output to hit operations.get directly.
    result = provider.fetch_output(pred_id, step)
    assert len(result.assets) == 1


def test_fetch_output_vertex_inline_bytes(tmp_path, mock_google):
    """Vertex AI returns video bytes inline (no Files API). fetch_output must
    save them locally and expose a file:// asset instead of calling
    client.files.download(), which raises ValueError on Vertex (issue #136)."""
    provider, client = mock_google
    provider._output_dir = tmp_path
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_vertex_operation()
    provider.poll(pred_id)

    result = provider.fetch_output(pred_id, step)
    client.files.download.assert_not_called()
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.url.startswith("file://")
    assert asset.media_type == "video/mp4"


def test_fetch_output_vertex_inline_bytes_no_output_dir(mock_google):
    """Vertex inline bytes without a configured output_dir fall back to a
    unique tempfile (default provider._output_dir is None)."""
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_vertex_operation()
    provider.poll(pred_id)

    result = provider.fetch_output(pred_id, step)
    client.files.download.assert_not_called()
    assert result.assets[0].url.startswith("file://")


def test_fetch_output_vertex_multiple_videos_no_collision(tmp_path, mock_google):
    """number_of_videos > 1 in Vertex mode must not collide on one output
    path — each generated video needs its own file (issue #136 follow-up:
    the initial fix indexed by step_id alone, overwriting all but the last)."""
    provider, client = mock_google
    provider._output_dir = tmp_path
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="a sunset",
        params={"number_of_videos": "2"},
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_vertex_operation(count=2)
    provider.poll(pred_id)

    result = provider.fetch_output(pred_id, step)
    assert len(result.assets) == 2
    urls = {asset.url for asset in result.assets}
    assert len(urls) == 2, "each video must get a distinct file:// path"
    for asset in result.assets:
        path = urlparse(asset.url).path
        assert Path(path).read_bytes()  # each file actually has its own bytes


def test_fetch_output_gemini_downloads_to_file_url(tmp_path, mock_google):
    """Gemini mode with output_dir set: stream the Files API download to an
    indexed local file and expose a file:// asset (issue #263)."""
    provider, client = mock_google
    provider._output_dir = tmp_path
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_gemini_operation()
    provider.poll(pred_id)

    result = provider.fetch_output(pred_id, step)
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.url.startswith("file://")
    assert asset.media_type == "video/mp4"
    out_file = tmp_path / f"{step.step_id}_0.mp4"
    assert out_file.exists() and out_file.read_bytes()
    # Version-safe download: no destination= (google-genai>=1.0 compatibility).
    assert "destination" not in client.files.download.call_args.kwargs


def test_fetch_output_gemini_no_output_dir(mock_google):
    """Gemini mode without a configured output_dir falls back to a unique
    tempfile, still exposing a file:// asset."""
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_gemini_operation()
    provider.poll(pred_id)

    result = provider.fetch_output(pred_id, step)
    asset = result.assets[0]
    assert asset.url.startswith("file://")
    assert Path(urlparse(asset.url).path).read_bytes()


def test_fetch_output_gemini_multiple_videos_no_collision(tmp_path, mock_google):
    """number_of_videos > 1 in Gemini mode must download each video to its own
    file — no collision on a single output path (mirrors the Vertex case)."""
    provider, client = mock_google
    provider._output_dir = tmp_path
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="a sunset",
        params={"number_of_videos": "2"},
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_gemini_operation(count=2)
    provider.poll(pred_id)

    result = provider.fetch_output(pred_id, step)
    assert len(result.assets) == 2
    urls = {asset.url for asset in result.assets}
    assert len(urls) == 2, "each video must get a distinct file:// path"
    for asset in result.assets:
        assert Path(urlparse(asset.url).path).read_bytes()


# --- Path-traversal / cross-job overwrite hardening (issue #284) ---


@pytest.mark.parametrize(
    "bad_step_id",
    [
        "../../../../tmp/pwned",
        "/tmp/pwned",  # noqa: S108 — attacker-controlled payload, never used as a real path
        "not-a-uuid",
    ],
)
def test_fetch_output_vertex_rejects_invalid_step_id(tmp_path, mock_google, bad_step_id):
    """A non-UUID step_id (traversal, absolute path, or junk) must be
    rejected before any write happens — it must never let the output escape
    output_dir (issue #284)."""
    provider, client = mock_google
    provider._output_dir = tmp_path
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="a sunset",
        step_id=bad_step_id,
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_vertex_operation()
    provider.poll(pred_id)

    with pytest.raises(ProviderError):
        provider.fetch_output(pred_id, step)
    # Nothing must have been written outside output_dir, and output_dir itself
    # must stay empty.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "bad_step_id",
    [
        "../../../../tmp/pwned",
        "/tmp/pwned",  # noqa: S108 — attacker-controlled payload, never used as a real path
        "not-a-uuid",
    ],
)
def test_fetch_output_gemini_rejects_invalid_step_id(tmp_path, mock_google, bad_step_id):
    """Same invalid-step_id containment check on the Gemini download branch."""
    provider, client = mock_google
    provider._output_dir = tmp_path
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="a sunset",
        step_id=bad_step_id,
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_gemini_operation()
    provider.poll(pred_id)

    with pytest.raises(ProviderError):
        provider.fetch_output(pred_id, step)
    assert list(tmp_path.iterdir()) == []


def test_fetch_output_refuses_to_follow_symlink(tmp_path, mock_google):
    """If a symlink is pre-planted at the would-be output path, the write must
    refuse rather than follow it and overwrite the symlink's target."""
    provider, client = mock_google
    provider._output_dir = tmp_path
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")

    secret_target = tmp_path.parent / "secret.mp4"
    secret_target.write_bytes(b"do-not-touch")
    symlink_path = tmp_path / f"{step.step_id}_0.mp4"
    symlink_path.symlink_to(secret_target)

    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_vertex_operation()
    provider.poll(pred_id)

    with pytest.raises(ProviderError):
        provider.fetch_output(pred_id, step)
    assert secret_target.read_bytes() == b"do-not-touch"


def test_fetch_output_refuses_to_overwrite_existing_file(tmp_path, mock_google):
    """A pre-existing regular file at the target path must not be clobbered."""
    provider, client = mock_google
    provider._output_dir = tmp_path
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")

    existing = tmp_path / f"{step.step_id}_0.mp4"
    existing.write_bytes(b"pre-existing-content")

    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_vertex_operation()
    provider.poll(pred_id)

    with pytest.raises(ProviderError):
        provider.fetch_output(pred_id, step)
    assert existing.read_bytes() == b"pre-existing-content"


def test_fetch_output_gemini_refuses_to_follow_symlink(tmp_path, mock_google):
    """Same symlink refusal on the Gemini download branch (mirrors the Vertex
    case): fetch_output must not follow a pre-planted symlink at the target
    path."""
    provider, client = mock_google
    provider._output_dir = tmp_path
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")

    secret_target = tmp_path.parent / "secret-gemini.mp4"
    secret_target.write_bytes(b"do-not-touch")
    symlink_path = tmp_path / f"{step.step_id}_0.mp4"
    symlink_path.symlink_to(secret_target)

    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_gemini_operation()
    provider.poll(pred_id)

    with pytest.raises(ProviderError):
        provider.fetch_output(pred_id, step)
    assert secret_target.read_bytes() == b"do-not-touch"


def test_fetch_output_gemini_refuses_to_overwrite_existing_file(tmp_path, mock_google):
    """Same no-overwrite protection on the Gemini download branch (mirrors the
    Vertex case): a pre-existing file at the target path must not be
    clobbered."""
    provider, client = mock_google
    provider._output_dir = tmp_path
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")

    existing = tmp_path / f"{step.step_id}_0.mp4"
    existing.write_bytes(b"pre-existing-content")

    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_gemini_operation()
    provider.poll(pred_id)

    with pytest.raises(ProviderError):
        provider.fetch_output(pred_id, step)
    assert existing.read_bytes() == b"pre-existing-content"


def test_fetch_output_error_raises(mock_google):
    provider, client = mock_google
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="bad")
    pred_id = provider.submit(step)
    error_op = SimpleNamespace(
        done=True, name="op-err", error="Safety filter triggered", response=None
    )
    client.operations.get.return_value = error_op
    provider.poll(pred_id)

    with pytest.raises(ProviderError, match="Safety filter"):
        provider.fetch_output(pred_id, step)


def test_invoke_full_lifecycle(mock_google):
    """Full invoke() succeeds with mocked client."""
    provider, client = mock_google
    client.operations.get.return_value = _make_completed_operation()

    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_invalid_aspect_ratio_raises(mock_google):
    provider, _ = mock_google
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="test",
        params={"aspect_ratio": "4:3"},
    )
    with pytest.raises(ProviderError, match="Invalid aspect_ratio"):
        provider.submit(step)


def test_invalid_resolution_raises(mock_google):
    provider, _ = mock_google
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="test",
        params={"resolution": "360p"},
    )
    with pytest.raises(ProviderError, match="Invalid resolution"):
        provider.submit(step)


def test_resume_works_with_operation_name(mock_google):
    """resume() works with just the operation name (no in-memory state needed)."""
    provider, client = mock_google
    client.operations.get.return_value = _make_completed_operation()
    step = Step(provider="google-veo", model="veo-2.0-generate-001", prompt="a sunset")
    # resume with just the operation name — no prior submit() needed
    result = provider.resume("op-123", step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_duration_alias(mock_google):
    """Standard 'duration' param is aliased to 'duration_seconds' for Veo."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="test",
        params={"duration": "6"},
    )
    provider.submit(step)
    client.models.generate_videos.assert_called_once()


def test_cost_none_by_default(mock_google):
    """As of genblaze-core 0.3.0 the SDK no longer ships pricing for Veo.
    cost_usd is None unless the user has registered pricing via
    ``provider.models.register_pricing()``. See
    ``docs/reference/pricing-recipes.md`` for the canonical recipe.
    """
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="a sunset",
        params={"duration_seconds": "6"},
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert result.cost_usd is None


def test_cost_tracked_with_user_registered_per_second(mock_google):
    """User-registered per-second strategy reads duration_seconds from params."""
    from genblaze_core.providers import PricingContext, PricingStrategy

    def per_second(rate: float) -> PricingStrategy:
        def _strategy(ctx: PricingContext) -> float | None:
            raw = ctx.step.params.get("duration_seconds") or ctx.step.params.get("duration")
            try:
                dur = int(raw) if raw is not None else 4
            except (TypeError, ValueError):
                dur = 4
            count = ctx.output_count or 1
            return rate * dur * count

        return _strategy

    provider, client = mock_google
    provider._models = provider.models.fork()
    provider.models.register_pricing("veo-3.0-generate-001", per_second(0.50))

    step = Step(
        provider="google-veo",
        model="veo-3.0-generate-001",
        prompt="a sunset",
        params={"duration": "8"},
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert result.cost_usd == pytest.approx(0.50 * 8)


def test_veo3_populates_tracks(mock_google):
    """Veo 3 models populate multi-track metadata on assets."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-3.0-generate-001",
        prompt="a sunset with music",
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.tracks is not None
    assert len(asset.tracks) == 2
    assert asset.tracks[0].kind == "video"
    assert asset.tracks[0].codec == "h264"
    assert asset.tracks[1].kind == "audio"
    assert asset.tracks[1].codec == "aac"
    assert asset.tracks[1].label == "generated-audio"


def test_veo3_fast_populates_tracks(mock_google):
    """Veo 3 fast model also populates tracks."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-3.0-fast-generate-001",
        prompt="quick clip",
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert result.assets[0].tracks is not None
    assert len(result.assets[0].tracks) == 2


def test_veo2_no_tracks(mock_google):
    """Veo 2 models do not populate tracks."""
    provider, client = mock_google
    step = Step(
        provider="google-veo",
        model="veo-2.0-generate-001",
        prompt="a sunset",
    )
    pred_id = provider.submit(step)
    client.operations.get.return_value = _make_completed_operation()
    provider.poll(pred_id)
    result = provider.fetch_output(pred_id, step)
    assert result.assets[0].tracks is None


# --- Compliance harness ---


class TestVeoCompliance(ProviderComplianceTests):
    """Verify VeoProvider satisfies the genblaze provider contract."""

    # SDK no longer ships pricing as of genblaze-core 0.3.0.
    expects_cost = False

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        mock_types = MagicMock()
        mock_types.GenerateVideosConfig = MagicMock
        mock_genai = MagicMock()
        mock_google_mod = MagicMock()
        mock_google_mod.genai = mock_genai
        with patch.dict(
            "sys.modules",
            {
                "google": mock_google_mod,
                "google.genai": mock_genai,
                "google.genai.types": mock_types,
            },
        ):
            yield

    def make_provider(self):
        from genblaze_google import VeoProvider

        mock_client = MagicMock()
        mock_client.models.generate_videos.return_value = _make_pending_operation()
        mock_client.operations.get.return_value = _make_completed_operation()
        # Gemini path: download returns bytes (no destination=); see fixture.
        mock_client.files.download.return_value = b"fake-mp4-bytes"
        provider = VeoProvider(api_key="test-key")
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(
            provider="google-veo",
            model="veo-2.0-generate-001",
            prompt="test prompt",
        )
