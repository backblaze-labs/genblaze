"""Streaming example — iterate over pipeline events in real time.

Uses the mock provider so it runs without API keys. For real providers
(Runway, Luma, Sora), step.progress events will include progress_pct and
preview_url values pulled from the provider's poll loop.
"""

from __future__ import annotations

import asyncio

from genblaze_core import MockProvider, Pipeline

pipe = Pipeline("hero").step(MockProvider(cost_usd=0.04), model="mock-v1", prompt="a sunset")

print("=== Sync stream ===")
for event in pipe.stream():
    if event.type == "pipeline.started":
        print(f"▶ pipeline run_id={event.run_id[:8]}...")
    elif event.type == "step.started":
        idx = event.step_index + 1
        print(f"  → step {idx}/{event.total_steps}: {event.provider}/{event.model}")
    elif event.type == "step.progress" and event.progress_pct is not None:
        preview = f" preview={event.preview_url}" if event.preview_url else ""
        print(f"    {event.progress_pct:.0%}{preview}")
    elif event.type == "step.completed":
        print(f"  ✓ step ok ({event.elapsed_sec:.2f}s)")
    elif event.type == "pipeline.completed":
        print(f"▶ done — hash={event.result.manifest.canonical_hash[:16]}...")


async def main() -> None:
    print("\n=== Async stream ===")
    async for event in pipe.astream():
        # `message` only lives on variants that actually carry a human-readable
        # note (pipeline.started/failed, step.progress, agent.iteration.started).
        # getattr keeps this compact — narrow with isinstance/type if you want
        # to avoid the default-None fallback.
        msg = getattr(event, "message", None) or ""
        print(f"  [{event.type}] {msg}")


asyncio.run(main())
