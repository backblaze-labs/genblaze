"""Tests for GMICloudVideoProvider (mocked — no real API calls)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.providers.base import SubmitResult
from genblaze_core.testing import ProviderComplianceTests

from .conftest import make_mock_http_client


@pytest.fixture
def provider():
    from genblaze_gmicloud import GMICloudVideoProvider

    p = GMICloudVideoProvider(api_key="test-api-key-123")
    p._http_client = make_mock_http_client(
        outcome_url="https://gmicloud-output.com/video.mp4",
    )
    return p


# --- Submit ---


def test_submit_returns_submit_result(provider):
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="a sunset")
    result = provider.submit(step)
    assert isinstance(result, SubmitResult)
    assert result.prediction_id == "req-abc123"


def test_submit_forwards_params(provider):
    step = Step(
        provider="gmicloud",
        model="kling-text2video-v1.6-pro",
        prompt="test",
        params={"duration": 10, "cfg_scale": 0.5},
    )
    provider.submit(step)
    body = provider._http_client.post.call_args.kwargs.get("json")
    assert body["payload"]["duration"] == 10
    assert body["payload"]["cfg_scale"] == 0.5


def test_submit_forwards_negative_prompt_from_step_field(provider):
    """Pipeline hoists negative_prompt out of params onto the Step field."""
    step = Step(
        provider="gmicloud",
        model="kling-text2video-v1.6-pro",
        prompt="a sunset",
        negative_prompt="blurry, low-res",
    )
    provider.submit(step)
    body = provider._http_client.post.call_args.kwargs.get("json")
    assert body["payload"]["negative_prompt"] == "blurry, low-res"


def test_submit_image_to_video(provider):
    from genblaze_core.models.asset import Asset

    step = Step(
        provider="gmicloud",
        model="kling-image2video-v1.6-pro",
        prompt="animate this",
        inputs=[Asset(url="https://example.com/image.png", media_type="image/png")],
    )
    provider.submit(step)
    body = provider._http_client.post.call_args.kwargs.get("json")
    assert body["payload"]["image"] == "https://example.com/image.png"


# --- Poll ---


def test_poll_returns_true_on_success(provider):
    assert provider.poll("req-abc123") is True


def test_poll_returns_false_on_processing(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"request_id": "req-abc123", "status": "processing"}
    provider._http_client.get.return_value = resp
    assert provider.poll("req-abc123") is False


def test_poll_returns_true_on_failed(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "failed", "error": "Content policy violation"}
    provider._http_client.get.return_value = resp
    assert provider.poll("req-abc123") is True


# --- Fetch output ---


def test_fetch_output_attaches_asset(provider):
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="a sunset")
    result = provider.fetch_output("req-abc123", step)
    assert len(result.assets) == 1
    assert result.assets[0].media_type == "video/mp4"
    assert "gmicloud-output.com" in result.assets[0].url


def test_poll_then_fetch_failed(provider):
    failed_resp = MagicMock()
    failed_resp.status_code = 200
    failed_resp.json.return_value = {"status": "failed", "error": "Content policy violation"}
    provider._http_client.get.return_value = failed_resp
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="bad")
    with pytest.raises(ProviderError, match="Content policy violation"):
        provider.fetch_output("req-abc123", step)


def test_fetch_output_cancelled_raises(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "cancelled"}
    provider._http_client.get.return_value = resp
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="test")
    with pytest.raises(ProviderError, match="cancelled"):
        provider.fetch_output("req-abc123", step)


def test_invoke_full_lifecycle(provider):
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="a sunset")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


# --- Cost ---


def test_cost_none_by_default(provider):
    """SDK no longer ships pricing for GMICloud as of genblaze-core 0.3.0."""
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="a sunset")
    result = provider.fetch_output("req-abc123", step)
    assert result.cost_usd is None


def test_cost_none_unknown_model(provider):
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="unknown-model", prompt="a sunset")
    result = provider.fetch_output("req-abc123", step)
    assert result.cost_usd is None


def test_cost_tracked_with_user_registered_per_unit(provider):
    """User-registered per-unit pricing flows through compute_cost."""
    from genblaze_core.providers import per_unit

    # Fork before mutating so the test doesn't pollute models_default().
    provider._models = provider.models.fork()
    provider.models.register_pricing("kling-text2video-v1.6-pro", per_unit(0.098))
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="a sunset")
    result = provider.fetch_output("req-abc123", step)
    assert result.cost_usd == pytest.approx(0.098)


def test_cost_tracked_with_user_registered_per_second(provider):
    """Per-second strategies still compose — user wires the rate via
    register_pricing(). See docs/reference/pricing-recipes.md for the
    canonical Seedance 2.0 recipe."""
    from genblaze_core.providers import PricingContext, PricingStrategy

    def per_duration(rate: float) -> PricingStrategy:
        def s(ctx: PricingContext) -> float | None:
            dur = ctx.step.params.get("duration")
            return rate * float(dur) if dur is not None else None

        return s

    provider._models = provider.models.fork()
    provider.models.register_pricing("seedance-2-0-260128", per_duration(0.052))
    provider.poll("req-abc123")
    step = Step(
        provider="gmicloud",
        model="seedance-2-0-260128",
        prompt="a sunset",
        params={"duration": 10},
    )
    result = provider.fetch_output("req-abc123", step)
    assert result.cost_usd == pytest.approx(0.052 * 10)


# --- Video metadata ---


def test_video_metadata_no_audio(provider):
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="test")
    result = provider.fetch_output("req-abc123", step)
    assert result.assets[0].video is not None
    assert result.assets[0].video.has_audio is False


def test_veo3_model_has_audio_metadata(provider):
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="veo3", prompt="test")
    result = provider.fetch_output("req-abc123", step)
    asset = result.assets[0]
    assert asset.video.has_audio is True
    assert asset.audio is not None
    assert asset.audio.codec == "aac"
    assert len(asset.tracks) == 2


def test_veo3_legacy_slug_no_longer_alias_resolves(provider):
    """Soft-launch clean break (genblaze-core 0.3.0): the SDK no longer
    registers per-slug PascalCase ``deprecated_aliases``. ``Veo3``
    passes through to the wire as-is — no alias redirect, no
    DeprecationWarning. The audio metadata heuristic in
    ``_HAS_AUDIO_MODELS`` keys off the canonical lowercase id only."""
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="Veo3", prompt="test")
    result = provider.fetch_output("req-abc123", step)
    # Audio metadata not auto-attached for the PascalCase form (the
    # _HAS_AUDIO_MODELS frozenset is canonical-lowercase-only).
    assert result.assets, "fetch_output should still produce an asset"


# --- Provider payload ---


def test_provider_payload_populated(provider):
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="test")
    result = provider.fetch_output("req-abc123", step)
    assert result.provider_payload["gmicloud"]["request_id"] == "req-abc123"
    assert result.provider_payload["gmicloud"]["status"] == "success"


def test_credentials_not_in_provider_payload(provider):
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="test")
    result = provider.fetch_output("req-abc123", step)
    assert "test-api-key-123" not in json.dumps(result.provider_payload)


# --- SSRF ---


def test_asset_url_rejects_non_https(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "success",
        "outcome": {"media_urls": [{"url": "file:///etc/passwd"}]},
    }
    provider._http_client.get.return_value = resp
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="test")
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        provider.fetch_output("req-abc123", step)


def test_asset_url_rejects_http(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "success",
        "outcome": {"media_urls": [{"url": "http://169.254.169.254/latest/meta-data/"}]},
    }
    provider._http_client.get.return_value = resp
    provider.poll("req-abc123")
    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="test")
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        provider.fetch_output("req-abc123", step)


def test_chain_input_rejects_http_url(provider):
    from genblaze_core.models.asset import Asset

    step = Step(
        provider="gmicloud",
        model="kling-image2video-v1.6-pro",
        prompt="animate",
        inputs=[Asset(url="http://evil.com/payload.bin", media_type="image/png")],
    )
    with pytest.raises(ProviderError, match="[Uu]nsafe"):
        provider.submit(step)


# --- Error mapping ---


def test_error_mapping_auth():
    from genblaze_gmicloud._errors import map_gmicloud_error

    assert (
        map_gmicloud_error(Exception("unauthorized: invalid credentials"))
        == ProviderErrorCode.AUTH_FAILURE
    )


def test_error_mapping_forbidden():
    from genblaze_gmicloud._errors import map_gmicloud_error

    assert map_gmicloud_error(Exception("forbidden")) == ProviderErrorCode.AUTH_FAILURE


def test_error_mapping_rate_limit():
    from genblaze_gmicloud._errors import map_gmicloud_error

    result = map_gmicloud_error(Exception("too many"), status_code=429)
    assert result == ProviderErrorCode.RATE_LIMIT


def test_error_mapping_server_error():
    from genblaze_gmicloud._errors import map_gmicloud_error

    assert map_gmicloud_error(Exception("fail"), status_code=502) == ProviderErrorCode.SERVER_ERROR


def test_error_mapping_invalid_input():
    from genblaze_gmicloud._errors import map_gmicloud_error

    assert map_gmicloud_error(Exception("bad"), status_code=400) == ProviderErrorCode.INVALID_INPUT


def test_error_mapping_content_policy_wins_over_400():
    """A 400 that carries a safety / policy message is CONTENT_POLICY, not INVALID_INPUT."""
    from genblaze_gmicloud._errors import map_gmicloud_error

    assert (
        map_gmicloud_error(Exception("prompt rejected by safety filter"), status_code=400)
        == ProviderErrorCode.CONTENT_POLICY
    )
    assert (
        map_gmicloud_error(Exception("content policy violation"), status_code=400)
        == ProviderErrorCode.CONTENT_POLICY
    )


def test_submit_unwraps_json_error_body(provider):
    """A JSON ``{"error": "..."}`` body is surfaced without double-wrapping."""
    err_resp = MagicMock()
    err_resp.status_code = 500
    err_resp.text = '{"error":"Backend error (400). Please try again."}'
    err_resp.json.return_value = {"error": "Backend error (400). Please try again."}
    provider._http_client.post.return_value = err_resp

    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="x")
    with pytest.raises(ProviderError) as exc_info:
        provider.submit(step)
    msg = str(exc_info.value)
    assert "Backend error (400). Please try again." in msg
    # Must not be double-encoded: the raw JSON body should not appear.
    assert '{"error"' not in msg


def test_submit_passes_pascalcase_slug_through_unchanged(provider):
    """Soft-launch clean break: the SDK no longer canonicalizes
    PascalCase ids via ``deprecated_aliases``. The slug goes to the
    wire verbatim. Users who need the lowercase form should pass it
    directly; users for whom GMICloud actually accepts PascalCase get
    that behavior natively now."""
    step = Step(provider="gmicloud", model="Kling-Text2Video-V1.6-Pro", prompt="x")
    provider.submit(step)
    body = provider._http_client.post.call_args.kwargs.get("json")
    assert body["model"] == "Kling-Text2Video-V1.6-Pro"


def test_submit_passes_non_json_body_through(provider):
    """Plain-text error bodies pass through unchanged."""
    err_resp = MagicMock()
    err_resp.status_code = 500
    err_resp.text = "upstream gateway timeout"
    provider._http_client.post.return_value = err_resp

    step = Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="x")
    with pytest.raises(ProviderError, match="upstream gateway timeout"):
        provider.submit(step)


def test_error_mapping_status_code_priority():
    from genblaze_gmicloud._errors import map_gmicloud_error

    result = map_gmicloud_error(Exception("unauthorized"), status_code=429)
    assert result == ProviderErrorCode.RATE_LIMIT


# --- Parameter pipeline (spec-driven via prepare_payload) ---


def test_duration_coerced_to_int(provider):
    step = Step(
        provider="gmicloud",
        model="kling-text2video-v1.6-pro",
        prompt="t",
        params={"duration": "10"},
    )
    assert provider.prepare_payload(step)["duration"] == 10


def test_guidance_scale_aliased_to_cfg_scale(provider):
    step = Step(
        provider="gmicloud",
        model="kling-text2video-v1.6-pro",
        prompt="t",
        params={"guidance_scale": 0.7},
    )
    result = provider.prepare_payload(step)
    assert result["cfg_scale"] == 0.7
    assert "guidance_scale" not in result


def test_prepare_payload_is_idempotent_for_normalized_params(provider):
    step = Step(
        provider="gmicloud",
        model="kling-text2video-v1.6-pro",
        prompt="t",
        params={"duration": 10, "cfg_scale": 0.7},
    )
    once = provider.prepare_payload(step)
    # Re-running against already-normalized params produces the same dict.
    step2 = Step(provider="gmicloud", model=step.model, prompt="t", params=once)
    assert provider.prepare_payload(step2) == once


# --- Passthrough ---


def test_unknown_model_passthrough(provider):
    step = Step(provider="gmicloud", model="SomeNewModel-v99", prompt="test")
    result = provider.submit(step)
    assert isinstance(result, SubmitResult)


# --- Compliance ---


class TestGMICloudCompliance(ProviderComplianceTests):
    # SDK no longer ships pricing for GMICloud (genblaze-core 0.3.0).
    # Users wire pricing via register_pricing(); see docs/reference/pricing-recipes.md.
    expects_cost = False

    def make_provider(self):
        from genblaze_gmicloud import GMICloudVideoProvider

        p = GMICloudVideoProvider(api_key="test-compliance-key")
        p._http_client = make_mock_http_client(
            outcome_url="https://gmicloud-output.com/video.mp4",
        )
        p.poll_interval = 0.0

        # Suppress initial_delay sleep in _attempt_once
        original_submit = p.submit

        def fast_submit(step, config=None):
            r = original_submit(step, config)
            r.estimated_seconds = None
            return r

        p.submit = fast_submit
        return p

    def make_step(self):
        return Step(provider="gmicloud", model="kling-text2video-v1.6-pro", prompt="test prompt")
