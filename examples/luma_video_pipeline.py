#!/usr/bin/env python3
"""Example: Luma Dream Machine video generation pipeline.

Generates a video using Luma Ray-2 with full provenance.

Models:
    - ray-2: Latest, highest quality
    - ray-flash-2: Faster, lower cost

Usage:
    export LUMAAI_API_KEY=...
    python examples/luma_video_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_luma import LumaProvider

    provider = LumaProvider()

    run, manifest = (
        Pipeline("luma-demo", project_id="examples")
        .step(
            provider,
            model="ray-2",
            prompt=(
                "A slow-motion shot of ocean waves crashing against "
                "volcanic rocks at golden hour, cinematic"
            ),
            modality=Modality.VIDEO,
            aspect_ratio="16:9",
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
