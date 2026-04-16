#!/usr/bin/env python3
"""Example: Google Imagen image generation pipeline.

Generates an image using Imagen 3 with full provenance.

Models:
    - imagen-3.0-generate-002: Latest, highest quality
    - imagen-3.0-fast-generate-001: Faster, lower cost

Usage:
    export GEMINI_API_KEY=...
    python examples/imagen_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_google import ImagenProvider

    provider = ImagenProvider(output_dir="output/images")

    run, manifest = (
        Pipeline("imagen-demo", project_id="examples")
        .step(
            provider,
            model="imagen-3.0-generate-002",
            prompt="A photorealistic aerial view of a coral reef teeming with tropical fish",
            modality=Modality.IMAGE,
            aspect_ratio="16:9",
        )
        .run(timeout=120, max_retries=1)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Image:     {run.steps[0].assets[0].url}")


if __name__ == "__main__":
    main()
