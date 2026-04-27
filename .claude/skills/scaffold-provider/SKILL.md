---
name: scaffold-provider
description: Scaffold a new genblaze provider connector package with all required files, tests, and entry points by analyzing existing connectors for conventions.
argument-hint: <provider-name> <modality: image|video|audio|music> [sync|polling]
allowed-tools: Read Grep Glob Bash Edit Write
---

# Scaffold a New Provider Connector

Generate a complete provider connector package for: **$ARGUMENTS**

Parse the arguments:
- `$0` — provider name (e.g. `fal`, `hedra`, `picovoice`); used for the package slug
- `$1` — modality: `image`, `video`, `audio`, or `music`
- `$2` — API style: `sync` (default — most providers) or `polling`

If any argument is missing or ambiguous, ask the user before proceeding. Do **not** invent a name or modality.

## Phase 1 — Learn current conventions

Read these files in order. **Skipping this phase produces drift.** The codebase is the source of truth; this skill must reflect what's already shipping.

1. **Base classes** — `libs/core/genblaze_core/providers/base.py`
   - `BaseProvider` (polling lifecycle: `submit` / `poll` / `fetch_output`)
   - `SyncProvider` (single `generate` method)
   - `validate_asset_url`, `validate_chain_input_url`, `classify_api_error`
2. **Contributor guide** — `docs/guides/new-provider.md` (the canonical checklist; this skill scaffolds what the guide describes)
3. **Two existing connectors** matching the modality. Use Glob on `libs/connectors/*/` and pick representative examples:
   - **sync image/audio reference** — `libs/connectors/elevenlabs/`, `libs/connectors/openai/` (DALL-E, TTS)
   - **polling video reference** — `libs/connectors/replicate/`, `libs/connectors/luma/`
   - For each, read: `pyproject.toml`, `genblaze_*/__init__.py`, `genblaze_*/provider.py`, `genblaze_*/_errors.py`, and the primary test file under `tests/`.

Extract these patterns from what you read (do not assume — verify):

| Pattern | Where to look |
|---------|---------------|
| Package + module slugs (`genblaze-{name}` / `genblaze_{name}`) | `pyproject.toml` `[project] name`, `[tool.hatch.build.targets.wheel] packages` |
| Class naming `{Name}Provider` (or capability-suffixed: `OpenAITTSProvider`) | `provider.py`, `__init__.py` |
| Entry-point format under `genblaze.providers` | `pyproject.toml` `[project.entry-points."genblaze.providers"]` |
| Error mapper shape — delegate to `classify_api_error` vs SDK-specific checks | `_errors.py` (compare LMNT minimal vs ElevenLabs verbose) |
| Capability declaration via `get_capabilities()` | `provider.py` |
| Standard-name → native param mapping in `normalize_params()` | `provider.py` |
| Per-model `ModelSpec` and `create_registry()` classmethod | `provider.py` |
| Pricing strategy choice from `genblaze_core.providers.pricing` | `provider.py` (look for `per_unit`, `per_input_chars`, `per_output_second`, `tiered`, `bucketed_by_duration`, `by_param`, `by_model_and_param`, `per_response_metric`) |
| Compliance harness wiring | `tests/test_*.py` (subclass of `ProviderComplianceTests`) |
| Standardization hooks (optional) | `preflight_auth`, `probe_model` — see `libs/connectors/gmicloud/genblaze_gmicloud/_base.py` |

## Phase 2 — Generate the scaffold

Create `libs/connectors/{name}/` with the file layout below. Match the **most recent** connectors' style — fields, imports, ordering — rather than this skill's prose. When the codebase and this skill disagree, the codebase wins.

### `pyproject.toml`

Mirror an existing connector's structure exactly. Required fields:

- `[project]` — `name = "genblaze-{name}"`, version `"0.1.0"`, the standard `authors`, `readme`, `requires-python = ">=3.11"`, `license = "MIT"`, classifiers, and `dependencies = ["genblaze-core>=0.2.0,<0.3", "<sdk>>=<min-version>"]`
- `[project.urls]` — Homepage / Documentation / Repository / Issues (copy from a sibling)
- `[project.optional-dependencies]` — `dev = ["pytest>=7.0"]`
- `[project.entry-points."genblaze.providers"]` — `{name} = "genblaze_{name}:{Name}Provider"` (one line per exported provider class)
- `[tool.hatch.build.targets.wheel]` — `packages = ["genblaze_{name}"]`
- `[tool.pytest.ini_options]` — `testpaths = ["tests"]`

### `genblaze_{name}/__init__.py`

Single line of imports plus `__all__`. Pattern:

```python
"""{Name} provider adapter for genblaze."""

from genblaze_{name}.provider import {Name}Provider

__all__ = ["{Name}Provider"]
```

### `genblaze_{name}/provider.py`

- Module docstring with API style, registry rationale, and a docs URL
- Subclass `SyncProvider` (sync) or `BaseProvider` (polling)
- `name = "{name}"` class attribute (lowercase slug, must be unique across connectors)
- `__init__(self, api_key: str | None = None, *, models: ModelRegistry | None = None)` — pass `models=models` to `super().__init__()` (lets users override the registry without subclassing)
- Lazy SDK import in `_get_client()` — raises `ProviderError` with a clear install hint on `ImportError`
- `@classmethod create_registry(cls) -> ModelRegistry:` returning per-model `ModelSpec` defaults with a `pricing` strategy from `genblaze_core.providers.pricing`. Use `EMPTY_REGISTRY` only when the provider truly has no enumerable models (Replicate-style)
- `get_capabilities()` returning `ProviderCapabilities` with the modality, supported inputs, and `accepts_chain_input=True` if the provider reads `step.inputs`
- `normalize_params()` mapping standard names (`duration`, `resolution`, `aspect_ratio`, `voice_id`, `output_format`) to the SDK's native keys with idempotent guards (`if "x" in p and "native_x" not in p:`)
- `generate()` (sync) or `submit()` / `poll()` / `fetch_output()` (polling) with explicit `# TODO:` markers for the actual API call shape
- `validate_asset_url(url)` on every output URL before constructing `Asset`
- `validate_chain_input_url(asset.url)` on each `step.inputs[i]` when `accepts_chain_input=True`
- Typed metadata: `AudioMetadata` / `VideoMetadata` populated on assets per modality
- Error handling: catch SDK exceptions, raise `ProviderError(..., error_code=map_{name}_error(exc))`

### `genblaze_{name}/_errors.py`

Start with the **minimal** delegating shape. Add SDK-specific branches only if the SDK exposes typed exceptions or status codes that string-matching can't disambiguate:

```python
"""Shared {Name} error mapping — used by provider.py."""
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error


def map_{name}_error(exc: Exception) -> ProviderErrorCode:
    """Map a {Name} API exception to a ProviderErrorCode."""
    return classify_api_error(exc)
```

### `genblaze_{name}/py.typed`

Empty marker file (enables type-checker consumption per PEP 561).

### `tests/__init__.py`

Empty.

### `tests/test_{name}.py`

Use the existing test files as the template. Required tests:

- **Error mapping** — one `test_map_error_*` per `ProviderErrorCode` branch in `_errors.py`
- **Submit happy path** — mocks the SDK client, asserts the right call is made and a prediction ID / step is returned
- **Full lifecycle via `invoke()`** — mocks submit→poll→fetch (or generate), asserts `StepStatus.SUCCEEDED` and at least one asset with a valid URL + media type
- **API error wrapping** — mocked SDK raises; provider must raise `ProviderError` with the right `error_code`
- **`normalize_params` idempotency** — `normalize_params(normalize_params(p)) == normalize_params(p)`
- **Cost tracking** — successful invoke populates `step.cost_usd`
- **Compliance harness** — a class subclassing `ProviderComplianceTests` (from `genblaze_core.testing`) that returns the mocked provider from `make_provider()`. The harness contributes 15 contract tests covering identity, lifecycle, asset validation, capabilities, audio metadata, chain-input safety, normalize_params idempotency, and cost.

If the provider does not yet populate `cost_usd` (pricing formula pending), the compliance subclass must set `expects_cost = False` with a comment explaining the gap — this is the documented escape hatch, not silent skipping.

## Phase 3 — Validate

Run from the repo root unless noted:

1. `pip install -e "libs/connectors/{name}[dev]"` — installs the new package
2. `pytest libs/connectors/{name}/tests/ -v` — connector tests pass with mocks
3. `make lint` — formatting + Ruff clean
4. `make typecheck` — mypy clean (the `py.typed` marker matters here)

Fix any failures **before** reporting. Do not skip a test or weaken an assertion to make it pass — surface the issue instead.

## Phase 4 — Report

Tell the user exactly:

- Which files were created (paths relative to repo root)
- Which `# TODO:` markers remain — actual SDK call shape, model IDs, pricing rates, dependency version pin
- Suggested follow-ups they own:
  - Run `make test` to gate the full suite
  - Add the connector to `docs/features/provider-system.md` (the "Cost Tracking" pricing-shape table) when pricing is wired
  - Consider opting into `preflight_auth` (cheap creds check) and `probe_model` (CI drift detection) — see `libs/connectors/gmicloud/genblaze_gmicloud/_base.py` for the reference implementation
  - Tune retry behavior with `RetryPolicy` if the SDK has unusual transient-failure semantics
- Confirm whether you ran `make lint` / `make typecheck` and the result

Keep the report tight: bullets, no narration.
