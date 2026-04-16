"""Shared test fixtures for genblaze-core."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from genblaze_core.models.asset import Asset
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step
from genblaze_core.providers.base import BaseProvider
from genblaze_core.runnable.config import RunnableConfig
from PIL import Image


@pytest.fixture()
def sample_manifest() -> Manifest:
    """A simple manifest with one step for embed/extract tests."""
    step = Step(provider="test", model="test-model", prompt="hello")
    run = Run(steps=[step])
    return Manifest.from_run(run)


@pytest.fixture()
def tmp_png(tmp_path: Path) -> Path:
    """Create a 1x1 red PNG."""
    p = tmp_path / "test.png"
    img = Image.new("RGBA", (1, 1), (255, 0, 0, 255))
    img.save(p)
    return p


@pytest.fixture()
def tmp_jpeg(tmp_path: Path) -> Path:
    """Create a 10x10 JPEG."""
    p = tmp_path / "test.jpg"
    img = Image.new("RGB", (10, 10), (255, 0, 0))
    img.save(p, "JPEG")
    return p


@pytest.fixture()
def tmp_webp(tmp_path: Path) -> Path:
    """Create a 10x10 WebP."""
    p = tmp_path / "test.webp"
    img = Image.new("RGB", (10, 10), (0, 255, 0))
    img.save(p, "WEBP")
    return p


class MockProvider(BaseProvider):
    """Provider that always succeeds with a single asset."""

    name = "mock"

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        return "pred-123"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        step.assets.append(Asset(url="https://example.com/out.png", media_type="image/png"))
        return step


@pytest.fixture()
def tmp_mp4(tmp_path: Path) -> Path:
    """Create a minimal valid MP4 file (ftyp + mdat boxes)."""
    p = tmp_path / "test.mp4"
    ftyp = b"\x00\x00\x00\x14" + b"ftyp" + b"isom" + b"\x00\x00\x00\x00" + b"isom"
    mdat = b"\x00\x00\x00\x08" + b"mdat"
    p.write_bytes(ftyp + mdat)
    return p


@pytest.fixture()
def tmp_mp3(tmp_path: Path) -> Path:
    """Create a minimal valid MP3 file with a silent frame."""
    p = tmp_path / "test.mp3"
    # Minimal MP3: ID3v2 header + one silent MPEG frame
    # ID3v2 header (10 bytes, no frames, size=0)
    id3_header = b"ID3\x03\x00\x00\x00\x00\x00\x00"
    # Minimal MPEG audio frame header (MPEG1 Layer3, 128kbps, 44100Hz, stereo)
    # 0xFF 0xFB 0x90 0x00 = sync + MPEG1/Layer3/128kbps/44100/stereo
    frame_header = b"\xff\xfb\x90\x00"
    # Frame payload (417 bytes for 128kbps @ 44100Hz minus 4 byte header)
    frame_payload = b"\x00" * 413
    p.write_bytes(id3_header + frame_header + frame_payload)
    return p


@pytest.fixture()
def tmp_wav(tmp_path: Path) -> Path:
    """Create a minimal valid WAV file."""
    import struct

    p = tmp_path / "test.wav"
    # Minimal WAV: RIFF header + fmt chunk + data chunk
    fmt_chunk = (
        b"fmt "
        + struct.pack("<I", 16)  # chunk size
        + struct.pack("<HHIIHH", 1, 1, 44100, 88200, 2, 16)  # PCM, mono, 44100Hz, 16-bit
    )
    audio_data = b"\x00" * 100
    data_chunk = b"data" + struct.pack("<I", len(audio_data)) + audio_data
    riff_size = 4 + len(fmt_chunk) + len(data_chunk)
    p.write_bytes(b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + fmt_chunk + data_chunk)
    return p


@pytest.fixture()
def mock_provider() -> MockProvider:
    return MockProvider()
