"""Shared fakes for the LiteLLM connector tests (no network, no keys)."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import litellm
import pytest

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image"
AUDIO_BYTES = b"ID3fake-audio"
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42fake-video"


class FakeLiteLLM:
    """Records every LiteLLM call and returns canned responses."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.image_items = [
            SimpleNamespace(b64_json=base64.b64encode(PNG_BYTES).decode(), url=None)
        ]
        self.statuses = ["queued", "completed"]
        self.video_error = None
        self.raise_on: dict[str, Exception] = {}

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        if name in self.raise_on:
            raise self.raise_on[name]

    def kwargs_of(self, name):
        return [kw for n, kw in self.calls if n == name]

    def image_generation(self, **kwargs):
        self._record("image_generation", kwargs)
        return SimpleNamespace(
            data=self.image_items,
            usage={"input_tokens": 12, "output_tokens": 4160},
            _hidden_params={"response_cost": 0.042},
        )

    def speech(self, **kwargs):
        self._record("speech", kwargs)
        return SimpleNamespace(content=AUDIO_BYTES, _hidden_params={"response_cost": 0.0003})

    def video_generation(self, **kwargs):
        self._record("video_generation", kwargs)
        return SimpleNamespace(id="video_abc123", status="queued")

    def video_status(self, **kwargs):
        self._record("video_status", kwargs)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return SimpleNamespace(
            id=kwargs["video_id"],
            status=status,
            model="sora-2",
            seconds="4",
            size="1280x720",
            error=self.video_error,
            usage={"duration_seconds": 4.0},
            _hidden_params={"response_cost": 0.4},
        )

    def video_content(self, **kwargs):
        self._record("video_content", kwargs)
        return VIDEO_BYTES

    def completion(self, **kwargs):
        self._record("completion", kwargs)
        response = litellm.ModelResponse(
            model="claude-sonnet-4-6",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Waves fold into light."},
                }
            ],
            usage={"prompt_tokens": 21, "completion_tokens": 7, "total_tokens": 28},
        )
        response._hidden_params = {"response_cost": 0.000168}
        return response


@pytest.fixture
def fake_litellm(monkeypatch):
    fake = FakeLiteLLM()
    for name in (
        "image_generation",
        "speech",
        "video_generation",
        "video_status",
        "video_content",
        "completion",
    ):
        monkeypatch.setattr(litellm, name, getattr(fake, name))
    return fake
