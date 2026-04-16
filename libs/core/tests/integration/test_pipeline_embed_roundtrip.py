"""Integration test: pipeline -> embed -> extract -> verify roundtrip."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from genblaze_core.media.embedder import SmartEmbedder
from genblaze_core.media.png import PngHandler
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import RunStatus, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.pipeline import Pipeline
from genblaze_core.providers.base import BaseProvider
from genblaze_core.runnable.config import RunnableConfig
from PIL import Image


class _FakeProvider(BaseProvider):
    """Provider that returns a single PNG asset."""

    name = "fake"

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        return "pred-roundtrip"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        step.assets.append(Asset(url="https://example.com/result.png", media_type="image/png"))
        return step


def test_pipeline_embed_extract_verify(tmp_path: Path) -> None:
    """Full lifecycle: build pipeline, embed manifest in PNG, extract and verify."""
    # 1. Run pipeline with mock provider
    provider = _FakeProvider()
    result = (
        Pipeline("roundtrip-test").step(provider, model="test/model", prompt="a red square").run()
    )

    assert result.run.status == RunStatus.COMPLETED
    assert result.run.steps[0].status == StepStatus.SUCCEEDED
    assert result.manifest.verify()

    # 2. Embed manifest into a PNG
    png_path = tmp_path / "output.png"
    img = Image.new("RGB", (1, 1), color="red")
    img.save(png_path)

    manifest = result.manifest
    embedder = SmartEmbedder()
    embedder.embed(png_path, manifest)

    # 3. Extract manifest from the PNG
    handler = PngHandler()
    extracted = handler.extract(png_path)

    # 4. Verify extracted manifest matches original
    assert extracted.canonical_hash == manifest.canonical_hash
    assert extracted.run.run_id == manifest.run.run_id
    assert len(extracted.run.steps) == len(manifest.run.steps)
    assert extracted.run.steps[0].prompt == "a red square"

    # 5. Verify hash integrity
    assert extracted.verify()
    assert handler.verify(png_path)
