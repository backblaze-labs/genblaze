#!/usr/bin/env python3
"""Quickstart: Generate a video with OpenAI Sora.

Usage:
    export OPENAI_API_KEY=...
    python examples/quickstart.py

For a simpler demo without API keys, see quickstart_local.py.
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_openai import SoraProvider

    run, manifest = (
        Pipeline("quickstart")
        .step(
            SoraProvider(),
            model="sora-2",
            prompt="A drone shot soaring over a coastal city at golden hour",
            modality=Modality.VIDEO,
        )
        .run(timeout=300)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Video:     {run.steps[0].assets[0].url}")


if __name__ == "__main__":
    main()
