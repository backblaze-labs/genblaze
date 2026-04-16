#!/usr/bin/env python3
"""Example: Runway Gen video generation pipeline.

Generates a video using Runway Gen-4 with full provenance.

Models:
    - gen4_turbo: Latest, fast
    - gen3a_turbo: Previous generation

Usage:
    export RUNWAYML_API_SECRET=...
    python examples/runway_video_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_runway import RunwayProvider

    provider = RunwayProvider()

    run, manifest = (
        Pipeline("runway-demo", project_id="examples")
        .step(
            provider,
            model="gen4_turbo",
            prompt=(
                "A timelapse of wildflowers blooming in a meadow, soft morning light, macro detail"
            ),
            modality=Modality.VIDEO,
            duration=10,
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
