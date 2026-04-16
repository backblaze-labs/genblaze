#!/usr/bin/env python3
"""Example: ElevenLabs Sound Effects pipeline.

Generates sound effects from text descriptions with full provenance.

Usage:
    export ELEVENLABS_API_KEY=...
    python examples/elevenlabs_sfx_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_elevenlabs import ElevenLabsSFXProvider

    provider = ElevenLabsSFXProvider(output_dir="output/sfx")

    run, manifest = (
        Pipeline("elevenlabs-sfx-demo", project_id="examples")
        .step(
            provider,
            model="eleven_text_to_sound_v2",
            prompt="Thunder crashing during a heavy rainstorm with distant rumbling",
            modality=Modality.AUDIO,
            duration_seconds=10,
        )
        .run(timeout=60, max_retries=1)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Audio:     {run.steps[0].assets[0].url}")


if __name__ == "__main__":
    main()
