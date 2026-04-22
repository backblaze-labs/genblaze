#!/usr/bin/env python3
"""Error handling: Partial failures and recovery with fail_fast=False.

Usage:
    python examples/error_handling.py

No API keys needed — uses MockProvider for demonstration.
"""

from genblaze_core import MockProvider, MockVideoProvider, Modality, Pipeline


def main() -> None:
    # Step 0 succeeds, step 1 fails — pipeline continues with fail_fast=False
    result = (
        Pipeline("error-demo")
        .step(
            MockVideoProvider(),
            model="mock-video",
            prompt="A working video generation",
            modality=Modality.VIDEO,
        )
        .step(
            MockProvider(should_fail=True, error_message="Simulated API error"),
            model="mock-failing",
            prompt="This step will fail",
            modality=Modality.VIDEO,
        )
        .run(fail_fast=False)
    )

    run, manifest = result

    print(f"Run status:     {run.status}")
    print(f"Succeeded:      {len(result.succeeded_steps())}")
    print(f"Failed:         {len(result.failed_steps())}")

    # Inspect errors
    summary = result.error_summary()
    if summary:
        print(f"\nError summary:\n{summary}")

    # Manifest is still generated for the steps that succeeded
    print(f"\nHash:           {manifest.canonical_hash}")
    print(f"Verified:       {manifest.verify()}")


if __name__ == "__main__":
    main()
