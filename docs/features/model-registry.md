<!-- last_verified: 2026-04-23 -->
# Model Registry

Unified, declarative surface for per-model configuration across every genblaze provider connector. Users can add new models, override pricing, and customize parameter handling at runtime without editing connector code.

## Why

Before the registry, each connector hard-coded its pricing dict (`_PRICING`), model list (`_MODELS`), and parameter forwarding rules (`forward_keys`). Users couldn't override pricing, couldn't register unreleased models, and couldn't portably express "16:9" across connectors with different native names (`ratio` vs `aspect_ratio`).

Now every connector consumes a `ModelRegistry` that describes models as `ModelSpec` data. Providers become thin HTTP adapters.

## Quickstart

### Override pricing on a known model

```python
from genblaze_core.providers import per_unit
from genblaze_openai import DalleProvider

reg = DalleProvider.models_default().fork()
reg.register_pricing("dall-e-3", per_unit(0.050))  # your volume rate
provider = DalleProvider(models=reg)
```

### Register a newly released model

```python
from genblaze_core.providers import ModelSpec, EnumSchema, IntSchema, per_unit
from genblaze_gmicloud import GMICloudVideoProvider

reg = GMICloudVideoProvider.models_default().fork()
reg.register(
    ModelSpec(
        model_id="new-video-model-v1",
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

### Custom cost formula

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

### `ModelSpec`

Frozen, slotted dataclass. Every field except `model_id` is optional — an empty spec is a permissive pass-through.

| Field | Purpose |
|---|---|
| `model_id` | Native model identifier (the registry key) |
| `aliases` | Alternate names that resolve to this spec (e.g. `chatgpt-image-latest` → `gpt-image-2`) |
| `modality` | `Modality.IMAGE` / `VIDEO` / `AUDIO` — informational |
| `pricing` | `PricingStrategy` callable returning USD cost |
| `param_aliases` | 1:1 rename (canonical → native), e.g. `{"aspect_ratio": "ratio"}` |
| `param_transformer` | Many-to-one rewrite (e.g. `(resolution, aspect_ratio) → size`) |
| `param_coercers` | Per-key type coercion, e.g. `{"duration": str, "sound": _bool_to_on_off}` |
| `param_schemas` | Declarative validation (`IntSchema`, `EnumSchema`, `StringSchema`, `BoolSchema`, `FloatSchema`, `ArraySchema`) |
| `param_defaults` | Filled when user didn't supply. User values win. |
| `param_required` | Keys that must be present after defaults are applied |
| `param_allowlist` | If set, only these keys are forwarded. `None` = pass everything (Replicate-style). |
| `param_constraints` | Cross-field rules (`requires_together`, `mutually_exclusive`, `required_one_of`, `implies`) |
| `input_mapping` | Routes `step.inputs` into native param names (`route_images`, `route_audio`, `route_by_media_type`, `route_keyframes`) |
| `extras` | Provider-specific escape hatch (not interpreted by the pipeline) |

### `ModelRegistry`

Layered store. Lookup order: user overrides → package defaults → fallback spec.

| Method | Use |
|---|---|
| `register(spec, override=True)` | Add or replace a spec |
| `register_pricing(model_id, strategy)` | Override only pricing (keeps other fields) |
| `extend(specs)` | Bulk register |
| `fork()` | Copy-on-write clone — per-instance overrides without mutating the parent |
| `get(model_id)` | Never returns None; falls back to alias then to fallback spec |
| `known()` | All registered model IDs |
| `prepare_payload(step)` | Run the 9-stage pipeline (see below) |

Reads are lockless (atomic dict reads under GIL); writes take an `RLock`. Safe across threads.

### Parameter pipeline

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

### `PricingStrategy`

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
run.step(provider="gmicloud", model="Kling-Text2Video-V1.6-Pro",
         aspect_ratio="16:9", duration=10)
run.step(provider="runway", model="gen4_turbo",
         aspect_ratio="16:9", duration=10)
```

Native names always win when both are supplied.

## Three levels of user customization

```python
# Level 1 — global (simple scripts)
from genblaze_gmicloud.models.video import build_video_registry
# mutate the package-default registry — affects all instances

# Level 2 — per-instance fork (recommended default — no leakage)
reg = GMICloudVideoProvider.models_default().fork()
reg.register_pricing("Kling-Text2Video-V1.6-Pro", per_unit(0.05))
provider = GMICloudVideoProvider(models=reg)

# Level 3 — custom registry from scratch
my_registry = ModelRegistry(
    defaults={"my-model": ModelSpec(model_id="my-model", pricing=per_unit(0.01))},
)
provider = GMICloudVideoProvider(models=my_registry)
```

## Performance

- `get(model_id)`: ~80 ns (two dict lookups)
- Full `prepare_payload` at 10 params: ~6 µs
- `PricingContext` construction + strategy: <1 µs
- Memory per spec: ~600 B; 50-model registry ≈ 30 KB resident
- Zero allocations in steady-state read path

Against 50–500 ms HTTP round-trips this is <0.01% overhead — and net perf is better than the pre-registry state because 12 bespoke `_track_cost` paths collapsed into one shared method.

## Thread safety

- Specs are frozen and slotted — safe to share
- Registry writes: `RLock`-protected
- Registry reads: lockless (CPython dict reads are atomic)
- Per-instance `fork()` is copy-on-write — no contention across threads using different forks

## Related

- Canonical params: see `libs/core/genblaze_core/providers/canonical_params.py`
- Pricing helpers: `libs/core/genblaze_core/providers/pricing.py`
- Input routers: `libs/core/genblaze_core/providers/input_mapping.py`
- Constraints: `libs/core/genblaze_core/providers/constraints.py`
- Reference migrations: GMICloud (`libs/connectors/gmicloud/genblaze_gmicloud/models/*.py`), DALL-E (`libs/connectors/openai/genblaze_openai/dalle.py`)
