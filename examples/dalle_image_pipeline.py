#!/usr/bin/env python3
"""Example: OpenAI DALL-E image generation pipeline.

Generates an image using DALL-E 3 or gpt-image-1 with full provenance.

Models:
    - gpt-image-1: Latest, most capable
    - dall-e-3: High quality with prompt rewriting
    - dall-e-2: Legacy

Usage:
    export OPENAI_API_KEY=sk-...
    python examples/dalle_image_pipeline.py
"""

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_openai import DalleProvider

    provider = DalleProvider()

    run, manifest = (
        Pipeline("dalle-demo", project_id="examples")
        .step(
            provider,
            model="dall-e-3",
            prompt="A watercolor painting of a cozy bookshop on a rainy evening",
            modality=Modality.IMAGE,
            size="1024x1024",
            quality="hd",
        )
        .run(timeout=120, max_retries=1)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Image:     {run.steps[0].assets[0].url}")


if __name__ == "__main__":
    main()
