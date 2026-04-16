#!/usr/bin/env python3
"""Example: LMNT Text-to-Speech pipeline.

Generates ultra-low latency speech with full provenance.

Usage:
    export LMNT_API_KEY=...
    python examples/lmnt_tts_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_lmnt import LMNTProvider

    provider = LMNTProvider(output_dir="output/audio")

    run, manifest = (
        Pipeline("lmnt-tts-demo", project_id="examples")
        .step(
            provider,
            model="lmnt-1",
            prompt="The quick brown fox jumps over the lazy dog.",
            modality=Modality.AUDIO,
            voice="lily",
        )
        .run(timeout=30, max_retries=1)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Audio:     {run.steps[0].assets[0].url}")


if __name__ == "__main__":
    main()
