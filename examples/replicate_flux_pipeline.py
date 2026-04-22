#!/usr/bin/env python3
"""Example: Replicate Flux pipeline (requires REPLICATE_API_TOKEN env var).

This example demonstrates the full Pipeline API with the Replicate provider.
It generates an image using Flux Schnell and embeds the manifest into the output PNG.

Usage:
    export REPLICATE_API_TOKEN=r8_...
    python examples/replicate_flux_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    # Import here so the example can be read without replicate installed
    from genblaze_replicate import ReplicateProvider

    provider = ReplicateProvider()

    # Build and execute pipeline
    run, manifest = (
        Pipeline("flux-demo", project_id="examples")
        .step(
            provider,
            model="black-forest-labs/flux-schnell",
            prompt="a photorealistic golden retriever puppy sitting in a field of wildflowers, "
            "golden hour lighting, shallow depth of field",
            modality=Modality.IMAGE,
            num_outputs=1,
            aspect_ratio="1:1",
        )
        .run(timeout=120, max_retries=1)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Steps:     {len(run.steps)}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Assets:    {len(run.steps[0].assets)}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Output:    {run.steps[0].assets[0].url}")


if __name__ == "__main__":
    main()
