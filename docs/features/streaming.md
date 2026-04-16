<!-- last_verified: 2026-04-17 -->
# Streaming

Push-style event iterators over pipeline execution. Use for progress UIs, dashboards, or feeding an agent loop.

## Quickstart

```python
from genblaze_core import Pipeline
from genblaze_openai import DalleProvider

pipe = Pipeline("hero").step(DalleProvider(), model="dall-e-3", prompt="a sunset", modality="image")

for event in pipe.stream():
    match event.type:
        case "step.progress":
            print(f"{event.progress_pct:.0%} {event.preview_url or ''}")
        case "step.completed":
            print(f"✓ {event.step.provider}/{event.step.model}")
        case "pipeline.completed":
            print(f"Done — hash {event.result.manifest.canonical_hash[:12]}")
```

Async:

```python
async for event in pipe.astream():
    ...
```

## Event types

| Type | When | Key fields |
|------|------|------------|
| `pipeline.started` | Before the first step runs | `run_id`, `total_steps`, `message` (pipeline name) |
| `step.started` | Before provider submit | `step_id`, `step_index`, `provider`, `model` |
| `step.progress` | Provider poll ticks + user-driven | `progress_pct`, `preview_url`, `elapsed_sec` |
| `step.completed` | Step succeeded | `step` (full Step model) |
| `step.failed` | Step failed | `step`, `message` (error) |
| `pipeline.completed` | Pipeline succeeded | `result` (PipelineResult) |
| `pipeline.failed` | Pipeline failed | `result`, `message` (error summary) |
| `agent.iteration.started` | AgentLoop began a new iteration | `data.iteration`, `message` (prior feedback) |
| `agent.iteration.evaluated` | Evaluator returned for an iteration | `data.passed`, `data.score`, `data.feedback` |
| `agent.completed` | AgentLoop finished | `result`, `data.passed`, `data.iterations`, `data.total_cost_usd` |

All events share: `type`, `timestamp`, and an optional `run_id`.

## Preview URLs

Providers that expose in-progress artifacts (Runway intermediate frames, waveform thumbnails, etc.) populate `ProgressEvent.preview_url`. Consumers receive them through `step.progress` events automatically — no special provider wiring required beyond passing `preview_url` to `_fire_progress`.

```python
# Inside a provider
self._fire_progress(
    step,
    config,
    status="processing",
    start_time=start_time,
    progress_pct=0.4,
    preview_url="https://cdn.example.com/runs/abc/preview-0040.jpg",
)
```

## Relationship to legacy callbacks

`on_progress` and `on_step_complete` still work exactly as before. `stream()` layers on top — it wraps the existing callback wiring with a queue. You can combine both:

```python
def log_progress(ev):
    print(f"[log] {ev.provider} {ev.progress_pct}")

for event in pipe.stream(on_progress=log_progress):  # both fire
    ...
```

## Error handling

If the pipeline raises an uncaught exception (not captured as a step failure), `stream()` drains any events already queued, then re-raises the exception after the iterator completes. Wrap iteration in a try/except to surface it.

## Internals

- `stream()` runs `run()` in a worker thread, yielding from `queue.Queue`.
- `astream()` runs `arun()` as an `asyncio.Task`, yielding from `asyncio.Queue`.
- See `libs/core/genblaze_core/pipeline/streaming.py`.
