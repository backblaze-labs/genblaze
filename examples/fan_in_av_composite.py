#!/usr/bin/env python3
"""Example: Fan-in audio/video composite with input_from.

Generates video and audio independently, then feeds both into a
compositor step using input_from=[0, 1]. This pattern is used for
AV muxing, multi-source editing, and any step that needs outputs
from multiple prior steps.

Usage:
    export OPENAI_API_KEY=sk-...
    export ELEVENLABS_API_KEY=...
    python examples/fan_in_av_composite.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_openai import SoraProvider

    # Step 0: Generate video
    # Step 1: Generate audio (using a TTS or SFX provider)
    # Step 2: Composite — receives outputs from steps 0 and 1 via input_from
    result = (
        Pipeline("av-composite", project_id="examples")
        .step(
            SoraProvider(),
            model="sora-2",
            prompt="A campfire crackling under a starry sky",
            modality=Modality.VIDEO,
        )
        .step(
            SoraProvider(),
            model="sora-2",
            prompt="Ambient forest sounds with crackling fire",
            modality=Modality.AUDIO,
        )
        .step(
            SoraProvider(),
            model="sora-2",
            prompt="Merge video and audio into a single composition",
            modality=Modality.VIDEO,
            input_from=[0, 1],
        )
        .run(timeout=600)
    )

    print(f"Run ID:    {result.run.run_id}")
    print(f"Status:    {result.run.status}")
    print(f"Hash:      {result.manifest.canonical_hash}")

    for i, step in enumerate(result.run.steps):
        print(f"\nStep {i}: {step.provider}/{step.model}")
        print(f"  Modality: {step.modality}")
        print(f"  Inputs:   {len(step.inputs)} asset(s)")
        if step.assets:
            print(f"  Output:   {step.assets[0].url}")


if __name__ == "__main__":
    main()
