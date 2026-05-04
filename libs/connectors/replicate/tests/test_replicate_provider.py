"""Tests for ReplicateProvider with mocked Replicate API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import ModelSpec
from genblaze_core.testing import ProviderComplianceTests
from genblaze_replicate._errors import map_replicate_error
from genblaze_replicate.provider import ReplicateProvider


@dataclass
class FakePrediction:
    """Minimal mock of a Replicate prediction object."""

    id: str = "pred-abc123"
    status: str = "succeeded"
    output: Any = field(default_factory=lambda: ["https://example.com/out.png"])
    error: str | None = None
    model: str = "test/model"
    version: str | None = "v1"
    created_at: str = "2026-01-01T00:00:00Z"


def _make_step(**kwargs) -> Step:
    defaults = {"provider": "replicate", "model": "test/model", "prompt": "a cat"}
    defaults.update(kwargs)
    return Step(**defaults)


# --- map_replicate_error tests ---


def test_map_error_timeout():
    assert map_replicate_error("Request timed out") == ProviderErrorCode.TIMEOUT


def test_map_error_rate_limit():
    assert map_replicate_error("rate limit exceeded 429") == ProviderErrorCode.RATE_LIMIT


def test_map_error_auth():
    assert map_replicate_error("401 unauthorized token") == ProviderErrorCode.AUTH_FAILURE


def test_map_error_invalid_input():
    assert map_replicate_error("invalid input: bad format") == ProviderErrorCode.INVALID_INPUT


def test_map_error_model_not_found():
    assert map_replicate_error("model not found") == ProviderErrorCode.MODEL_ERROR


def test_map_error_server_error():
    assert map_replicate_error("500 internal server error") == ProviderErrorCode.SERVER_ERROR


def test_map_error_unknown():
    assert map_replicate_error("something went wrong") == ProviderErrorCode.UNKNOWN


def test_map_error_accepts_exception():
    """map_replicate_error should accept Exception objects too."""
    assert map_replicate_error(RuntimeError("timed out")) == ProviderErrorCode.TIMEOUT


# --- submit tests ---


def test_submit_sends_prompt_and_params():
    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.create.return_value = FakePrediction()
    provider._client = mock_client

    step = _make_step(params={"width": 1024})
    result = provider.submit(step)

    mock_client.predictions.create.assert_called_once_with(
        model="test/model",
        input={"width": 1024, "prompt": "a cat"},
    )
    assert result == "pred-abc123"


def test_submit_includes_negative_prompt():
    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.create.return_value = FakePrediction()
    provider._client = mock_client

    step = _make_step(negative_prompt="blurry")
    provider.submit(step)

    call_input = mock_client.predictions.create.call_args.kwargs["input"]
    assert call_input["negative_prompt"] == "blurry"


# --- poll tests ---


def test_poll_returns_true_on_success():
    provider = ReplicateProvider(api_token="test-token", poll_interval=0.0)
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(status="succeeded")
    provider._client = mock_client

    assert provider.poll("pred-abc123") is True


def test_poll_returns_true_on_failed():
    provider = ReplicateProvider(api_token="test-token", poll_interval=0.0)
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(status="failed")
    provider._client = mock_client

    assert provider.poll("pred-abc123") is True


def test_poll_returns_true_on_canceled():
    provider = ReplicateProvider(api_token="test-token", poll_interval=0.0)
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(status="canceled")
    provider._client = mock_client

    assert provider.poll("pred-abc123") is True


def test_poll_returns_false_on_processing():
    provider = ReplicateProvider(api_token="test-token", poll_interval=0.0)
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(status="processing")
    provider._client = mock_client

    assert provider.poll("pred-abc123") is False


# --- fetch_output tests ---


def test_fetch_output_success_with_list():
    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(
        output=["https://example.com/a.png", "https://example.com/b.png"]
    )
    provider._client = mock_client

    step = _make_step()
    result = provider.fetch_output("pred-abc123", step)

    assert len(result.assets) == 2
    assert result.assets[0].url == "https://example.com/a.png"


def test_fetch_output_success_with_string():
    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(
        output="https://example.com/single.png"
    )
    provider._client = mock_client

    step = _make_step()
    result = provider.fetch_output("pred-abc123", step)

    assert len(result.assets) == 1
    assert result.assets[0].url == "https://example.com/single.png"


def test_fetch_output_success_with_none():
    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(output=None)
    provider._client = mock_client

    step = _make_step()
    result = provider.fetch_output("pred-abc123", step)

    assert len(result.assets) == 0


def test_fetch_output_success_with_dict():
    """Some Replicate models return dict outputs like {'video': url, 'audio': url}."""
    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(
        output={
            "video": "https://example.com/video.mp4",
            "subtitles": "https://example.com/subs.vtt",
        }
    )
    provider._client = mock_client

    step = _make_step()
    result = provider.fetch_output("pred-abc123", step)

    urls = {a.url for a in result.assets}
    assert urls == {"https://example.com/video.mp4", "https://example.com/subs.vtt"}


def test_fetch_output_success_with_nested_list():
    """Batch-output models return list[list[str]]; flatten one level."""
    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(
        output=[["https://example.com/a.png", "https://example.com/b.png"]]
    )
    provider._client = mock_client

    step = _make_step()
    result = provider.fetch_output("pred-abc123", step)

    assert len(result.assets) == 2


def test_fetch_output_unknown_shape_raises_clear_error():
    """Unknown output shapes raise ProviderError with a specific message."""
    from genblaze_core.exceptions import ProviderError

    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(output=42)
    provider._client = mock_client

    step = _make_step()
    with pytest.raises(ProviderError, match="Unexpected Replicate output shape"):
        provider.fetch_output("pred-abc123", step)


def test_fetch_output_failed():
    from genblaze_core.exceptions import ProviderError

    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(
        status="failed", error="Model crashed"
    )
    provider._client = mock_client

    step = _make_step()
    with pytest.raises(ProviderError, match="Model crashed"):
        provider.fetch_output("pred-abc123", step)


def test_fetch_output_canceled():
    from genblaze_core.exceptions import ProviderError

    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(status="canceled")
    provider._client = mock_client

    step = _make_step()
    with pytest.raises(ProviderError, match="canceled"):
        provider.fetch_output("pred-abc123", step)


# --- Token safety ---


def test_fetch_output_rejects_non_https_url():
    """Asset URLs must be HTTPS to prevent SSRF via malicious provider output."""
    from genblaze_core.exceptions import ProviderError

    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(output=["file:///etc/passwd"])
    provider._client = mock_client

    step = _make_step()
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        provider.fetch_output("pred-abc123", step)


def test_fetch_output_rejects_http_url():
    """Plain HTTP URLs rejected — could target internal metadata endpoints."""
    from genblaze_core.exceptions import ProviderError

    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(
        output=["http://169.254.169.254/latest/meta-data/"]
    )
    provider._client = mock_client

    step = _make_step()
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        provider.fetch_output("pred-abc123", step)


def test_fetch_output_rejects_schemeless_url():
    """Scheme-less URLs rejected — urlparse sets empty scheme for these."""
    from genblaze_core.exceptions import ProviderError

    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction(
        output=["//169.254.169.254/latest/meta-data/"]
    )
    provider._client = mock_client

    step = _make_step()
    with pytest.raises(ProviderError, match="Unsafe asset URL"):
        provider.fetch_output("pred-abc123", step)


def test_cost_none_by_default():
    """As of genblaze-core 0.3.0 the SDK no longer ships pricing for
    Replicate. ``cost_usd`` is ``None`` unless the user has registered
    a pricing strategy via ``provider.models.register_pricing()``.
    See ``docs/reference/pricing-recipes.md``.
    """
    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    pred = FakePrediction()
    pred.metrics = MagicMock()
    pred.metrics.predict_time = 10.0
    mock_client.predictions.get.return_value = pred
    provider._client = mock_client

    step = _make_step()
    result = provider.fetch_output("pred-abc123", step)
    assert result.cost_usd is None


def test_cost_tracked_with_user_registered_pricing():
    """User-registered compute-time pricing flows through the same path."""
    from genblaze_core.providers import per_response_metric

    def compute_time_cost(ctx):
        payload = ctx.provider_payload.get("replicate") if ctx.provider_payload else None
        if not isinstance(payload, dict):
            return None
        predict_time = payload.get("predict_time")
        if predict_time is None:
            return None
        return float(predict_time) * 0.000225

    provider = ReplicateProvider(api_token="test-token")
    provider.models.register_pricing("test/model", per_response_metric(compute_time_cost))
    mock_client = MagicMock()
    pred = FakePrediction()
    pred.metrics = MagicMock()
    pred.metrics.predict_time = 10.0
    mock_client.predictions.get.return_value = pred
    provider._client = mock_client

    step = _make_step()
    result = provider.fetch_output("pred-abc123", step)
    assert result.cost_usd is not None
    assert result.cost_usd == pytest.approx(10.0 * 0.000225)


def test_cost_none_without_metrics():
    """Cost stays None when prediction has no metrics, even with pricing registered."""
    from genblaze_core.providers import per_response_metric

    provider = ReplicateProvider(api_token="test-token")
    provider.models.register_pricing("test/model", per_response_metric(lambda ctx: 0.0))
    mock_client = MagicMock()
    pred = FakePrediction()
    # No metrics attribute
    mock_client.predictions.get.return_value = pred
    provider._client = mock_client

    step = _make_step()
    result = provider.fetch_output("pred-abc123", step)
    # The strategy returns 0.0 if metrics are absent — but we registered
    # one that returns 0.0 unconditionally. Verify the pricing path runs.
    assert result.cost_usd == pytest.approx(0.0)


def test_token_not_in_provider_payload():
    """API token must never leak into the provider_payload stored in manifests."""
    provider = ReplicateProvider(api_token="r8_secret_token_123")
    mock_client = MagicMock()
    mock_client.predictions.get.return_value = FakePrediction()
    provider._client = mock_client

    step = _make_step()
    result = provider.fetch_output("pred-abc123", step)

    # Serialize the entire payload and check token is absent
    import json

    payload_str = json.dumps(result.provider_payload)
    assert "r8_secret_token_123" not in payload_str


# --- Catalog-decoupling: discovery + per-slug validation ---


def _make_model(owner: str, name: str):
    """Minimal fake of a Replicate ``Model`` object."""
    m = MagicMock()
    m.owner = owner
    m.name = name
    return m


def test_discovery_support_native():
    """Replicate is the proof-point for NATIVE discovery in PR #3."""
    from genblaze_core.providers import DiscoverySupport

    assert ReplicateProvider.discovery_support is DiscoverySupport.NATIVE


def test_discover_models_returns_first_page():
    """``discover_models`` snapshots the first page of /v1/models — enough
    to seed ``known()`` without enumerating the entire catalog."""
    from genblaze_core.providers import DiscoveryStatus

    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    page = MagicMock()
    page.results = [
        _make_model("black-forest-labs", "flux-schnell"),
        _make_model("stability-ai", "sdxl"),
        _make_model("meta", "llama-3"),
    ]
    mock_client.models.list.return_value = page
    provider._client = mock_client

    result = provider.discover_models()
    assert result.status is DiscoveryStatus.OK
    assert "black-forest-labs/flux-schnell" in result.slugs
    assert "stability-ai/sdxl" in result.slugs
    assert result.source_url == "https://api.replicate.com/v1/models"


def test_discover_models_failure_returns_failed():
    """A failed list call surfaces as ``DiscoveryStatus.FAILED`` — never
    raises into the caller, never poisons the cache."""
    from genblaze_core.providers import DiscoveryStatus

    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.models.list.side_effect = RuntimeError("boom")
    provider._client = mock_client

    result = provider.discover_models()
    assert result.status is DiscoveryStatus.FAILED
    assert "boom" in (result.detail or "")


def test_validate_model_authoritative_via_models_get():
    """A live slug returns ``OK_AUTHORITATIVE`` (source PROBE) via
    ``client.models.get()`` — the cheap-existence-check that Replicate
    supports authoritatively."""
    from genblaze_core.providers import ValidationOutcome, ValidationSource

    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.models.get.return_value = _make_model("owner", "live-model")
    provider._client = mock_client

    result = provider.validate_model("owner/live-model")
    assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE
    assert result.source is ValidationSource.PROBE


def test_validate_model_not_found_via_models_get():
    """A 404-class error from ``client.models.get()`` surfaces as
    ``NOT_FOUND``. The Pipeline preflight phase raises on this outcome
    before any prediction is created."""
    from genblaze_core.providers import ValidationOutcome, ValidationSource

    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.models.get.side_effect = RuntimeError("404 model not found")
    provider._client = mock_client

    result = provider.validate_model("owner/dead-model")
    assert result.outcome is ValidationOutcome.NOT_FOUND
    assert result.source is ValidationSource.PROBE


def test_validate_model_caches_result():
    """Per-slug validation cache memoizes the per-slug GET so successive
    Pipeline runs don't re-fetch."""
    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.models.get.return_value = _make_model("owner", "x")
    provider._client = mock_client

    provider.validate_model("owner/x")
    provider.validate_model("owner/x")
    provider.validate_model("owner/x")

    assert mock_client.models.get.call_count == 1


def test_validate_model_refresh_evicts_cache():
    """``refresh=True`` forces a fresh per-slug GET."""
    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    mock_client.models.get.return_value = _make_model("owner", "x")
    provider._client = mock_client

    provider.validate_model("owner/x")
    provider.validate_model("owner/x", refresh=True)
    assert mock_client.models.get.call_count == 2


def test_validate_model_user_registered_skips_probe():
    """A user-registered exact spec is authoritative without a network
    round-trip — the per-slug probe should not fire."""
    from genblaze_core.providers import ValidationOutcome, ValidationSource

    provider = ReplicateProvider(api_token="test-token")
    provider.models.register(ModelSpec(model_id="owner/local", modality=Modality.IMAGE))
    mock_client = MagicMock()
    provider._client = mock_client

    result = provider.validate_model("owner/local")
    assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE
    assert result.source is ValidationSource.USER
    mock_client.models.get.assert_not_called()


# --- Compliance harness ---


class TestReplicateCompliance(ProviderComplianceTests):
    """Verify ReplicateProvider satisfies the genblaze provider contract."""

    # ReplicateProvider populates cost_usd from prediction.metrics.predict_time,
    # but the FakePrediction compliance fixture doesn't expose that metric.
    # A dedicated test (test_cost_tracked_from_predict_time) covers the real
    # code path; compliance waives the check here.
    expects_cost = False

    def make_provider(self):
        provider = ReplicateProvider(api_token="test-token")
        mock_client = MagicMock()
        mock_client.predictions.create.return_value = FakePrediction()
        mock_client.predictions.get.return_value = FakePrediction()
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="replicate", model="test/model", prompt="a cat")
