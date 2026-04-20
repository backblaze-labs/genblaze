#!/usr/bin/env python3
"""Example: GMICloud video generation pipeline.

Generates a video using GMICloud's request queue and captures full provenance
in a manifest.

Models (subset — any model on GMICloud's queue is supported):
    - Kling-Text2Video-V1.6-Pro: Fast text-to-video
    - Kling-Image2Video-V2.1-Master: High-quality image-to-video
    - Veo3: Google Veo 3 with audio
    - Wan-2.6-T2V: Wan text-to-video

Auth options (in priority order):
    - API key: Set GMI_API_KEY env var (recommended)
    - SDK: Set GMI_CLOUD_EMAIL + GMI_CLOUD_PASSWORD env vars

Usage:
    export GMI_API_KEY=...
    python examples/gmicloud_video_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_gmicloud import GMICloudVideoProvider

    # API key auth (recommended)
    provider = GMICloudVideoProvider()

    # Or SDK email/password auth:
    # provider = GMICloudVideoProvider(email="user@example.com", password="...")

    run, manifest = (
        Pipeline("gmicloud-video-demo", project_id="examples")
        .step(
            provider,
            model="Kling-Text2Video-V1.6-Pro",
            prompt=(
                "A drone shot flying over a misty mountain valley at sunrise, "
                "golden light filtering through clouds, cinematic"
            ),
            modality=Modality.VIDEO,
            duration=10,
            aspect_ratio="16:9",
        )
        .run(timeout=600, max_retries=1)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Video:     {run.steps[0].assets[0].url}")
    if run.steps[0].cost_usd is not None:
        print(f"Cost:      ${run.steps[0].cost_usd:.3f}")


if __name__ == "__main__":
    main()
