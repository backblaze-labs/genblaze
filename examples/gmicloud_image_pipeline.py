#!/usr/bin/env python3
"""Example: GMICloud image generation pipeline.

Generates an image using GMICloud's request queue and captures full provenance
in a manifest.

Models (subset — any image model on GMICloud's queue is supported):
    - seedream-5.0-lite: Fast text-to-image
    - gemini-2.5-flash-image: Google Gemini image generation
    - reve-edit-fast-20251030: Image editing
    - flux-kontext-pro: High-quality text-to-image

Auth options (in priority order):
    - API key: Set GMI_API_KEY env var (recommended)
    - SDK: Set GMI_CLOUD_EMAIL + GMI_CLOUD_PASSWORD env vars

Usage:
    export GMI_API_KEY=...
    python examples/gmicloud_image_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_gmicloud import GMICloudImageProvider

    provider = GMICloudImageProvider()

    run, manifest = (
        Pipeline("gmicloud-image-demo", project_id="examples")
        .step(
            provider,
            model="seedream-5.0-lite",
            prompt=(
                "A photorealistic macro shot of morning dew on a spider web, "
                "soft bokeh background, warm golden hour lighting"
            ),
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
    if run.steps[0].cost_usd is not None:
        print(f"Cost:      ${run.steps[0].cost_usd:.3f}")


if __name__ == "__main__":
    main()
