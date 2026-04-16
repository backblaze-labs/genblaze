<!-- last_verified: 2026-04-17 -->
# Observability

Pluggable tracers surface pipeline lifecycle events to logs, OpenTelemetry, LangSmith, or any custom backend.

## Quickstart

```python
from genblaze_core import Pipeline, LoggingTracer

Pipeline("my-pipe", tracer=LoggingTracer()).step(...).run()
```

Or layered:

```python
from genblaze_core import CompositeTracer, LoggingTracer, OTelTracer

tracer = CompositeTracer([LoggingTracer(), OTelTracer()])
Pipeline("my-pipe", tracer=tracer).step(...).run()
```

## Tracer ABC

Implement any subset of these hooks — defaults are no-ops:

| Hook | When |
|------|------|
| `on_run_start(run_id, name, *, tenant_id, total_steps, metadata)` | Pipeline begins |
| `on_step_start(run_id, step, *, step_index, total_steps)` | Before provider.invoke |
| `on_event(event: StreamEvent)` | Every StreamEvent (including progress ticks) |
| `on_step_end(run_id, step, *, duration_ms, step_index)` | After provider.invoke |
| `on_run_end(run_id, result: PipelineResult)` | Pipeline finishes |

**Safety**: tracer exceptions are caught + logged at WARNING. A broken tracer never breaks a pipeline.

## Built-in backends

- **NoOpTracer** — default; zero cost
- **LoggingTracer(logger=StructuredLogger)** — emits JSON events via the structured logger
- **OTelTracer(tracer_name)** — creates OpenTelemetry spans for run + step boundaries; requires `opentelemetry-api` installed
- **CompositeTracer([...])** — fan-out to multiple tracers, isolates failures

## LangSmith integration

```bash
pip install genblaze-langsmith
```

```python
from genblaze_langsmith import LangSmithTracer
from genblaze_core import Pipeline

tracer = LangSmithTracer(project_name="my-gen-pipeline")
Pipeline("hero", tracer=tracer).step(...).run()
```

Each pipeline run appears as a `chain` run in LangSmith with step-level child runs for provider calls. Prompts, params, asset URLs, and manifest hashes are captured.

## Migration from `structured_log=True`

The legacy flag still works — it resolves to `LoggingTracer` internally. New code should pass an explicit `tracer=` for clarity:

```python
# Old
Pipeline("p", structured_log=True).step(...)
# New
Pipeline("p", tracer=LoggingTracer()).step(...)
```

## Custom tracers

Subclass `Tracer`. Example that exports per-step timing to Prometheus:

```python
from genblaze_core import Tracer
from prometheus_client import Histogram

STEP_DURATION = Histogram("genblaze_step_duration_ms", "Step execution time", ["provider", "model"])

class PrometheusTracer(Tracer):
    def on_step_end(self, run_id, step, *, duration_ms, step_index):
        STEP_DURATION.labels(provider=step.provider, model=step.model).observe(duration_ms)
```

## Internals

- `libs/core/genblaze_core/observability/tracer.py` — ABC + built-ins
- `libs/core/genblaze_core/observability/events.py` — StreamEvent model
- `libs/connectors/langsmith/` — LangSmith backend (separate package)
