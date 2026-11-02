"""Tests for LiteLLMVideoProvider (mocked; no billable requests)."""

from __future__ import annotations

import hashlib

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests
from genblaze_litellm import LiteLLMVideoProvider

from .conftest import VIDEO_BYTES


def _step(**params):
    return Step(
        provider="litellm-video",
        model="openai/sora-2",
        modality=Modality.VIDEO,
        prompt="waves rolling onto a beach",
        params=params,
    )


def test_submit_poll_fetch_lifecycle(fake_litellm, tmp_path):
    provider = LiteLLMVideoProvider(output_dir=tmp_path, poll_interval=0)
    result = provider.invoke(_step(duration=4, size="1280x720"))

    assert result.status == StepStatus.SUCCEEDED
    submit = fake_litellm.kwargs_of("video_generation")[0]
    assert submit["model"] == "openai/sora-2"
    assert submit["seconds"] == "4"  # duration alias, stringified for the Videos API
    assert submit["size"] == "1280x720"
    assert submit["drop_params"] is True

    asset = result.assets[0]
    assert asset.media_type == "video/mp4"
    assert asset.sha256 == hashlib.sha256(VIDEO_BYTES).hexdigest()
    assert asset.duration == 4.0
    assert result.provider_payload["litellm"]["video_id"] == "video_abc123"
    assert result.cost_usd == pytest.approx(0.4)
    assert fake_litellm.kwargs_of("video_content")[0]["video_id"] == "video_abc123"


def test_poll_result_is_reused_by_fetch_output(fake_litellm, tmp_path):
    fake_litellm.statuses = ["completed"]
    provider = LiteLLMVideoProvider(output_dir=tmp_path)
    assert provider.poll("video_abc123") is True
    provider.fetch_output("video_abc123", _step())
    assert len(fake_litellm.kwargs_of("video_status")) == 1


def test_failed_job_raises_model_error(fake_litellm):
    fake_litellm.statuses = ["failed"]
    fake_litellm.video_error = {"message": "moderation blocked"}
    provider = LiteLLMVideoProvider()
    with pytest.raises(ProviderError, match="moderation blocked") as exc:
        provider.fetch_output("video_abc123", _step())
    assert exc.value.error_code == ProviderErrorCode.MODEL_ERROR
    assert not fake_litellm.kwargs_of("video_content")


def test_submit_is_single_attempt(fake_litellm):
    import litellm

    fake_litellm.raise_on["video_generation"] = litellm.Timeout(
        message="ambiguous timeout", model="sora-2", llm_provider="openai"
    )
    result = LiteLLMVideoProvider(poll_interval=0).invoke(_step())
    assert result.status == StepStatus.FAILED
    assert result.error_code == ProviderErrorCode.TIMEOUT
    assert len(fake_litellm.kwargs_of("video_generation")) == 1


class TestLiteLLMVideoCompliance(ProviderComplianceTests):
    @pytest.fixture(autouse=True)
    def _fake(self, fake_litellm):
        self.fake = fake_litellm
        fake_litellm.statuses = ["completed"]

    def make_provider(self):
        return LiteLLMVideoProvider(poll_interval=0)

    def make_step(self):
        return _step()
