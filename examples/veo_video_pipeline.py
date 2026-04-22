#!/usr/bin/env python3
"""Example: Google Veo video generation pipeline.

Generates a video using Google Veo and captures full provenance in a manifest.

Models:
    - veo-2.0-generate-001: Stable, cost-effective
    - veo-3.0-generate-001: GA, generates video with synchronized audio
    - veo-3.0-fast-generate-001: GA, faster variant

Auth options:
    - Gemini API: Set GEMINI_API_KEY env var
    - Vertex AI: Pass project="your-gcp-project"

Usage:
    export GEMINI_API_KEY=...
    python examples/veo_video_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_google import VeoProvider

    # Gemini API auth (simplest)
    provider = VeoProvider()

    # Or Vertex AI auth:
    # provider = VeoProvider(project="my-gcp-project", location="us-central1")

    # Generate an 8-second video at 720p, 16:9
    run, manifest = (
        Pipeline("veo-demo", project_id="examples")
        .step(
            provider,
            model="veo-3.0-generate-001",
            prompt=(
                "A time-lapse of a coral reef coming to life, with colorful fish "
                "swimming through vibrant coral formations, natural ocean lighting"
            ),
            modality=Modality.VIDEO,
            aspect_ratio="16:9",
            duration_seconds=8,
            resolution="720p",
            enhance_prompt=True,
        )
        .run(timeout=600, max_retries=1)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Video:     {run.steps[0].assets[0].url}")


if __name__ == "__main__":
    main()
