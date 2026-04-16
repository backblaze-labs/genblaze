<!-- last_verified: 2026-03-16 -->
# Feature: Pipeline

## Purpose
Fluent API for building and executing multi-step generative media workflows with automatic manifest creation.

## Used By
- API: `Pipeline` class, `PipelineResult`
- Integrates with: providers, sinks, media embedding

## Core Functions
- `Pipeline.step()` — Add a generation step (supports `fallback_models` for model fallback chains)
- `Pipeline.cache(StepCache)` — Enable step-level caching
- `Pipeline.run(sink=None, fail_fast=True, pipeline_timeout=None, on_step_complete=None)` — Execute all steps synchronously, return `PipelineResult`
- `Pipeline.arun(sink=None, fail_fast=True, max_concurrency=None, pipeline_timeout=None, on_step_complete=None)` — Execute all steps asynchronously with optional concurrency limit
- `Pipeline.batch_run(prompts, max_concurrency=5, sink=None, pipeline_timeout=None, on_step_complete=None)` — Execute pipeline independently for each prompt (sync)
- `Pipeline.abatch_run(prompts, max_concurrency=5, sink=None, pipeline_timeout=None, on_step_complete=None)` — Execute pipeline for each prompt with async concurrency control
- `PipelineResult.save()` — Save output with optional manifest embedding
- `StepCache` — File-based cache keyed by deterministic hash of step inputs
- `StepCompleteEvent` — Dataclass fired via `on_step_complete` after each step finishes

## Canonical Files
- Pipeline: `libs/core/genblaze_core/pipeline/pipeline.py`
- PipelineResult: `libs/core/genblaze_core/pipeline/result.py`
- StepCache: `libs/core/genblaze_core/pipeline/cache.py`

## Inputs
- `name`: str — Pipeline name
- `tenant_id`: str — Tenant identifier
- `max_concurrency`: int | None — Global concurrency limit for async steps
- `provider`: BaseProvider — Provider adapter for each step
- `model`, `prompt`, `step_type`, `**params`: Step configuration
- `fallback_models`: list[str] | None — Models to try on `MODEL_ERROR` failure
- `sink`: optional BaseSink — Output destination on `.run()`
- `pipeline_timeout`: float | None — End-to-end timeout in seconds for the entire pipeline (checked before each step)
- `on_step_complete`: Callable[[StepCompleteEvent], None] | None — Callback fired after each step finishes (success or failure)
- `on_submit`: Callable[[str, Any], None] | None — Callback fired after provider `submit()` with `(step_id, prediction_id)` for checkpoint persistence
- `input_from`: list[int] | int | None — Route inputs from specific prior steps by index (overrides chain mode)

## Outputs
- `PipelineResult` — Contains `.run` (Run) and `.manifest` (Manifest)
- Supports tuple unpacking: `run, manifest = result`
- Side effects: provider API calls, optional sink writes

## Flow
- `Pipeline()` creates pipeline with name and tenant
- `.step()` queues step definitions (provider, model, prompt, params)
- `.run()` / `.arun()` iterates steps → calls `provider.invoke(step)` → collects assets
- With `fail_fast=True` (default), stops on first failed step
- With `fail_fast=False`, continues executing remaining steps after failure
- Builds `Run` and `Manifest` from completed steps
- If `sink` provided, writes run data
- Returns `PipelineResult`

## Edge Cases
- Provider failure mid-pipeline → step gets `error_code`; with `fail_fast=True` pipeline stops, with `fail_fast=False` it continues
- Empty pipeline (no steps) → raises `GenblazeError`
- Sink write failure → does not affect manifest/run creation
- Cache hit → provider not called, cached step returned directly
- Cache stores only successful steps — failed steps are not cached
- Exception-raising tasks in `_gather_fail_fast` → captured as FAILED steps (not dropped)
- Model fallback: on `MODEL_ERROR`, tries each `fallback_models` entry; records `fallback_from`/`fallback_model` in step metadata. Cache stores successful fallback results under the fallback model's key (not the original), so a later run with the fallback model as primary gets a cache hit
- `batch_run` / `abatch_run`: each prompt gets independent pipeline execution; `max_concurrency` limits parallel runs
- `pipeline_timeout` raises `PipelineTimeoutError` when wall-clock time exceeds limit (checked before each step, not mid-step)
- `on_step_complete` fires for both succeeded and failed steps; for concurrent `arun()`, fires after all steps complete
- `input_from` on `.step()` allows fan-in from specific prior steps by index (overrides chain mode)

## Compositing

Use `FFmpegCompositor` to mux generated video and audio into a single MP4. Combine with `input_from` to fan-in from multiple prior steps:

```python
from genblaze_core import Pipeline, Modality, FFmpegCompositor
from genblaze_core.models.enums import StepType

result = (
    Pipeline("av-mux")
    .step(video_provider, model="sora-2", prompt="sunset timelapse", modality=Modality.VIDEO)
    .step(audio_provider, model="eleven_v3", prompt="ocean waves", modality=Modality.AUDIO)
    .step(
        FFmpegCompositor(),
        model="mux",
        modality=Modality.VIDEO,
        step_type=StepType.MIX,
        input_from=[0, 1],  # fan-in: video from step 0 + audio from step 1
    )
    .run()
)

muxed = result.run.steps[2].assets[0]
assert muxed.video.has_audio is True
assert len(muxed.tracks) == 2  # video + audio tracks
```

Requires `ffmpeg` installed on the system. Configurable via `FFmpegCompositor(output_dir=..., ffmpeg_path=..., timeout=...)`.

## Testing

Use `MockProvider`, `MockVideoProvider`, and `MockAudioProvider` from `genblaze_core.testing` to test pipelines without real API calls:

```python
from genblaze_core.testing import MockVideoProvider, MockAudioProvider
from genblaze_core.pipeline import Pipeline

# Basic — returns video/mp4 with VideoMetadata(codec="h264")
result = Pipeline("test").step(MockVideoProvider(), model="m", prompt="sunset").run()
assert result.run.steps[0].assets[0].video.codec == "h264"

# Multi-step AV pipeline
result = (
    Pipeline("av-test")
    .step(MockVideoProvider(), model="v", prompt="sunset timelapse")
    .step(MockAudioProvider(), model="a", prompt="ocean waves")
    .run()
)

# Simulate failures
from genblaze_core.testing import MockProvider
from genblaze_core.models.enums import ProviderErrorCode

provider = MockProvider(should_fail=True, error_code=ProviderErrorCode.RATE_LIMIT)
```

See `MockProvider` for full options: `assets`, `latency`, `cost_usd`, `should_fail`, `error_code`.

## Verification
- Test files: `libs/core/tests/unit/test_pipeline.py`, `libs/core/tests/unit/test_mock_providers.py`, `libs/core/tests/integration/test_pipeline_embed_roundtrip.py`
- Required cases: single step, multi-step, tuple unpacking, cache hit, cache miss, cache clear, arun, arun with cache, fail_fast, empty pipeline guard, embed roundtrip
- Quick verify: `cd libs/core && pytest tests/unit/test_pipeline.py -v`
- Full verify: `make test`
- Pass criteria: all pipeline tests green, PipelineResult fields populated
