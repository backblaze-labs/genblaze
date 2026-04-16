#!/usr/bin/env python3
"""Example: Stability AI Stable Audio music generation pipeline.

Generates music and ambient audio with full provenance.

Model: stable-audio-2.5 — up to 3 minutes of audio at 44.1kHz stereo.

Usage:
    export STABILITY_API_KEY=...
    python examples/stability_audio_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_stability_audio import StabilityAudioProvider

    provider = StabilityAudioProvider(output_dir="output/music")

    run, manifest = (
        Pipeline("stability-audio-demo", project_id="examples")
        .step(
            provider,
            model="stable-audio-2.5",
            prompt="Upbeat lo-fi hip hop beat with warm piano chords and vinyl crackle",
            modality=Modality.AUDIO,
            duration=30,
            output_format="mp3",
        )
        .run(timeout=120, max_retries=1)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Audio:     {run.steps[0].assets[0].url}")


if __name__ == "__main__":
    main()
