#!/usr/bin/env python3
"""Example: Google Imagen image generation pipeline.

Generates an image using Imagen 4 with full provenance.

Models:
    - imagen-4.0-generate-001: Latest, highest quality
    - imagen-4.0-fast-generate-001: Faster, lower cost

Note: imagen-4.0-* is catalog-listed but entitlement-gated for freshly
created Gemini API keys — preflight passes, but the actual generate call
can still 404. If that happens, use GeminiImageProvider
(model="gemini-2.5-flash-image") instead; it needs no such entitlement.

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
            model="imagen-4.0-generate-001",
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
