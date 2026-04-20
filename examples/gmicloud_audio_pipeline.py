#!/usr/bin/env python3
"""Example: GMICloud audio/TTS generation pipeline.

Generates audio using GMICloud's request queue and captures full provenance
in a manifest.

Models (subset — any audio model on GMICloud's queue is supported):
    - ElevenLabs-TTS-v3: High-quality text-to-speech
    - MiniMax-TTS-Speech-2.6-Turbo: Fast TTS
    - MiniMax-Music-2.5: Music generation

Auth options (in priority order):
    - API key: Set GMI_API_KEY env var (recommended)
    - SDK: Set GMI_CLOUD_EMAIL + GMI_CLOUD_PASSWORD env vars

Usage:
    export GMI_API_KEY=...
    python examples/gmicloud_audio_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_gmicloud import GMICloudAudioProvider

    provider = GMICloudAudioProvider()

    run, manifest = (
        Pipeline("gmicloud-audio-demo", project_id="examples")
        .step(
            provider,
            model="ElevenLabs-TTS-v3",
            prompt="Welcome to Genblaze, the fastest way to build generative AI pipelines.",
            modality=Modality.AUDIO,
        )
        .run(timeout=120, max_retries=1)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Audio:     {run.steps[0].assets[0].url}")
    if run.steps[0].cost_usd is not None:
        print(f"Cost:      ${run.steps[0].cost_usd:.3f}")


if __name__ == "__main__":
    main()
