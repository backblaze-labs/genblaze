#!/usr/bin/env python3
"""Example: Fan-in audio/video composite with input_from.

Generates video and narration audio independently, then muxes both into
a single MP4 using FFmpegCompositor. Demonstrates input_from=[0, 1] —
a step that pulls outputs from multiple prior steps.

Requirements:
    pip install genblaze-openai genblaze-elevenlabs
    ffmpeg on PATH (the compositor shells out to it)

Usage:
    export OPENAI_API_KEY=sk-...
    export ELEVENLABS_API_KEY=...
    python examples/fan_in_av_composite.py
"""

from genblaze_core import FFmpegCompositor, Modality, Pipeline, StepType


def main() -> None:
    from genblaze_elevenlabs import ElevenLabsSFXProvider
    from genblaze_openai import SoraProvider

    # Step 0: Video generation (Sora)
    # Step 1: Sound effects (ElevenLabs) — independent of step 0
    # Step 2: Local ffmpeg mux — consumes outputs of steps 0 and 1
    result = (
        Pipeline("av-composite", project_id="examples")
        .step(
            SoraProvider(),
            model="sora-2",
            prompt="A campfire crackling under a starry sky, slow orbit",
            modality=Modality.VIDEO,
            seconds=4,
            size="1280x720",
        )
        .step(
            ElevenLabsSFXProvider(),
            model="eleven_text_to_sound_v2",
            prompt="Crackling campfire with distant owl and wind in pines",
            modality=Modality.AUDIO,
            duration_seconds=4,
        )
        .step(
            FFmpegCompositor(),
            model="ffmpeg-mux",
            modality=Modality.VIDEO,
            step_type=StepType.MIX,
            input_from=[0, 1],
        )
        .run(timeout=600)
    )

    print(f"Run ID:    {result.run.run_id}")
    print(f"Status:    {result.run.status}")
    print(f"Hash:      {result.manifest.canonical_hash}")
    print(f"Verified:  {result.manifest.verify()}")

    for i, step in enumerate(result.run.steps):
        print(f"\nStep {i}: {step.provider}/{step.model}")
        print(f"  Modality: {step.modality}")
        print(f"  Inputs:   {len(step.inputs)} asset(s)")
        if step.assets:
            print(f"  Output:   {step.assets[0].url}")


if __name__ == "__main__":
    main()
