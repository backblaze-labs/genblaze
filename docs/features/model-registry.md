<!-- last_verified: 2026-07-15 -->
# Model Registry

Unified, declarative surface for per-model configuration across every
genblaze provider connector. Users can add new models, override pricing,
register new pattern families, and customize parameter handling at
runtime without editing connector code.

## Why

Before the registry, each connector hard-coded its pricing dict
(`_PRICING`), model list (`_MODELS`), and parameter forwarding rules
(`forward_keys`). Users couldn't override pricing, couldn't register
unreleased models, and couldn't portably express "16:9" across connectors
with different native names (`ratio` vs `aspect_ratio`).

In 0.3.0 the registry took its second step: connectors no longer ship
slug lists at all. They ship **`ModelFamily`** rules — pattern-keyed
parameter-shape templates. A new vendor model that fits an existing
family pattern works the day it ships upstream, with no SDK release
required. Pricing tables moved to
[`docs/reference/pricing-recipes.md`](../reference/pricing-recipes.md);
copy what you need.

## At a glance — three units

| Unit | Role |
|---|---|
| [`ModelFamily`](#modelfamily) | **Primary.** A regex + spec_template that matches every slug in a vendor model line (e.g. `^veo-` covers all Google Veo variants). Authoritative for new slugs that fit the pattern. |
| [`ModelSpec`](#modelspec) | Per-slug override or the body of a family's `spec_template`. Carries pricing, param contracts, schemas, allowlist, input mapping. |
| [`ModelRegistry`](#modelregistry) | The layered store that holds families and user specs and resolves a slug at runtime. |

## Quickstart

### Override pricing on a known model

```python
from genblaze_core.providers import per_unit
from genblaze_openai import DalleProvider

reg = DalleProvider.models_default().fork()
reg.register_pricing("dall-e-3", per_unit(0.050))   # your volume rate
provider = DalleProvider(models=reg)
```

### Register a newly released model that fits no shipped family

```python
from genblaze_core.providers import EnumSchema, IntSchema, ModelSpec, per_unit
from genblaze_gmicloud import GMICloudVideoProvider

reg = GMICloudVideoProvider.models_default().fork()
reg.register(
    ModelSpec(
        model_id="brand-new-vendor-model-v1",
        pricing=per_unit(0.25),
        param_schemas={
            "duration": IntSchema(min=1, max=30),
            "aspect_ratio": EnumSchema(frozenset({"16:9", "9:16", "1:1"})),
        },
        param_required=frozenset({"prompt"}),
        param_allowlist=frozenset({"prompt", "duration", "aspect_ratio"}),
        extras={"envelope_key": "payload"},
    )
)
provider = GMICloudVideoProvider(models=reg)
```

### Register a whole family at once

```python
import re
from genblaze_core.providers import ModelFamily, ModelSpec
from genblaze_core.models.enums import Modality
from genblaze_gmicloud import GMICloudVideoProvider

reg = GMICloudVideoProvider.models_default().fork()
reg.register_family(
    ModelFamily(
        name="my-private-line",
        pattern=re.compile(r"^private-vid-"),
        spec_template=ModelSpec(model_id="*", modality=Modality.VIDEO),
        description="Private-preview models from my vendor.",
        example_slugs=("private-vid-2025-q1",),
    )
)
provider = GMICloudVideoProvider(models=reg)
```

### Custom cost formula via `per_response_metric`

```python
from genblaze_core.providers import per_response_metric
from genblaze_replicate import ReplicateProvider

reg = ReplicateProvider.models_default().fork()
reg.register_pricing(
    "black-forest-labs/flux-schnell",
    per_response_metric(
        lambda ctx: ctx.provider_payload.get("replicate", {}).get("predict_time", 0) * 1.5e-4
    ),
)
provider = ReplicateProvider(models=reg)
```

## Concepts

### `ModelFamily`

Frozen, slotted dataclass. Pattern-keyed param-shape rule. Every slug
matching `pattern` resolves to a copy of `spec_template` with `model_id`
substituted to the slug — slugs are not stored on the family.

| Field | Purpose |
|---|---|
| `name` | Stable identifier for logs, metrics, error messages (e.g. `openai-tts`, `nvidia-cosmos-video2world`) |
| `pattern` | **Compiled** regex (`re.compile(r"^veo-")`); validated for catastrophic-backtracking at construction (`assert_safe`) |
| `spec_template` | A `ModelSpec` with `model_id="*"`. Carries param contracts, transformers, schemas, allowlist, input mapping, `extras`. **Pricing must be `None`** (per-slug pricing is user-registered via `register_pricing`) |
| `description` | One-line human-readable description for docs and error messages |
| `example_slugs` | Editorial slugs that match the family. Used for documentation, nearest-neighbor "Did you mean…?" suggestions on `NOT_FOUND`, and (for NATIVE providers) liveness gating in CI |
| `unstable_examples` | Slugs known or suspected dead — preserved through migration as a `frozenset` hint to maintainers and users until a `probe` is implemented and CI-passing |
| `probe` | Optional [`FamilyProbe`](#familyprobe) used by PARTIAL providers to confirm liveness. PARTIAL providers without a probe can only return `OK_PROVISIONAL` for family-matched slugs |
| `discovery_required` | If `True`, the permissive fallback alone is insufficient — preflight must consult discovery (or fail). Reserved for families whose users universally expect strict preflight semantics |

**Pattern style guide:** anchor with `^` whenever the family describes a
prefix-style line (`^veo-`); anchor `^...$` for a closed set; prefer
non-capturing groups `(?:...)`; avoid nested unbounded quantifiers (the
constructor rejects them via `pattern_safety.assert_safe()`).

`MAX_PROVIDER_FAMILIES = 32` caps the per-registry family count to keep
linear-scan resolution under the perf budget. Connectors hitting the cap
should consolidate patterns or split into multiple modality registries.

### `ModelSpec`

Frozen, slotted dataclass. Every field except `model_id` is optional —
an empty spec is a permissive pass-through. Used in two places: (1) as a
per-slug user override registered with `reg.register(spec)`; (2) as the
body of a `ModelFamily.spec_template` (with `model_id="*"`, substituted
at resolution time).

| Field | Purpose |
|---|---|
| `model_id` | Native model identifier (the registry key, or `"*"` on a family template) |
| `aliases` | Alternate names that resolve to this spec (e.g. `chatgpt-image-latest` → `gpt-image-2`) |
| `deprecated_aliases` | Old ids that still resolve but emit a `DeprecationWarning`. Use when a provider renames a slug; keep for one minor version before removal |
| `modality` | `Modality.IMAGE` / `VIDEO` / `AUDIO` — informational |
| `pricing` | `PricingStrategy` callable returning USD cost. **Always `None` on family `spec_template`s** — pricing is user-registered |
| `param_aliases` | 1:1 rename (canonical → native), e.g. `{"aspect_ratio": "ratio"}` |
| `param_transformer` | Many-to-one rewrite (e.g. `(resolution, aspect_ratio) → size`) |
| `param_coercers` | Per-key type coercion, e.g. `{"duration": str, "sound": _bool_to_on_off}` |
| `param_schemas` | Declarative validation (`IntSchema`, `EnumSchema`, `StringSchema`, `BoolSchema`, `FloatSchema`, `ArraySchema`) |
| `param_defaults` | Filled when user didn't supply. User values win |
| `param_required` | Keys that must be present after defaults are applied |
| `param_allowlist` | If set, only these keys are forwarded. `None` = pass everything (Replicate-style) |
| `param_constraints` | Cross-field rules (`requires_together`, `mutually_exclusive`, `required_one_of`, `implies`) |
| `input_mapping` | Routes `step.inputs` into native param names (`route_images`, `route_audio`, `route_by_media_type`, `route_keyframes`) |
| `extras` | Provider-specific escape hatch (not interpreted by the pipeline) |

### `ModelRegistry`

Layered store. Lookup order:

1. user spec (registered via `register(spec)`)
2. user family (registered via `register_family(family)`, prepended)
3. provider family (connector-shipped `provider_families=(...)`)
4. discovery cache (peek-only — NATIVE providers consult it)
5. fallback spec (permissive pass-through)

| Method | Use |
|---|---|
| `register(spec, override=True)` | Add or replace a spec in the user layer |
| `register_pricing(model_id, strategy)` | Override only pricing (keeps other fields) |
| `extend(specs)` | Bulk-register specs (single alias-index rebuild) |
| `register_family(family)` | Prepend a user-defined family to the resolution chain (highest priority) |
| `fork()` | Copy-on-write clone — per-instance overrides without mutating the parent. Forks the discovery cache too |
| `get(model_id)` | Never returns `None`; falls back to family → alias → fallback. Emits `DeprecationWarning` (once per slug per registry) when resolved via `deprecated_aliases` |
| `match_family(model_id)` | Returns the first matching `FamilyMatch` or `None`. User families take precedence over provider families |
| `validate(model_id, *, discovery_support=...)` | Returns a [`ValidationResult`](#validationresult) — the non-network, deterministic part of validation |
| `resolve_canonical(model_id)` | Returns the canonical slug the upstream API expects; passes caller input through when only the fallback matched |
| `has(model_id)` | True if the id maps to a non-fallback spec (user spec, alias, deprecated alias, or family pattern) |
| `known()` | All registered / discoverable model IDs (user specs + family `example_slugs` + discovery cache snapshot). **Documentation grade, not a contract** |
| `prepare_payload(step)` | Run the parameter pipeline (see below) |

Reads are lockless against the immutable provider-families tuple; writes
take an `RLock`. Safe across threads.

### `DiscoverySupport`

Per-provider declaration of upstream catalog API support. Drives the
outcomes `validate_model()` returns.

| Tier | Meaning | Example connectors |
|---|---|---|
| `NATIVE` | Authoritative `GET /models` (or equivalent) covering the provider's full surface. Family-matched slugs upgrade to `OK_AUTHORITATIVE` iff they appear in the live discovery cache | OpenAI (TTS / DALL-E / Sora), ElevenLabs TTS, Replicate, NVIDIA chat |
| `PARTIAL` | Catalog exists but doesn't enumerate every endpoint, or per-slug probing is the authoritative path. PARTIAL providers cannot return `OK_AUTHORITATIVE` from family-match alone — they need a `FamilyProbe` (or fall back to `OK_PROVISIONAL`) | NVIDIA generative endpoints, GMICloud, Google (Veo / Imagen) |
| `NONE` | No catalog API. Family match returns `OK_PROVISIONAL`. The user owns slug freshness; the SDK is honest that it cannot verify | Decart, Runway, Luma, Stability-Audio, ElevenLabs SFX |

Every provider class declares its tier as a class constant
(`discovery_support = DiscoverySupport.NATIVE`). The conformance harness
gates the declaration's presence.

### `FamilyProbe`

`PARTIAL` providers attach a probe callable to each `ModelFamily` so the
registry can confirm a slug is live without a discovery endpoint. The
canonical shapes:

- **Catalog endpoint:** `client.models.get(model=slug)` (Google) or HEAD on
  a model URL.
- **Invalid-payload trick:** POST a deliberately-empty body. `404` →
  slug is gone (`LiveProbeResult.DEAD`); `400` → slug exists, payload was
  garbage (`LiveProbeResult.LIVE`); transport error → `UNKNOWN` (caller
  downgrades to `OK_PROVISIONAL`). Used by NVIDIA generative endpoints
  and GMICloud.

Probe results are cached per-provider via a single-flight `threading.Event`,
TTL-bounded (`PROBE_CACHE_TTL_SECONDS=3600`), with FIFO eviction at
`PROBE_CACHE_MAX_ENTRIES=256`. Per-instance overrides are available via
`Provider(probe_cache_ttl=..., probe_cache_max_entries=...)`.

`FamilyProbe` is typed as `Callable[..., LiveProbeResult]` — the only
stable contract is "first positional is slug, return is
`LiveProbeResult`." Each connector chooses its probe's keyword shape
(`http: httpx.Client` for raw-HTTP probes; `client: <SdkClient>` for
SDK-based probes) and forwards the right transport via its
`_invoke_family_probe` hook.

### `ValidationResult`

`validate_model(slug)` returns a `ValidationResult` describing what the
SDK can say about a slug:

| Field | Type | Meaning |
|---|---|---|
| `outcome` | `ValidationOutcome` | `OK_AUTHORITATIVE`, `OK_PROVISIONAL`, `UNKNOWN_PERMISSIVE`, `NOT_FOUND` |
| `source` | `ValidationSource` | Where the verdict came from: `USER`, `FAMILY`, `DISCOVERY`, `PROBE`, `FALLBACK` |
| `detail` | `str \| None` | Human-readable context (e.g. `"discovery cache stale by 4h"`, `"known_unstable; verify with discover_models()"`) |
| `suggested_slugs` | `tuple[str, ...]` | Up to 3 nearest-neighbor matches when `outcome is NOT_FOUND` |

Outcome semantics:

- `OK_AUTHORITATIVE` — confirmed live (user-registered, in discovery
  cache, or successful probe). Step proceeds.
- `OK_PROVISIONAL` — slug looks plausible (matches a family pattern)
  but no authoritative confirmation. Includes the unstable-slug case:
  when a slug is in the family's `unstable_examples` set, the outcome
  is `OK_PROVISIONAL` and `detail` starts with `"known_unstable"`
  (today: `"known_unstable; verify with discover_models()"`). Step
  proceeds; one WARN log line per (provider, slug) pair per Pipeline
  instance.
- `UNKNOWN_PERMISSIVE` — no family match, no user spec, not in
  discovery, but the connector's permissive fallback spec applies.
  The slug goes through to upstream, which decides. `source` is
  `ValidationSource.FALLBACK`. One WARN log line per pipeline.
- `NOT_FOUND` — slug is conclusively unknown and no fallback applies
  (e.g. NATIVE provider with a fresh discovery cache that doesn't
  list the slug). Step is skipped at preflight unless
  `pipeline.preflight(False)` opts out — `Pipeline(preflight=False)`
  is the constructor equivalent.

> **Note on `is_ok`.** `ValidationResult.is_ok` returns `True` only
> for `OK_AUTHORITATIVE` and `OK_PROVISIONAL`. An `UNKNOWN_PERMISSIVE`
> result will *also* let the step proceed (the fallback applies), but
> `is_ok` returns `False` because the SDK can't confirm liveness. If
> you're branching on the result, prefer
> `outcome in {OK_AUTHORITATIVE, OK_PROVISIONAL, UNKNOWN_PERMISSIVE}`
> when "will this step run" is what you actually care about.

### `validate_model()` — checking a slug

```python
from genblaze_core.providers import ValidationOutcome
from genblaze_openai import OpenAITTSProvider

provider = OpenAITTSProvider(api_key="...")

# Cheap, deterministic — uses cache + family resolution.
result = provider.validate_model("tts-1")
print(result.outcome, result.source, result.detail)

# Force a discovery refresh — bypass the cache, hit the upstream
# /models endpoint. Use sparingly.
result = provider.validate_model("just-shipped-model", refresh=True)
if result.outcome is ValidationOutcome.NOT_FOUND:
    for hint in result.suggested_slugs:
        print(f"Did you mean {hint!r}?")
```

`refresh=True` re-runs the connector's discovery fetcher and updates the
cache. The next non-refresh call sees the new snapshot. Single-flight
guarantees only one fetch is in flight per registry at a time, even
under high concurrency.

## Parameter pipeline

Runs inside `BaseProvider.prepare_payload(step)`:

1. Merge top-level Step fields (`prompt`, `negative_prompt`, `seed`) with `step.params`
2. `normalize_params()` hook (provider-level escape hatch)
3. **`param_aliases`** — canonical → native (1:1), non-destructive when native already set
4. **`param_transformer`** — many-to-one / arbitrary rewrites
5. **`input_mapping`** — chain inputs merged; user params win
6. **`param_coercers`** — per-key type/value coercion
7. **`param_defaults`** — fill missing
8. **`param_schemas`** — validate type/enum/range
9. **`param_required`** — after defaults
10. **`param_constraints`** — cross-field rules
11. **`param_allowlist`** — filter (or raise under `strict_params=True`)

## `PricingStrategy`

```python
PricingStrategy = Callable[[PricingContext], float | None]

@dataclass(frozen=True, slots=True)
class PricingContext:
    step: Step
    assets: Sequence[Asset]
    provider_payload: Mapping[str, Any]
    output_count: int          # len(assets)
    output_duration_s: float | None   # sum of asset durations
```

Packaged strategies cover the common shapes:

| Helper | Shape |
|---|---|
| `per_unit(rate)` | Flat per output asset |
| `per_input_chars(rate, per=1000)` | USD per N characters of prompt |
| `per_output_second(rate)` | USD per second of output duration |
| `per_response_metric(extract)` | Pull a number from `provider_payload` and return it |
| `tiered(table, key)` | Table lookup keyed by `(quality, size)` etc. |
| `bucketed_by_duration(buckets)` | `((lo, hi), price)` bucketed by output duration |
| `by_param(param, table)` | Single-param lookup, e.g. `{"480p": 0.04, "720p": 0.08}` |
| `by_model_and_param(param, table)` | Two-key lookup `(model, param_value)` |
| `first_match(*strategies)` | First non-None wins |

The SDK ships zero hardcoded prices as of 0.3.0 — see
[`docs/reference/pricing-recipes.md`](../reference/pricing-recipes.md) for
copy-pasteable per-provider rate tables.

## Canonical parameter vocabulary

A tight set of portable names in `genblaze_core.providers.canonical_params`:

```python
PROMPT, NEGATIVE_PROMPT, SEED, N
IMAGE, IMAGE_END, AUDIO, VIDEO
DURATION, ASPECT_RATIO, RESOLUTION, FPS
VOICE, OUTPUT_FORMAT, QUALITY
```

Plus value vocabularies: `ASPECT_RATIOS = {"1:1","16:9","9:16", ...}` and `RESOLUTIONS_TIERED = {"480p","720p","1080p","4k"}`.

A pipeline written with canonical names is portable across providers that alias them to native equivalents:

```python
# Works on any video provider whose spec aliases aspect_ratio appropriately
run.step(provider="gmicloud", model="kling-text2video-v1.6-pro",
         aspect_ratio="16:9", duration=10)
run.step(provider="runway", model="gen4_turbo",
         aspect_ratio="16:9", duration=10)
```

Native names always win when both are supplied.

## Three levels of user customization

```python
# Level 1 — global mutation (simple scripts, one-off tools)
from genblaze_gmicloud.models.video import build_video_registry
# build_video_registry() returns the package-default registry; mutating
# it affects every instance constructed from then on. Use sparingly —
# convenient for scripts, leaky for libraries.

# Level 2 — per-instance fork (recommended default — no leakage)
from genblaze_core.providers import per_unit
from genblaze_gmicloud import GMICloudVideoProvider
reg = GMICloudVideoProvider.models_default().fork()
reg.register_pricing("kling-text2video-v1.6-pro", per_unit(0.05))
provider = GMICloudVideoProvider(models=reg)

# Level 3 — custom registry from scratch (test harnesses, custom routing)
from genblaze_core.providers import ModelRegistry, ModelSpec
my_registry = ModelRegistry()
my_registry.register(ModelSpec(model_id="my-model", pricing=per_unit(0.01)))
provider = GMICloudVideoProvider(models=my_registry)
```

For multi-tenant deployments where each tenant needs an isolated probe
cache and discovery snapshot, use `fork()` per tenant. The fork shares
the discovery fetcher closure (same auth, same upstream) but isolates
the cache state — a refresh on one tenant's clone doesn't blow out the
others' warm caches.

## Renaming a model slug safely

When an upstream provider renames a model (common when vendor APIs move
from PascalCase docs to lowercase live slugs), don't break existing
callers:

```python
ModelSpec(
    model_id="seedream-5.0-lite",                         # new canonical slug
    deprecated_aliases=frozenset({"Seedream-5.0-Lite"}),  # old id, kept for 1 minor
    pricing=per_unit(0.035),
    # ... other fields as needed
)
```

Existing `step.model="Seedream-5.0-Lite"` calls resolve to the new spec, a single `DeprecationWarning` fires (once per slug per registry), and `resolve_canonical()` sends `seedream-5.0-lite` on the wire. Drop the alias after one minor version bump.

## Performance

- `get(model_id)` (user-spec hit): ~80 ns (single dict lookup)
- `match_family(model_id)` (32 families, miss): <50 µs (linear scan)
- `match_family()` (adversarial input, 32 families): <100 µs (`pattern_safety` rejects unsafe patterns at construction; the static heuristic always runs, plus an additional `google-re2` compile check when the `re2`/`dev` extras are installed — re2 is not a substitute for the heuristic, since runtime matching always uses stdlib `re`). Enforced by `libs/core/tests/perf/test_registry_perf.py`.
- Full `prepare_payload` at 10 params: ~6 µs
- `PricingContext` construction + strategy: <1 µs
- `validate_model()` (no fetch, family-cached): <50 µs
- `validate_model(refresh=True)`: bound by upstream discovery latency (~50–500 ms typical)
- Memory per registry: <100 KB (specs + compiled patterns + discovery cache)
- Zero allocations in steady-state read path

## Thread safety

- Specs are frozen and slotted — safe to share
- Family `pattern` is precompiled — never re-compiled at lookup time
- Provider-families tuple is immutable post-construction — lock-free reads
- User-family list and discovery cache: `RLock`-guarded with snapshot reads
- Probe cache: single-flight via `threading.Event` (one fetch per slug under contention)
- Per-instance `fork()` is copy-on-write — no contention across threads using different forks

## Migrating from 0.2.x

See [`docs/guides/migrating-to-0.3.md`](../guides/migrating-to-0.3.md)
for before/after examples, validation-outcome handling, and the
`NOT_FOUND` decision tree.

## Related

- Family + DiscoverySupport implementation: `libs/core/genblaze_core/providers/family.py`
- Validation outcomes: `libs/core/genblaze_core/providers/validation.py`
- Discovery cache: `libs/core/genblaze_core/providers/discovery.py`
- Canonical params: `libs/core/genblaze_core/providers/canonical_params.py`
- Pricing helpers: `libs/core/genblaze_core/providers/pricing.py`
- Input routers: `libs/core/genblaze_core/providers/input_mapping.py`
- Constraints: `libs/core/genblaze_core/providers/constraints.py`
- Pricing recipes (per-provider): [`docs/reference/pricing-recipes.md`](../reference/pricing-recipes.md)
- Reference family migrations: GMICloud (`libs/connectors/gmicloud/genblaze_gmicloud/models/*.py`), OpenAI (`libs/connectors/openai/genblaze_openai/{tts,dalle,provider}.py`), Google (`libs/connectors/google/genblaze_google/_families.py`)
