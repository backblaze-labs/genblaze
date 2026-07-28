"""Tests for SoraProvider (mocked — no real API calls)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests

# SoraProvider reuses DalleProvider's SSRF-pinned https downloader for
# image-to-video chain inputs (see genblaze_openai/provider.py:submit()).
_SORA_CONN_PATCH = "genblaze_openai.dalle.open_pinned_https_connection"


def _fake_pinned_conn(body: bytes) -> MagicMock:
    """Minimal http.client-like connection that yields ``body`` once."""
    conn = MagicMock()
    resp = MagicMock()
    resp.status = 200
    resp.read.side_effect = [body, b""]
    conn.getresponse.return_value = resp
    return conn


@pytest.fixture
def mock_openai(tmp_path):
    """Patch openai module with a mock client.

    ``output_dir=tmp_path`` gives ``_resolve_local_file`` an allowed root for
    the file:// image-input tests below (mirrors DalleProvider's
    ``mock_b64_dalle`` fixture in test_dalle_provider.py).
    """
    mock_client = MagicMock()

    # Mock videos.create → returns job with id
    mock_client.videos.create.return_value = SimpleNamespace(id="vid-abc123")

    # Mock videos.retrieve → returns completed video
    mock_client.videos.retrieve.return_value = SimpleNamespace(
        id="vid-abc123",
        status="completed",
        model="sora-2",
    )

    # Mock videos.download_content → returns writable response.
    # openai SDK 2.x renamed videos.content → videos.download_content; the old
    # name is gone, so make any lingering .content call blow up like the real
    # SDK would (a bare MagicMock would silently auto-create it) (#127).
    mock_content = MagicMock()

    def _write_video(path):
        with open(path, "wb") as f:
            f.write(b"video")

    mock_content.write_to_file = MagicMock(side_effect=_write_video)
    mock_client.videos.download_content.return_value = mock_content
    mock_client.videos.content.side_effect = AttributeError(
        "'Videos' object has no attribute 'content'"
    )

    with patch.dict("sys.modules", {"openai": MagicMock()}):
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test-key", output_dir=str(tmp_path))
        provider._client = mock_client
        yield provider, mock_client


def test_submit_returns_video_id(mock_openai):
    provider, client = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="a sunset")
    vid_id = provider.submit(step)
    assert vid_id == "vid-abc123"
    client.videos.create.assert_called_once()


def test_poll_returns_true_on_completed(mock_openai):
    provider, _ = mock_openai
    assert provider.poll("vid-abc123") is True


def test_poll_returns_false_on_in_progress(mock_openai):
    provider, client = mock_openai
    client.videos.retrieve.return_value = SimpleNamespace(id="vid-abc123", status="in_progress")
    assert provider.poll("vid-abc123") is False


def test_fetch_output_attaches_asset(mock_openai):
    provider, _ = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="a sunset")
    result = provider.fetch_output("vid-abc123", step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "video/mp4"
    # Now saves locally as file:// URI instead of unauthenticated API URL
    assert result.assets[0].url.startswith("file://")


def test_fetch_output_uses_download_content(mock_openai):
    """Download must go through videos.download_content, not the removed
    videos.content, and keep passing variant='video' (#127)."""
    provider, client = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="a sunset")
    provider.fetch_output("vid-abc123", step)
    client.videos.download_content.assert_called_once_with("vid-abc123", variant="video")
    client.videos.content.assert_not_called()


def test_fetch_output_failed_raises(mock_openai):
    provider, client = mock_openai
    client.videos.retrieve.return_value = SimpleNamespace(
        id="vid-abc123", status="failed", error="Content policy violation"
    )
    step = Step(provider="openai-sora", model="sora-2", prompt="bad prompt")
    with pytest.raises(ProviderError, match="Content policy violation"):
        provider.fetch_output("vid-abc123", step)


def test_invoke_full_lifecycle(mock_openai):
    """Full invoke() succeeds with mocked client."""
    provider, _ = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="a sunset")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_invalid_seconds_raises(mock_openai):
    provider, _ = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="test", params={"seconds": 7})
    with pytest.raises(ProviderError, match="Invalid seconds"):
        provider.submit(step)


def test_invalid_size_raises(mock_openai):
    provider, _ = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="test", params={"size": "500x500"})
    with pytest.raises(ProviderError, match="Invalid size"):
        provider.submit(step)


def test_submit_with_https_image_input(mock_openai):
    """Image-to-video: a remote https:// image is downloaded and forwarded as
    ``input_reference`` — ``videos.create`` has no ``image`` kwarg (#126)."""
    from genblaze_core.models.asset import Asset

    provider, client = mock_openai
    img = Asset(url="https://example.com/frame.png", media_type="image/png")
    step = Step(provider="openai-sora", model="sora-2", prompt="animate this", inputs=[img])

    # The real SDK reads the file body synchronously during create(); capture
    # it here too, since our own `finally` closes the handle once submit()
    # returns (matches DalleProvider's file-handle-in-finally convention).
    captured = {}

    def _capture(**kwargs):
        captured["bytes"] = kwargs["input_reference"].read()
        return SimpleNamespace(id="vid-abc123")

    client.videos.create.side_effect = _capture

    with patch(_SORA_CONN_PATCH, return_value=_fake_pinned_conn(b"remote-png-bytes")):
        vid_id = provider.submit(step)

    assert vid_id == "vid-abc123"
    call_kwargs = client.videos.create.call_args[1]
    assert "image" not in call_kwargs
    assert captured["bytes"] == b"remote-png-bytes"


def test_submit_with_local_file_image_input(mock_openai, tmp_path):
    """Chain image-to-video: a local file:// temp path (upstream step output,
    pre-sink-upload) is read and uploaded via ``input_reference`` (#126)."""
    from genblaze_core.models.asset import Asset

    provider, client = mock_openai
    img_path = tmp_path / "frame.png"
    img_path.write_bytes(b"local-png-bytes")
    img = Asset(url=f"file://{img_path}", media_type="image/png")
    step = Step(provider="openai-sora", model="sora-2", prompt="animate this", inputs=[img])

    captured = {}

    def _capture(**kwargs):
        captured["bytes"] = kwargs["input_reference"].read()
        return SimpleNamespace(id="vid-abc123")

    client.videos.create.side_effect = _capture

    vid_id = provider.submit(step)

    assert vid_id == "vid-abc123"
    call_kwargs = client.videos.create.call_args[1]
    assert "image" not in call_kwargs
    assert captured["bytes"] == b"local-png-bytes"


def test_submit_stringifies_seconds_for_sdk(mock_openai):
    """openai SDK's VideoSeconds is Literal['4', '8', '12'] — must be str, not int (#126)."""
    provider, client = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="a sunset", params={"seconds": 8})
    provider.submit(step)
    call_kwargs = client.videos.create.call_args[1]
    assert call_kwargs["seconds"] == "8"
    assert isinstance(call_kwargs["seconds"], str)


def test_submit_without_inputs_still_works(mock_openai):
    """Text-only generation works when no inputs are provided."""
    provider, client = mock_openai
    step = Step(provider="openai-sora", model="sora-2", prompt="a sunset over the ocean")
    vid_id = provider.submit(step)
    assert vid_id == "vid-abc123"
    call_kwargs = client.videos.create.call_args[1]
    assert "image" not in call_kwargs
    assert "input_reference" not in call_kwargs


def test_submit_skips_non_image_inputs(mock_openai):
    """Non-image inputs (e.g. video) are ignored; no 'image' param is sent."""
    from genblaze_core.models.asset import Asset

    provider, client = mock_openai
    vid_asset = Asset(url="https://example.com/clip.mp4", media_type="video/mp4")
    step = Step(provider="openai-sora", model="sora-2", prompt="extend this", inputs=[vid_asset])
    provider.submit(step)
    call_kwargs = client.videos.create.call_args[1]
    assert "image" not in call_kwargs


def test_submit_rejects_unsafe_chain_input_url(mock_openai):
    """Chain input URLs must be HTTPS or file:// — http:// is rejected."""
    from genblaze_core.models.asset import Asset

    provider, _ = mock_openai
    img = Asset(url="http://evil.com/payload.png", media_type="image/png")
    step = Step(provider="openai-sora", model="sora-2", prompt="animate", inputs=[img])
    with pytest.raises(ProviderError, match="Unsafe chain input URL"):
        provider.submit(step)


# --- Cost tracking ---
#
# As of genblaze-core 0.3.0 the SDK ships zero hardcoded prices for Sora —
# a flat per-video rate would misreport cost by 10x+ since Sora bills per
# (model, size, seconds), and no widely-verified per-second rate table was
# available to source. See the module docstring and the "OpenAI Sora"
# section of docs/reference/pricing-recipes.md for the canonical guidance:
# register a per-second strategy with your own verified rate — a fabricated
# number would turn "unknown cost" into a silently-wrong concrete one, which
# is worse for a budget gate than None.


def test_estimate_cost_none_by_default():
    """No ModelSpec ships pricing, so estimate_cost() returns None until the
    caller registers a strategy — this is the bug reported in #222/#223."""
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test-key")
    assert provider.estimate_cost("sora-2", {"seconds": 8, "size": "1280x720"}) is None


def _per_second(rate: float):
    """Mirrors the ``per_second`` recipe in docs/reference/pricing-recipes.md
    exactly, including its guards, so these tests double as a regression
    check on that doc staying accurate:

    - Reads the native ``seconds`` param, falling back to the canonical
      ``duration`` alias — ``estimate_cost()`` never runs
      ``normalize_params()``, so a caller passing ``duration`` (the alias
      ``generate()`` would otherwise rewrite to ``seconds`` at submit time)
      must still resolve to a cost. Sora assets also carry no probed
      duration metadata (see ``fetch_output()``), so ``ctx.output_duration_s``
      isn't an option either.
    - A non-numeric, negative, or non-finite value yields ``None`` (unknown
      cost) rather than raising or silently reporting a poisoned/negative
      cost.
    """
    from genblaze_core.providers import PricingContext

    def _strategy(ctx: PricingContext) -> float | None:
        seconds = ctx.step.params.get("seconds") or ctx.step.params.get("duration")
        if seconds is None:
            return None
        try:
            seconds_f = float(seconds)
        except (TypeError, ValueError):
            return None
        if seconds_f < 0 or seconds_f != seconds_f or seconds_f in (float("inf"), float("-inf")):
            return None
        return seconds_f * rate

    return _strategy


def test_estimate_cost_with_registered_per_second_pricing():
    """Registering a per-second strategy (the documented recipe) makes
    estimate_cost() return a Decimal that scales with requested duration —
    doubling ``seconds`` doubles the estimate."""
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test-key")
    provider._models = provider.models.fork()
    provider.models.register_pricing("sora-2", _per_second(0.10))

    short_cost = provider.estimate_cost("sora-2", {"seconds": 4})
    long_cost = provider.estimate_cost("sora-2", {"seconds": 8})

    assert isinstance(short_cost, Decimal)
    assert isinstance(long_cost, Decimal)
    assert float(long_cost) == pytest.approx(float(short_cost) * 2)


def test_estimate_cost_falls_back_to_duration_alias():
    """estimate_cost() never runs normalize_params(), so a caller passing
    the canonical ``duration`` alias (rather than native ``seconds``) must
    still get a real cost out of the registered recipe — not a silent
    None. Regression guard for the dual-key read."""
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test-key")
    provider._models = provider.models.fork()
    provider.models.register_pricing("sora-2", _per_second(0.10))

    seconds_cost = provider.estimate_cost("sora-2", {"seconds": 8})
    duration_cost = provider.estimate_cost("sora-2", {"duration": 8})

    assert duration_cost is not None
    assert duration_cost == seconds_cost


@pytest.mark.parametrize(
    "params",
    [
        {"seconds": "not-a-number"},
        {"seconds": None},
        {"seconds": -4},
        {"seconds": float("nan")},
        {"seconds": float("inf")},
        {"seconds": object()},
        {},
    ],
)
def test_estimate_cost_returns_none_on_malformed_seconds(params):
    """The registered strategy's input guards must return None — not
    raise, not silently report a negative/NaN/infinite cost — for every
    malformed shape of the ``seconds``/``duration`` param."""
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        from genblaze_openai import SoraProvider

        provider = SoraProvider(api_key="test-key")
    provider._models = provider.models.fork()
    provider.models.register_pricing("sora-2", _per_second(0.10))

    assert provider.estimate_cost("sora-2", params) is None


# --- Compliance harness ---


class TestSoraCompliance(ProviderComplianceTests):
    """Verify SoraProvider satisfies the genblaze provider contract."""

    # Sora pricing formula (model × resolution × seconds) not yet implemented.
    # Flat-cost attribution was removed to avoid misreporting; re-enable
    # when the per-second formula lands.
    expects_cost = False

    @pytest.fixture(autouse=True)
    def _patch_sdk(self):
        with patch.dict("sys.modules", {"openai": MagicMock()}):
            yield

    def make_provider(self):
        from genblaze_openai import SoraProvider

        mock_content = MagicMock()
        mock_content.write_to_file = MagicMock(
            side_effect=lambda path: open(path, "wb").write(b"video")
        )
        mock_client = MagicMock()
        mock_client.videos.create.return_value = SimpleNamespace(id="vid-abc123")
        mock_client.videos.retrieve.return_value = SimpleNamespace(
            id="vid-abc123", status="completed", model="sora-2"
        )
        mock_client.videos.download_content.return_value = mock_content
        provider = SoraProvider(api_key="test-key")
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="openai-sora", model="sora-2", prompt="test prompt")
