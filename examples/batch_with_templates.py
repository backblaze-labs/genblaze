#!/usr/bin/env python3
"""Example: Batch generation with PromptTemplate variables.

Uses PromptTemplate + batch_run() with dicts to generate multiple
images from a parameterized prompt. Each dict fills in the template
variables, producing independent runs with full provenance.

Usage:
    export OPENAI_API_KEY=sk-...
    python examples/batch_with_templates.py
"""

from genblaze_core import Modality, Pipeline
from genblaze_core.models.prompt_template import PromptTemplate


def main() -> None:
    from genblaze_openai import DalleProvider

    template = PromptTemplate("A {style} painting of a {subject} at {time_of_day}")

    results = (
        Pipeline("batch-templates", project_id="examples")
        .step(
            DalleProvider(),
            model="dall-e-3",
            prompt=template,
            modality=Modality.IMAGE,
            size="1024x1024",
        )
        .batch_run(
            [
                {"style": "watercolor", "subject": "lighthouse", "time_of_day": "sunset"},
                {"style": "oil", "subject": "mountain village", "time_of_day": "dawn"},
                {"style": "pencil sketch", "subject": "old bookshop", "time_of_day": "midnight"},
            ],
            max_concurrency=3,
            timeout=120,
        )
    )

    for i, result in enumerate(results):
        step = result.run.steps[0]
        print(f"\nBatch {i}:")
        print(f"  Prompt:  {step.prompt}")
        print(f"  Status:  {step.status}")
        print(f"  Hash:    {result.manifest.canonical_hash}")
        if step.assets:
            print(f"  Image:   {step.assets[0].url}")


if __name__ == "__main__":
    main()
