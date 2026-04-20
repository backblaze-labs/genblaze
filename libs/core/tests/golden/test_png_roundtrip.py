"""Golden test: embed → extract round-trip with 1x1 PNG fixture."""

from pathlib import Path

from genblaze_core.builders import RunBuilder, StepBuilder
from genblaze_core.media.png import PngHandler
from genblaze_core.models.enums import Modality
from genblaze_core.models.manifest import Manifest
from PIL import Image


def test_png_roundtrip(tmp_path: Path):
    """Full end-to-end: build models → embed in PNG → extract → verify."""
    # 1. Create a 1x1 PNG fixture
    png_path = tmp_path / "fixture.png"
    Image.new("RGBA", (1, 1), (0, 128, 255, 255)).save(png_path)

    # 2. Build step → run → manifest
    step = (
        StepBuilder("replicate", "black-forest-labs/flux-schnell")
        .prompt("a golden retriever in a field of sunflowers")
        .modality(Modality.IMAGE)
        .params(width=1024, height=1024, num_inference_steps=4)
        .asset("https://replicate.delivery/output.png", "image/png")
        .build()
    )
    run = RunBuilder("golden-test").tenant("test-org").add_step(step).build()
    manifest = Manifest.from_run(run)

    original_hash = manifest.canonical_hash
    assert original_hash, "Hash should be computed"

    # 3. Embed manifest into PNG
    handler = PngHandler()
    handler.embed(png_path, manifest)

    # 4. Extract manifest from PNG
    extracted = handler.extract(png_path)

    # 5. Verify round-trip fidelity
    assert extracted.canonical_hash == original_hash
    assert extracted.schema_version == "1.5"
    assert extracted.run.run_id == run.run_id
    assert extracted.run.name == "golden-test"
    assert extracted.run.tenant_id == "test-org"
    assert len(extracted.run.steps) == 1

    ext_step = extracted.run.steps[0]
    assert ext_step.provider == "replicate"
    assert ext_step.model == "black-forest-labs/flux-schnell"
    assert ext_step.prompt == "a golden retriever in a field of sunflowers"
    assert ext_step.params["width"] == 1024
    assert len(ext_step.assets) == 1
    assert ext_step.assets[0].url == "https://replicate.delivery/output.png"

    # 6. Verify hash integrity
    assert extracted.verify(), "Extracted manifest should verify"
    assert handler.verify(png_path), "PNG handler verify should pass"
