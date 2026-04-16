#!/usr/bin/env python3
"""Quickstart (local): Build and verify a manifest with zero API keys.

Demonstrates RunBuilder/StepBuilder to construct a Run manually,
then creates and verifies a SHA-256 manifest — no network calls needed.

Usage:
    pip install genblaze-core
    python examples/quickstart_local.py
"""

from genblaze_core import (
    Manifest,
    Modality,
    RunBuilder,
    StepBuilder,
    StepStatus,
)


def main() -> None:
    # Build a step as if Sora generated a video
    step = (
        StepBuilder("openai", "sora-2")
        .prompt("A drone shot soaring over a coastal city at golden hour")
        .modality(Modality.VIDEO)
        .params(size="1280x720", n_seconds=8)
        .seed(42)
        .status(StepStatus.SUCCEEDED)
        .asset("file://output/demo.mp4", "video/mp4")
        .build()
    )

    # Build a run containing the step
    run = RunBuilder("quickstart-local").add_step(step).build()

    # Create a manifest and verify its integrity
    manifest = Manifest.from_run(run)

    print(f"Run ID:    {run.run_id}")
    print(f"Steps:     {len(run.steps)}")
    print(f"Provider:  {run.steps[0].provider}")
    print(f"Model:     {run.steps[0].model}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")
    print(f"\nManifest JSON (first 200 chars):\n{manifest.to_canonical_json()[:200]}...")


if __name__ == "__main__":
    main()
