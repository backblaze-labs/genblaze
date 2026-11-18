#!/usr/bin/env python3
"""Example: SoraProvider against an OpenAI-compatible Videos API server.

OpenAI retired Sora 2 and its hosted Videos API on 2026-09-24, so this
example targets a server that implements the same API instead, such as a
self-hosted vLLM-Omni or SGLang deployment, or a hosted gateway. The run
still captures full provenance in a manifest.

Usage:
    export SORA_BASE_URL=http://localhost:8091/v1   # include the /v1 prefix
    export SORA_MODEL=your-video-model              # a slug the server lists
    export OPENAI_API_KEY=...                       # whatever the server expects
    python examples/sora_video_pipeline.py

``base_url`` must be https://, or http:// on a loopback host, because the
API key is sent to it.
"""

import os

from genblaze_core import Modality, Pipeline


def main() -> None:
    from genblaze_openai import SoraProvider

    provider = SoraProvider(base_url=os.environ["SORA_BASE_URL"])

    run, manifest = (
        Pipeline("sora-compatible-demo", project_id="examples")
        .step(
            provider,
            model=os.environ["SORA_MODEL"],
            prompt=(
                "A cinematic drone shot gliding over a misty mountain valley "
                "at sunrise, golden light breaking through clouds"
            ),
            modality=Modality.VIDEO,
        )
        .run(timeout=600, max_retries=1)
    )

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {run.steps[0].status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    if run.steps[0].assets:
        print(f"Video:     {run.steps[0].assets[0].url}")


if __name__ == "__main__":
    main()
