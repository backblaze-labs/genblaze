#!/usr/bin/env python3
"""Example: OpenAI Text-to-Speech audio pipeline.

Generates speech audio from text with full provenance.

Models:
    - tts-1: Fast, good for real-time
    - tts-1-hd: Higher quality
    - gpt-4o-mini-tts: Most expressive, supports instructions

Usage:
    export OPENAI_API_KEY=sk-...
    python examples/tts_audio_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_openai import OpenAITTSProvider

    provider = OpenAITTSProvider(output_dir="output/audio")

    run, manifest = (
        Pipeline("tts-demo", project_id="examples")
        .step(
            provider,
            model="tts-1-hd",
            prompt=(
                "Welcome to Genblaze, the open-source framework for "
                "generative media pipelines with provenance tracking."
            ),
            modality=Modality.AUDIO,
            voice="nova",
            response_format="mp3",
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
