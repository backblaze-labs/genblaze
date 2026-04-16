#!/usr/bin/env python3
"""Example: ElevenLabs Text-to-Speech pipeline.

Generates speech audio using ElevenLabs with full provenance.

Models:
    - eleven_v3: Latest, most expressive (70+ languages)
    - eleven_multilingual_v2: Stable multilingual (29 languages)
    - eleven_flash_v2_5: Ultra-low latency (~75ms)

Usage:
    export ELEVENLABS_API_KEY=...
    python examples/elevenlabs_tts_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_elevenlabs import ElevenLabsTTSProvider

    provider = ElevenLabsTTSProvider(output_dir="output/audio")

    run, manifest = (
        Pipeline("elevenlabs-tts-demo", project_id="examples")
        .step(
            provider,
            model="eleven_v3",
            prompt=(
                "Welcome to Genblaze. This audio was generated with "
                "ElevenLabs and tracked with full provenance."
            ),
            modality=Modality.AUDIO,
            voice_id="JBFqnCBsd6RMkjVDRZzb",
            output_format="mp3_44100_128",
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
