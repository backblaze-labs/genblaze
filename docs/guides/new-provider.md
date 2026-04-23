# Adding a New Provider

Step-by-step guide for contributing a provider adapter to genblaze.

## Quick start with Claude Code

If you use [Claude Code](https://claude.ai/claude-code), the fastest way to scaffold a new provider is the built-in skill:

```
/scaffold-provider <name> <modality> [sync|polling]
```

Examples:
```
/scaffold-provider fal image sync
/scaffold-provider hedra video polling
/scaffold-provider picovoice audio
```

This reads the existing connectors and generates all required files (package, provider, error mapper, tests) following current conventions. You then fill in the actual API calls.

The rest of this guide documents what the skill generates and why — read it to understand the contracts your provider must satisfy.

## Choose your base class

| API style | Base class | What you implement |
|-----------|------------|-------------------|
| Synchronous (OpenAI, Stability, ElevenLabs) | `SyncProvider` | `generate(step) → Step` |
| Async/polling (Replicate, fal.ai queue) | `BaseProvider` | `submit()`, `poll()`, `fetch_output()` |

**Use `SyncProvider` unless your API requires polling.** Most providers are sync.

## 1. Create the package

```
libs/connectors/myprovider/
├── genblaze_myprovider/
│   ├── __init__.py
│   └── provider.py
├── tests/
│   ├── __init__.py
│   └── test_myprovider.py
└── pyproject.toml
```

## 2. Set up pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "genblaze-myprovider"
version = "0.1.0"
description = "MyProvider adapter for genblaze"
requires-python = ">=3.11"
license = "MIT"
dependencies = [
    "genblaze-core>=0.1.0",
    "myprovider-sdk>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=7.0"]

# Required: registers your provider for discover_providers()
[project.entry-points."genblaze.providers"]
myprovider = "genblaze_myprovider:MyProvider"

[tool.hatch.build.targets.wheel]
packages = ["genblaze_myprovider"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

The entry point under `genblaze.providers` is how `discover_providers()` and the CLI `replay` command find your provider at runtime.

## 3. Implement the provider

### Sync provider (recommended for most APIs)

```python
"""MyProvider — adapter for the MyProvider API."""
from __future__ import annotations
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import SyncProvider, validate_asset_url
from genblaze_core.runnable.config import RunnableConfig


class MyProvider(SyncProvider):
    name = "myprovider"

    def __init__(self, api_key: str | None = None):
        super().__init__()
        self._api_key = api_key
        self._client: Any = None

    def _get_client(self):
        if self._client is None:
            try:
                import myprovider_sdk
                self._client = myprovider_sdk.Client(api_key=self._api_key)
            except ImportError as exc:
                raise ProviderError(
                    "myprovider-sdk not installed. Run: pip install myprovider-sdk"
                ) from exc
        return self._client

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        client = self._get_client()
        try:
            # Map step params to your API's format
            resp = client.generate(
                prompt=step.prompt,
                **step.params,
            )

            # Attach output assets — always validate URLs
            for output_url in resp.outputs:
                validate_asset_url(output_url)
                step.assets.append(Asset(
                    url=output_url,
                    media_type="image/png",  # use most specific type available
                ))

            return step
        except ProviderError:
            raise
        except Exception as exc:
            # Classify errors so retry logic works correctly
            raise ProviderError(
                f"MyProvider failed: {exc}",
                error_code=ProviderErrorCode.UNKNOWN,
            ) from exc
```

### Async/polling provider

Only use this if your API returns a job ID and requires polling:

```python
from genblaze_core.providers.base import BaseProvider

class MyAsyncProvider(BaseProvider):
    name = "myprovider"

    def submit(self, step: Step, config=None) -> Any:
        # Start the job, return the job/prediction ID
        return client.create_job(prompt=step.prompt, **step.params).id

    def poll(self, prediction_id: Any, config=None) -> bool:
        # Return True when the job is finished (success or failure)
        job = client.get_job(prediction_id)
        return job.status in ("succeeded", "failed", "canceled")

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        # Retrieve results, attach assets, raise on failure
        job = client.get_job(prediction_id)
        if job.status == "failed":
            raise ProviderError(job.error, error_code=ProviderErrorCode.UNKNOWN)
        for url in job.output_urls:
            validate_asset_url(url)
            step.assets.append(Asset(url=url, media_type="image/png"))
        return step
```

## 4. Declare capabilities

Override `get_capabilities()` to declare what your provider supports. This enables upfront validation in Pipeline before any API calls are made.

```python
from genblaze_core.providers.base import ProviderCapabilities

def get_capabilities(self) -> ProviderCapabilities:
    return ProviderCapabilities(
        supported_modalities=[Modality.IMAGE],
        supported_inputs=["text"],
        models=["my-model-v1", "my-model-v2"],
        output_formats=["image/png"],
    )
```

Set `accepts_chain_input=True` if your provider reads `step.inputs` (image-to-video, audio-to-audio, etc.).

## 5. Parameter normalization

Override `normalize_params()` to map standard parameter names to your API's native names. This ensures users can use consistent parameter names across providers.

```python
def normalize_params(self, params: dict, modality=None) -> dict:
    p = dict(params)
    # duration → my_api_duration (your API's native key)
    if "duration" in p and "my_api_duration" not in p:
        p["my_api_duration"] = p.pop("duration")
    # aspect_ratio → ratio
    if "aspect_ratio" in p and "ratio" not in p:
        p["ratio"] = p.pop("aspect_ratio")
    return p
```

Standard parameter names to map: `duration`, `resolution`, `aspect_ratio`, `voice_id`, `output_format`.

**Idempotency rule:** `normalize_params(normalize_params(p)) == normalize_params(p)`. Use `if "x" in p and "native_x" not in p` guards.

## 6. Chain input validation

If your provider accepts `step.inputs` (chain inputs from prior pipeline steps), **always** validate URLs before forwarding them to external APIs:

```python
from genblaze_core.providers.base import validate_chain_input_url

if step.inputs:
    for inp in step.inputs:
        validate_chain_input_url(inp.url)  # Rejects http:// and other unsafe schemes
        params["image"] = inp.url
```

This prevents SSRF — only `https://` and `file://` URLs are allowed.

## 7. Error classification

Providers **should** set `error_code` on `ProviderError` for proper retry behavior:

| Error code | When to use | Retried? |
|-----------|-------------|----------|
| `TIMEOUT` | Request timed out | Yes |
| `RATE_LIMIT` | 429 / rate limit hit | Yes |
| `SERVER_ERROR` | 500/502/503 | Yes |
| `AUTH_FAILURE` | 401/403 | No |
| `INVALID_INPUT` | 400 / validation error | No |
| `MODEL_ERROR` | Model not found / crashed | No |
| `UNKNOWN` | Anything else | No |

If you don't set an error code, the base class falls back to regex matching on the exception message — this is fragile and should not be relied on.

## 8. Error mapper module

Create a `_errors.py` module in your connector package with a shared error mapping function. This keeps provider.py focused on business logic and follows the convention used by all connectors.

For most providers, delegate to the shared `classify_api_error` classifier:

```python
# genblaze_myprovider/_errors.py
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error

def map_myprovider_error(exc: Exception) -> ProviderErrorCode:
    """Map a MyProvider API exception to a ProviderErrorCode."""
    return classify_api_error(exc)
```

If your provider has SDK-specific exception types or HTTP status codes that need special handling (e.g., gRPC codes, custom error classes), add provider-specific checks before the fallback:

```python
def map_myprovider_error(exc: Exception) -> ProviderErrorCode:
    """Map a MyProvider API exception to a ProviderErrorCode."""
    # Provider-specific checks first
    if isinstance(exc, MySDKRateLimitError):
        return ProviderErrorCode.RATE_LIMIT
    # Fall back to shared string-based classifier
    return classify_api_error(exc)
```

Import and use in provider.py: `from ._errors import map_myprovider_error`

## 9. Asset contract

- `Asset.url` must be an absolute HTTPS URL — call `validate_asset_url()` before storing
- `Asset.media_type` should be the most specific MIME type you can determine (e.g., `image/png` not `image/*`)
- If your API returns base64 data instead of URLs, you'll need to write the data to a temp file or storage backend and set the URL accordingly
- Never store API tokens or secrets in `step.provider_payload`

## 10. Asset metadata

Populate typed metadata on assets so downstream consumers (embedding, analytics) can inspect output properties without probing files.

**Audio providers** — set `asset.audio`:
```python
from genblaze_core.models.asset import AudioMetadata
asset.audio = AudioMetadata(channels=1, codec="mp3")
```

**Video providers** — set `asset.video`:
```python
from genblaze_core.models.asset import VideoMetadata
asset.video = VideoMetadata(has_audio=False, codec="h264")
```

## 11. Cost tracking

Pricing is declared **per model** on `ModelSpec.pricing`. The base class runs the strategy after `fetch_output()` and sets `step.cost_usd` — your connector doesn't compute cost itself. Expose your specs via `create_registry()`:

```python
from genblaze_core.providers import (
    BaseProvider, ModelRegistry, ModelSpec, per_unit,
)

def _build_registry() -> ModelRegistry:
    return ModelRegistry(
        defaults={
            "my-model-v1": ModelSpec(model_id="my-model-v1", pricing=per_unit(0.10)),
            "my-model-v2": ModelSpec(model_id="my-model-v2", pricing=per_unit(0.25)),
        },
    )

class MyProvider(BaseProvider):
    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return _build_registry()
```

Packaged pricing helpers cover the common shapes:

| Shape | Helper |
|---|---|
| Flat per output asset | `per_unit(rate)` |
| Per second of output | `per_output_second(rate)` |
| Per N characters of prompt | `per_input_chars(rate, per=1000)` |
| Table lookup `(quality, size) → price` | `tiered(table, key=lambda ctx: ...)` |
| Bucketed by output duration | `bucketed_by_duration([((lo, hi), price), ...])` |
| Single-param lookup | `by_param("resolution", {"480p": 0.04, "720p": 0.08})` |
| `(model, param) → price` | `by_model_and_param("duration", {...})` |
| Pull from response | `per_response_metric(lambda ctx: ctx.provider_payload[...])` |

For anything else, write a `PricingStrategy` callable — `Callable[[PricingContext], float | None]`. Keep it pure and synchronous (no I/O).

**Unknown models** (newly-released, snapshots, aliases) fall back to the permissive default spec — the request goes through, `cost_usd=None`. Users can add pricing at runtime via `MyProvider.models_default().fork().register_pricing(...)` — no provider release required.

See [`docs/features/model-registry.md`](../features/model-registry.md) for the full `ModelSpec` surface (param aliases, schemas, input routing).

## 12. Poll result caching (BaseProvider only)

For polling providers, cache the poll result so `fetch_output()` doesn't make a redundant API call. Use the base class helpers:

```python
def poll(self, prediction_id, config=None):
    job = client.get_job(prediction_id)
    if job.status in ("succeeded", "failed"):
        self._cache_poll_result(prediction_id, job)  # Cache for fetch_output
        return True
    return False

def fetch_output(self, prediction_id, step):
    job = self._get_cached_poll_result(prediction_id)  # Use cache if available
    if job is None:
        job = client.get_job(prediction_id)  # Fallback to fresh fetch
    ...
```

Also call `super().__init__()` in your constructor to initialize the cache.

## 13. Advanced: timing hints with SubmitResult (BaseProvider only)

If your provider knows roughly how long a generation will take, return a `SubmitResult` from `submit()` instead of a plain prediction ID. The base class will delay the first poll, reducing unnecessary API calls.

```python
from genblaze_core.providers.base import SubmitResult

def submit(self, step, config=None):
    job = client.create_job(prompt=step.prompt)
    # Provider API returns an estimated completion time
    return SubmitResult(
        prediction_id=job.id,
        estimated_seconds=job.estimated_seconds,  # e.g. 30.0
    )
```

The base class delays the first poll by ~80% of the estimate. This is backward compatible — returning a plain ID still works.

## 14. Advanced: crash recovery with resume()

`BaseProvider` provides `resume(prediction_id, step)` and `aresume()` for recovering in-flight jobs after a worker restart. These skip `submit()` and go directly to polling. No override is needed — they work automatically as long as `poll()` and `fetch_output()` are implemented correctly.

```python
# Recover a job that was submitted before a crash
result = provider.resume("job-abc123", step)
```

For this to work, callers must persist the `prediction_id` externally. The `on_submit` callback in `RunnableConfig` fires right after `submit()` returns, giving callers a chance to save the ID:

```python
config = {"on_submit": lambda step_id, pred_id: save_to_db(step_id, pred_id)}
result = provider.invoke(step, config)
```

## 15. Advanced: progress callbacks

The base class fires `ProgressEvent` callbacks at status transitions (submitted, processing, succeeded, failed). Callers opt in via `RunnableConfig`:

```python
from genblaze_core.providers.progress import ProgressEvent

def on_progress(event: ProgressEvent):
    print(f"{event.provider}: {event.status} ({event.elapsed_sec:.1f}s)")

config = {"on_progress": on_progress}
result = provider.invoke(step, config)
```

`SyncProvider` subclasses get this for free. `BaseProvider` subclasses get it automatically during polling. No provider code is needed — the lifecycle orchestration handles it.

## 16. Write tests

Use the compliance test harness for automatic coverage, then add provider-specific tests:

```python
from genblaze_core.testing import ProviderComplianceTests

class TestMyProviderCompliance(ProviderComplianceTests):
    def make_provider(self):
        # Return a provider with mocked/faked API client
        provider = MyProvider(api_key="test")
        provider._client = FakeClient()
        return provider

# Add provider-specific tests for error mapping, edge cases, etc.
def test_my_specific_error_handling():
    ...
```

The compliance harness covers 15 tests: name uniqueness, lifecycle methods, invoke success, timestamps, asset URL validation, media types, capabilities type, audio metadata, chain input security, normalize_params idempotency, and cost tracking (soft check).

## 17. Export and install

`genblaze_myprovider/__init__.py`:
```python
from genblaze_myprovider.provider import MyProvider
__all__ = ["MyProvider"]
```

Install in dev mode:
```bash
cd libs/connectors/myprovider
pip install -e ".[dev]"
```

## Checklist

- [ ] Package at `libs/connectors/myprovider/`
- [ ] `pyproject.toml` with `genblaze.providers` entry point
- [ ] Subclass `SyncProvider` (or `BaseProvider` for polling APIs)
- [ ] `super().__init__()` called in constructor
- [ ] `get_capabilities()` declares supported modalities, inputs, models
- [ ] `normalize_params()` maps standard names (duration, resolution, aspect_ratio)
- [ ] `validate_asset_url()` called on all output asset URLs
- [ ] `validate_chain_input_url()` called on all `step.inputs` URLs (if `accepts_chain_input=True`)
- [ ] `_errors.py` module with `map_*_error(exc) -> ProviderErrorCode`
- [ ] Errors raised as `ProviderError` with explicit `error_code`
- [ ] `AudioMetadata`/`VideoMetadata` populated on assets
- [ ] `create_registry()` returns a `ModelRegistry` with per-model `pricing` strategies
- [ ] Poll result cached in `poll()`, consumed in `fetch_output()` (BaseProvider only)
- [ ] No API tokens in `provider_payload`
- [ ] Compliance tests pass (`ProviderComplianceTests` subclass)
- [ ] Provider-specific tests for error mapping
- [ ] `make test` passes from repo root
- [ ] `make lint` passes
