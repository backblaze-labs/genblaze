"""Tests for NvidiaAudioProvider."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.step import Step
from genblaze_core.testing import ProviderComplianceTests

from .conftest import make_mock_http_client

_MP3_B64 = base64.b64encode(b"fake-mp3-bytes-for-test").decode()
_WAV_B64 = base64.b64encode(b"fake-wav-bytes-for-test").decode()


@pytest.fixture
def provider(tmp_path):
    from genblaze_nvidia import NvidiaAudioProvider

    p = NvidiaAudioProvider(api_key="nvapi-test", output_dir=tmp_path)
    p._client._http_client = make_mock_http_client(
        submit_body={"artifacts": [{"base64": _MP3_B64, "mime_type": "audio/mpeg"}]},
    )
    return p


def test_generate_writes_audio_file(provider, tmp_path):
    step = Step(provider="nvidia-audio", model="nvidia/riva-tts", prompt="hello world")
    result = provider.generate(step)
    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.media_type == "audio/mpeg"
    assert asset.url.startswith("file://")
    path = Path(asset.url.removeprefix("file://"))
    assert path.parent == tmp_path


def test_generate_sets_mono_for_tts(provider):
    step = Step(provider="nvidia-audio", model="nvidia/riva-tts", prompt="hello")
    result = provider.generate(step)
    assert result.assets[0].audio.channels == 1


def test_generate_sets_stereo_for_music(provider):
    """Fugatto (is_music=True in the registry) → stereo metadata."""
    step = Step(provider="nvidia-audio", model="nvidia/fugatto", prompt="upbeat synthwave")
    result = provider.generate(step)
    assert result.assets[0].audio.channels == 2


def test_wav_mime_maps_to_pcm_codec(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"artifacts": [{"base64": _WAV_B64, "mime_type": "audio/wav"}]}
    resp.text = "{}"
    resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.post.return_value = resp

    step = Step(provider="nvidia-audio", model="nvidia/riva-tts", prompt="hi")
    result = provider.generate(step)
    assert result.assets[0].audio.codec == "pcm"


def test_invoke_full_lifecycle(provider):
    step = Step(provider="nvidia-audio", model="nvidia/riva-tts", prompt="hello")
    result = provider.invoke(step)
    assert result.status == StepStatus.SUCCEEDED
    assert len(result.assets) == 1


def test_unknown_model_passthrough(provider):
    step = Step(
        provider="nvidia-audio",
        model="some-vendor/unreleased-audio-v99",
        prompt="test",
    )
    result = provider.generate(step)
    assert len(result.assets) == 1


def test_generate_hosted_url(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"audio_url": "https://nvcf.example/song.mp3"}
    resp.text = "{}"
    resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.post.return_value = resp

    step = Step(provider="nvidia-audio", model="nvidia/fugatto", prompt="song")
    result = provider.generate(step)
    assert result.assets[0].url == "https://nvcf.example/song.mp3"


def test_generate_no_output_raises(provider):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {}
    resp.text = "{}"
    resp.headers = {"Content-Type": "application/json"}
    provider._client._http_client.post.return_value = resp

    step = Step(provider="nvidia-audio", model="nvidia/riva-tts", prompt="hi")
    with pytest.raises(ProviderError, match="no asset URL or base64"):
        provider.generate(step)


class TestNvidiaAudioCompliance(ProviderComplianceTests):
    # NIM free tier is RPM-gated — no per-generation billing. Pricing opt-out.
    expects_cost = False

    def make_provider(self):
        import tempfile

        from genblaze_nvidia import NvidiaAudioProvider

        p = NvidiaAudioProvider(api_key="nvapi-compliance", output_dir=Path(tempfile.mkdtemp()))
        p._client._http_client = make_mock_http_client(
            submit_body={"artifacts": [{"base64": _MP3_B64, "mime_type": "audio/mpeg"}]},
        )
        return p

    def make_step(self):
        return Step(provider="nvidia-audio", model="nvidia/riva-tts", prompt="test")
