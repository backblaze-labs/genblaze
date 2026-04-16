#!/usr/bin/env python3
"""Example: Chain an image generator into a video generator (DALL-E → Sora).

Demonstrates chain=True mode where each step's output assets become
the next step's inputs — DALL-E generates a still image, then Sora
animates it into a video.

Usage:
    export OPENAI_API_KEY=sk-...
    python examples/chain_image_to_video.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_openai import DalleProvider, SoraProvider

    result = (
        Pipeline("image-to-video", project_id="examples", chain=True)
        .step(
            DalleProvider(),
            model="dall-e-3",
            prompt="A serene mountain lake at dawn with mist rising",
            modality=Modality.IMAGE,
            size="1024x1024",
        )
        .step(
            SoraProvider(),
            model="sora-2",
            prompt="Gentle wind ripples the lake surface, mist slowly drifts",
            modality=Modality.VIDEO,
        )
        .run(timeout=300, max_retries=1)
    )

    print(f"Run ID:    {result.run.run_id}")
    print(f"Status:    {result.run.status}")
    print(f"Hash:      {result.manifest.canonical_hash}")
    print(f"Verified:  {result.manifest.verify()}")

    for i, step in enumerate(result.run.steps):
        print(f"\nStep {i}: {step.provider}/{step.model}")
        print(f"  Status:  {step.status}")
        if step.assets:
            print(f"  Output:  {step.assets[0].url}")
        if step.inputs:
            print(f"  Inputs:  {len(step.inputs)} asset(s)")


if __name__ == "__main__":
    main()
