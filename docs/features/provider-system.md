<!-- last_verified: 2026-04-08 -->
# Feature: Provider System

## Purpose
Pluggable adapter pattern for generative AI APIs with standardized lifecycle, error classification, and retry tracking.

## Used By
- API: `BaseProvider`, `SyncProvider`, `ReplicateProvider`
- Pipeline: providers execute each step

## Core Functions
- `SyncProvider.generate(step)` — Single-method interface for sync APIs (OpenAI, Stability, etc.)
- `BaseProvider.submit(step)` — Submit work, return prediction ID (polling APIs)
- `BaseProvider.poll(prediction_id)` — Check completion status
- `BaseProvider.fetch_output(prediction_id, step)` — Fetch results, populate assets
- `BaseProvider.invoke(step)` — Orchestrate lifecycle with error handling and retry
- `validate_asset_url(url)` — HTTPS-only URL validation for API response URLs
- `validate_chain_input_url(url)` — Validates chain input URLs; allows `file://` (local outputs) and `https://`

## Provider Types

| Base class | API style | Methods to implement |
|-----------|-----------|---------------------|
| `SyncProvider` | Synchronous (OpenAI, Stability, ElevenLabs) | `generate()` |
| `BaseProvider` | Async/polling (Replicate, fal.ai queue) | `submit()`, `poll()`, `fetch_output()` |
| `FFmpegCompositor` | Local compositor (ffmpeg subprocess) | `generate()` (SyncProvider) |

`SyncProvider` wraps `generate()` into the submit/poll/fetch lifecycle automatically.

`FFmpegCompositor` is a built-in SyncProvider that muxes video + audio assets into a single MP4 container using ffmpeg. It expects `step.inputs` with at least one video and one audio asset (typically via `input_from` fan-in). See [Pipeline compositing](pipeline.md#compositing).

## Canonical Files
- Provider base: `libs/core/genblaze_core/providers/base.py`
- FFmpegCompositor: `libs/core/genblaze_core/providers/compositor.py`
- Compliance tests: `libs/core/genblaze_core/testing.py`
- Replicate adapter: `libs/connectors/replicate/genblaze_replicate/provider.py`
- New provider guide: `docs/guides/new-provider.md`

## Inputs
- `Step` with provider, model, prompt, params

## Outputs
- `Step` with populated `assets`, `provider_payload`, `retries`, `error_code`, `status`, `cost_usd`

## Flow
- `invoke()` calls `submit()` → gets prediction ID (or `generate()` for SyncProvider)
- After `submit()`, fires `on_submit(step_id, prediction_id)` callback if configured (for checkpoint persistence)
- Polls `poll()` until complete (SyncProvider always returns True)
- Calls `fetch_output()` → populates step assets
- On failure: classifies error via `ProviderErrorCode`, tracks retries

## Error Classification
Providers should always raise `ProviderError` with an explicit `error_code`. The base class has a string-based `classify_api_error()` fallback, but well-written providers should not rely on it.

- Provider timeout → `ProviderErrorCode.TIMEOUT` (retryable)
- Rate limit (429) → `ProviderErrorCode.RATE_LIMIT` (retryable)
- Server error (5xx) → `ProviderErrorCode.SERVER_ERROR` (retryable)
- Auth failure (401/403) → `ProviderErrorCode.AUTH_FAILURE`
- Invalid input → `ProviderErrorCode.INVALID_INPUT`
- Unknown error → `ProviderErrorCode.UNKNOWN`

## Cost Tracking

Pricing is declared per-model on `ModelSpec.pricing` (a `PricingStrategy` callable) and resolved automatically by the base class after `fetch_output()`. Each connector exposes its rates via `create_registry()`:

- DALL-E / gpt-image: `tiered` pricing keyed by `(quality, size)`; `gpt-image-2` disclosed as `pricing=None`
- Sora, Luma: `pricing=None` until per-second formulas are disclosed
- TTS (OpenAI, ElevenLabs, LMNT): `per_input_chars(rate, per=...)`
- ElevenLabs SFX: `bucketed_by_duration([((lo, hi), price), ...])`
- Imagen, Decart image: `per_unit(rate)`
- Decart video: `by_param("resolution", ...)` (per-resolution flat rate)
- Google Veo, Stability Audio, GMI Seedance 2.0: per-output-second
- Runway: `by_model_and_param("duration", ...)` tuple-keyed
- Replicate: `per_response_metric` reading `predict_time` from the Replicate response

Users can override any model's pricing at runtime via `Provider.models_default().fork().register_pricing(model_id, strategy)` — no provider release required. Unknown models (newly-released, dated snapshots, aliases) fall back to a permissive spec and submit successfully with `cost_usd=None`.

See [model-registry.md](model-registry.md) for the full `ModelSpec` surface (pricing strategies, param aliases, input routing, schemas, constraints).

## Error Deduplication
Each connector family shares a single error mapper module:
- `genblaze_openai._errors.map_openai_error` (DALL-E, Sora, TTS)
- `genblaze_google._errors.map_google_error` (Veo, Imagen)
- `genblaze_elevenlabs._errors.map_elevenlabs_error` (TTS, SFX)
- `genblaze_gmicloud._errors.map_gmicloud_error` (Kling video)

## Poll Result Caching

`BaseProvider` provides built-in poll result caching to avoid redundant API calls between `poll()` and `fetch_output()`. All polling providers use these helpers:

- `_cache_poll_result(prediction_id, result)` — called in `poll()` when a terminal state is reached
- `_get_cached_poll_result(prediction_id)` — called in `fetch_output()` to retrieve cached result (returns None if not cached, consuming the entry)
- `_cleanup_poll_cache()` — called periodically to evict entries older than 1 hour TTL

`SyncProvider` subclasses do not need caching (poll is always True).

## Retry Jitter
Retry backoff uses jittered exponential backoff: `min(2^attempt, 30) * (1 + random(0, 0.25))` to prevent thundering herd effects.

## Word-Level Timing
TTS providers can populate `Asset.audio.word_timings` with a list of `WordTiming` objects containing `word`, `start`, `end`, and optional `confidence` fields. This is first-class typed data — no longer buried in `metadata` or `provider_payload`.

- **LMNT**: Automatically populates word timings from the `durations` response.
- **ElevenLabs**: Set `with_timestamps=True` in `step.params` to request character-level alignment, which is grouped into word-level `WordTiming` objects.

Backward compatibility: raw dicts in `word_timings` (e.g. from older manifests) are automatically coerced to `WordTiming` via a model validator.

## Provider Capabilities

Providers can declare their capabilities via `get_capabilities()`, which returns a `ProviderCapabilities` dataclass. This enables upfront validation and discovery without invoking the provider.

```python
from genblaze_core import ProviderCapabilities, Modality

class MyProvider(BaseProvider):
    def get_capabilities(self):
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text", "image"],
            max_duration=60.0,
            resolutions=["720p", "1080p"],
            output_formats=["video/mp4"],
            models=["model-v1", "model-v2"],
        )
```

All fields are optional (default `None` = unspecified). The base `get_capabilities()` returns `None`, so existing providers continue to work without implementing it.

| Field | Type | Description |
|-------|------|-------------|
| `supported_modalities` | `list[Modality]` | Output modalities (e.g. VIDEO, AUDIO, IMAGE) |
| `supported_inputs` | `list[str]` | Accepted input types (e.g. "text", "image") |
| `max_duration` | `float` | Maximum output duration in seconds |
| `resolutions` | `list[str]` | Supported resolution labels (e.g. "720p", "4k") |
| `output_formats` | `list[str]` | MIME types the provider can output |
| `models` | `list[str]` | Known model identifiers |

## Asset Contract
- All asset URLs must be HTTPS or file:// (for locally-saved content) — call `validate_asset_url()` for remote URLs
- Providers that receive binary data (gpt-image-\* family, TTS, Imagen, etc.) save to local files and use `file://` URIs. `DalleProvider` also accepts `file://` and `https://` inputs for the `/images/edits` endpoint — routing is automatic based on `step.inputs` presence.
- Set `Asset.media_type` to the most specific MIME type available
- TTS providers should populate `Asset.audio.word_timings` when timing data is available
- Never store API tokens in `step.provider_payload`

## Verification
- Test files: `libs/core/tests/unit/test_sync_provider.py`, `libs/core/tests/unit/test_provider_retry.py`, `libs/connectors/replicate/tests/test_replicate_provider.py`
- Provider compliance: subclass `ProviderComplianceTests` from `genblaze_core.testing` (15 tests: identity, lifecycle, invoke, assets, capabilities, audio metadata, chain validation, normalize_params idempotency, cost tracking)
- Quick verify: `cd libs/core && pytest tests/unit/test_sync_provider.py tests/unit/test_provider_retry.py -v`
- Full verify: `make test`
- Pass criteria: provider lifecycle correct, errors classified with explicit codes, asset URLs validated, compliance tests pass
