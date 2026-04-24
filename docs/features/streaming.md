<!-- last_verified: 2026-04-24 -->
# Streaming

Push-style event iterators over pipeline execution. Use for progress UIs, dashboards, or feeding an agent loop.

`StreamEvent` is a Pydantic discriminated union: every variant is its own subclass with only the fields that variant actually carries. Branching on `event.type` — or `isinstance(event, StepFailedEvent)` — narrows correctly under pyright / mypy and in IDE autocomplete.

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
        case "step.failed":
            print(f"✗ {event.step_id}: {event.error}")
        case "pipeline.completed":
            print(f"Done — hash {event.result.manifest.canonical_hash[:12]}")
```

Async:

```python
async for event in pipe.astream():
    ...
```

## Event types

Every variant carries `type` and `timestamp`. Variant-specific required/optional fields are below (all 10 variants live in `genblaze_core.observability.events`).

| Type | Class | Required fields | Other fields |
|------|-------|-----------------|--------------|
| `pipeline.started` | `PipelineStartedEvent` | `run_id`, `total_steps` | `message` (pipeline name) |
| `step.started` | `StepStartedEvent` | `run_id`, `step_id`, `step_index`, `total_steps`, `provider`, `model` | — |
| `step.progress` | `StepProgressEvent` | `step_id`, `provider`, `model` | `run_id`, `progress_pct`, `preview_url`, `elapsed_sec`, `message`, `data` |
| `step.completed` | `StepCompletedEvent` | `step_id`, `step_index`, `total_steps`, `provider`, `model`, `elapsed_sec` | `run_id`, `step_status`, `step` (in-process `Step`) |
| `step.failed` | `StepFailedEvent` | `step_id`, `step_index`, `total_steps`, `provider`, `model`, `elapsed_sec` | `run_id`, `error`, `step_status`, `step` (in-process `Step`) |
| `pipeline.completed` | `PipelineCompletedEvent` | `run_id` | `run_status`, `manifest_hash`, `result` (in-process `PipelineResult`) |
| `pipeline.failed` | `PipelineFailedEvent` | `run_id` | `message`, `run_status`, `manifest_hash`, `result` |
| `agent.iteration.started` | `AgentIterationStartedEvent` | `iteration`, `total` | `message` (prior feedback) |
| `agent.iteration.evaluated` | `AgentIterationEvaluatedEvent` | `iteration`, `passed` | `score`, `feedback`, `result` |
| `agent.completed` | `AgentCompletedEvent` | `passed`, `iterations` | `total_cost_usd`, `result` |

Notes:

- **`step` / `result` are in-process only** — present on the Python object, excluded from JSON serialization. Wire consumers read the derived `step_status` / `manifest_hash` / `run_status` / `error` fields.
- **`step.failed` carries `error`, not `message`.** The legacy dataclass emitted both keys with the same string for failure events; the discriminated union keeps only `error`. Webhook / SSE / log consumers that key on `message` for failures should switch.
- **Agent events expose flat fields.** `event.iteration` / `event.score` / `event.passed`, not `event.data["iteration"]` etc.
- Not every variant has `run_id` — agent-loop events don't (they're pipeline-independent).

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

## Early break

Breaking out of iteration before the terminal event (`pipeline.completed` / `pipeline.failed`) is safe and non-blocking. The worker thread (sync) or task (async) keeps running until the pipeline naturally completes, but control returns to the caller immediately. Consequences:

- Remaining events after the break are discarded.
- Any post-break exception in the pipeline is suppressed.
- Asset generation, sink writes, and tracer callbacks still run to completion — breaking out of the stream does **not** abort the pipeline.

To actually cancel pending work, use `astream()` (which cancels the worker task; in-flight `asyncio` awaits raise `CancelledError`) or kill the surrounding process. There is no way to interrupt a sync `run()` mid-flight.

## Buffering / backpressure

Event queues are unbounded. In practice this is fine — even a 30-minute video run emits only ~60 events (≤100 KB) because providers poll at 1–30s intervals. The queue grows only while a consumer is blocked; a slow consumer holding the iterator for minutes could buffer a few MB at most. No backpressure is applied to providers; if you need to throttle work, use `max_concurrency` on the pipeline rather than slowing the stream consumer.

## Narrowing with `isinstance`

Each variant is importable from `genblaze_core.observability`. `isinstance(ev, StepFailedEvent)` narrows to that class with its required fields; type checkers catch invalid access at lint time.

```python
from genblaze_core.observability import (
    StepFailedEvent, PipelineCompletedEvent, StreamEvent,
)

for ev in pipe.stream():
    if isinstance(ev, StepFailedEvent):
        log.error("step %s failed: %s", ev.step_id, ev.error)
    elif isinstance(ev, PipelineCompletedEvent):
        publish_manifest(ev.manifest_hash, ev.run_status)
```

`isinstance(ev, StreamEvent)` remains truthy for any variant — useful in plumbing code that treats events uniformly.

## Wire format + TypeScript consumers

`event.to_dict()` is a JSON-safe serialization: `type` + `timestamp` + the variant's declared fields, with in-process `step` / `result` excluded. Under the hood it's `model_dump(mode="json", exclude_none=True)`.

For external parsers:

```python
from genblaze_core.observability import StreamEventAdapter

# Parse an inbound event dict into the correct variant via the `type` discriminator
event = StreamEventAdapter.validate_python(some_json_dict)  # returns StepFailedEvent, etc.
```

TypeScript / Node consumers should pull generated types from `libs/spec/ts/genblaze.d.ts` (or the future `@genblaze/spec` npm package). The same discriminator narrows in TypeScript:

```ts
import type { StreamEvent } from "@genblaze/spec";

function render(ev: StreamEvent) {
  if (ev.type === "step.failed") {
    // ev.error, ev.step_id — fully typed
  }
}
```

The authoritative JSON Schemas live at `libs/spec/schemas/events/v1/` (one per variant + a parent `stream-event.schema.json` with `oneOf` + `discriminator`). `test_spec_conformance.py` enforces Pydantic ↔ schema parity on every `make test`.

## Internals

- `stream()` runs `run()` in a worker thread, yielding from `queue.Queue`.
- `astream()` runs `arun()` as an `asyncio.Task`, yielding from `asyncio.Queue`.
- Event variants + `AnyStreamEvent` + `StreamEventAdapter`: `libs/core/genblaze_core/observability/events.py`.
- Construction sites: `libs/core/genblaze_core/pipeline/streaming.py`, `pipeline/pipeline.py`, `agents/loop.py`.
