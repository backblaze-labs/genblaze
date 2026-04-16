#!/usr/bin/env python3
"""Example: OpenAI Sora video generation pipeline.

Generates a video using Sora and captures full provenance in a manifest.

Models:
    - sora-2: Fast, good for iteration (default)
    - sora-2-pro: Higher quality, slower

Usage:
    export OPENAI_API_KEY=sk-...
    python examples/sora_video_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_openai import SoraProvider

    provider = SoraProvider()

    # Generate a 4-second video at 1280x720 (landscape)
    run, manifest = (
        Pipeline("sora-demo", project_id="examples")
        .step(
            provider,
            model="sora-2",
            prompt=(
                "A cinematic drone shot gliding over a misty mountain valley "
                "at sunrise, golden light breaking through clouds"
            ),
            modality=Modality.VIDEO,
            seconds=4,
            size="1280x720",
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
