"""Tests for ReplicateProvider with mocked Replicate API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.models.step import Step
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


def test_cost_tracked_from_metrics():
    """Cost is computed from prediction.metrics.predict_time."""
    provider = ReplicateProvider(api_token="test-token")
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
    """Cost stays None when prediction has no metrics."""
    provider = ReplicateProvider(api_token="test-token")
    mock_client = MagicMock()
    pred = FakePrediction()
    # No metrics attribute
    mock_client.predictions.get.return_value = pred
    provider._client = mock_client

    step = _make_step()
    result = provider.fetch_output("pred-abc123", step)
    assert result.cost_usd is None


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


# --- Compliance harness ---


class TestReplicateCompliance(ProviderComplianceTests):
    """Verify ReplicateProvider satisfies the genblaze provider contract."""

    def make_provider(self):
        provider = ReplicateProvider(api_token="test-token")
        mock_client = MagicMock()
        mock_client.predictions.create.return_value = FakePrediction()
        mock_client.predictions.get.return_value = FakePrediction()
        provider._client = mock_client
        return provider

    def make_step(self):
        return Step(provider="replicate", model="test/model", prompt="a cat")
