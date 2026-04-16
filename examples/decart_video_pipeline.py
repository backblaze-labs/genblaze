#!/usr/bin/env python3
"""Example: Decart Lucy video generation pipeline.

Generates a video using Decart's Lucy model with full provenance.

Video models:
    - lucy-pro-t2v: Text-to-video (highest quality)
    - lucy-pro-i2v: Image-to-video
    - lucy-dev-i2v: Image-to-video (faster, dev tier)

Image models:
    - lucy-pro-t2i: Text-to-image
    - lucy-pro-i2i: Image-to-image editing

Usage:
    export DECART_API_KEY=...
    python examples/decart_video_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_decart import DecartProvider

    provider = DecartProvider(output_dir="output/video")

    run, manifest = (
        Pipeline("decart-demo", project_id="examples")
        .step(
            provider,
            model="lucy-pro-t2v",
            prompt="A serene ocean with dolphins jumping at sunset, cinematic lighting",
            modality=Modality.VIDEO,
            resolution="720p",
        )
        .run(timeout=300, max_retries=1)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Video:     {run.steps[0].assets[0].url}")


if __name__ == "__main__":
    main()
