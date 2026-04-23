<!-- last_verified: 2026-04-23 -->
# Model Registry v3 — unified provider config surface

Resolves `framework-dx-recommendations.md` item #7 (Dynamic cost config).

## Goal

Turn models, pricing, parameters, chain-input routing, and cross-field rules into **declarative data** shared across all 12 provider connectors. Users can register new models, override pricing, and customize parameter handling at runtime without editing connector code.

## Problem

Today each connector hard-codes its `_PRICING` dict, `_MODELS` set, and inline `forward_keys` tuple. Users cannot override pricing (item #7). Param handling is duplicated 12 ways. Adding a newly-released model requires a connector release.

## Design

### 1. Core primitives (new)

File layout under `libs/core/genblaze_core/providers/`:

| File | Purpose | LOC |
|---|---|---|
| `spec.py` | `ModelSpec`, `ParamSchema` + variants (Int/Enum/String/Bool/Array) | ~250 |
| `pricing.py` | `PricingContext`, `PricingStrategy` protocol, packaged helpers | ~200 |
| `input_mapping.py` | `route_images`, `route_audio`, `route_by_media_type`, `route_keyframes`, `chain_routers` | ~150 |
| `constraints.py` | `requires_together`, `mutually_exclusive`, `required_one_of` | ~60 |
| `model_registry.py` | `ModelRegistry` (layered, copy-on-write, thread-safe) | ~180 |
| `canonical/params.py` (new module) | Canonical name + value constants | ~80 |

Tests: one file per module under `libs/core/tests/unit/test_model_registry_*.py`. ~600 LOC.

### 2. `ModelSpec` shape

```python
@dataclass(frozen=True, slots=True)
class ModelSpec:
    model_id: str
    aliases: frozenset[str] = frozenset()
    modality: Modality | None = None
    pricing: PricingStrategy | None = None
    param_aliases: Mapping[str, str] = field(default_factory=dict)
    param_transformer: Callable[[dict], dict] | None = None
    param_coercers: Mapping[str, Callable[[Any], Any]] = field(default_factory=dict)
    param_schemas: Mapping[str, ParamSchema] = field(default_factory=dict)
    param_defaults: Mapping[str, Any] = field(default_factory=dict)
    param_required: frozenset[str] = frozenset()
    param_allowlist: frozenset[str] | None = None
    param_constraints: tuple[Callable[[dict], None], ...] = ()
    input_mapping: Callable[[list[Asset]], dict[str, Any]] | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)
```

### 3. `ModelRegistry` surface

```python
class ModelRegistry:
    def __init__(self, defaults: Mapping[str, ModelSpec], fallback: ModelSpec): ...
    def register(self, spec: ModelSpec, *, override: bool = False) -> None
    def register_pricing(self, model_id: str, pricing: PricingStrategy) -> None
    def get(self, model_id: str) -> ModelSpec  # never None
    def fork(self) -> ModelRegistry
    def extend(self, other: Mapping[str, ModelSpec]) -> None
    def known(self) -> list[str]
    def filter_params(self, model_id: str, params: dict) -> dict
    def prepare_payload(self, step: Step) -> dict[str, Any]
```

Lookup: user layer → defaults → fallback. O(1) reads (two dict probes). Writes lock.

### 4. Pipeline (in `prepare_payload`)

```
user params
 → aliases (canonical → native, 1:1)
 → param_transformer (many-to-one rewrites; Sora resolution+aspect_ratio → size)
 → input_mapping(step.inputs) merged (user wins)
 → coercers (type/value normalization)
 → defaults (fill missing)
 → schemas (validate type/enum/range)
 → required check
 → constraints (cross-field)
 → allowlist filter (if set)
```

### 5. Pricing: `PricingContext` + helpers

```python
@dataclass(frozen=True, slots=True)
class PricingContext:
    step: Step
    assets: Sequence[Asset]
    provider_payload: Mapping[str, Any]

    @cached_property
    def output_count(self) -> int
    @cached_property
    def output_duration_s(self) -> float | None  # sums Asset.duration_ms

PricingStrategy = Callable[[PricingContext], float | None]
```

Packaged helpers in `pricing.py`:

- `per_unit(rate)` — flat per output asset
- `per_input_chars(rate, per=1000)` — text length → cost
- `per_response_metric(extract)` — pulls a numeric from `provider_payload`
- `per_output_second(rate)` — reads `ctx.output_duration_s`
- `tiered(table, key)` — `(quality, size) → price` style
- `bucketed_by_duration(buckets)` — ElevenLabs SFX pattern
- `by_model_and_duration(table)` — Runway pattern
- `by_param(param_name, table)` — Decart pattern

All are pure, synchronous, O(1).

### 6. `BaseProvider` integration

```python
class BaseProvider(Runnable[Step, Step]):
    _models_cache: ClassVar[ModelRegistry | None] = None

    @classmethod
    def models_default(cls) -> ModelRegistry:
        if cls._models_cache is None:
            cls._models_cache = cls.create_registry()
        return cls._models_cache

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        """Override in subclasses. Default returns empty + permissive fallback."""
        return _EMPTY_REGISTRY

    def __init__(self, *, models: ModelRegistry | None = None) -> None:
        self._models = models or self.__class__.models_default()
        # ... existing init
```

After `fetch_output` returns, if `step.cost_usd is None`:
```python
spec = self._models.get(step.model)
if spec.pricing is not None:
    ctx = PricingContext(step=step, assets=step.assets, provider_payload=step.provider_payload)
    step.cost_usd = spec.pricing(ctx)
```

Providers that don't override `create_registry()` are unaffected. Zero breakage.

### 7. Canonical params (user-facing portability)

`genblaze_core/canonical/params.py`:
```python
PROMPT = "prompt"; NEGATIVE_PROMPT = "negative_prompt"
SEED = "seed"; N = "n"
IMAGE = "image"; IMAGE_END = "image_end"; AUDIO = "audio"; VIDEO = "video"
DURATION = "duration"; ASPECT_RATIO = "aspect_ratio"
RESOLUTION = "resolution"; FPS = "fps"
VOICE = "voice"; OUTPUT_FORMAT = "output_format"; QUALITY = "quality"

ASPECT_RATIOS = frozenset({"1:1","16:9","9:16","4:3","3:4","21:9"})
RESOLUTIONS_TIERED = frozenset({"480p","720p","1080p","4k"})
```

## Migration plan

Each connector gets a `models.py` (or `models/` subpackage for multi-family providers) declaring specs. Provider `__init__` accepts `models=` kwarg. Inline `_PRICING`/`_MODELS`/`forward_keys` deleted.

| Connector | Files | Models | Diff |
|---|---|---|---|
| gmicloud | models/{video,image,audio}.py | ~16 + N + N | ~400 |
| openai-dalle | models.py | 6 | ~220 |
| openai-tts | models.py | 3 | ~60 |
| openai-sora | models.py | 2 | ~100 |
| google-imagen | models.py | 2 | ~60 |
| google-veo | models.py | 3 | ~120 |
| elevenlabs-tts | models.py | 4 | ~80 |
| elevenlabs-sfx | models.py | 1 | ~40 |
| lmnt | models.py | fallback | ~40 |
| luma | models.py | 2 | ~60 |
| runway | models.py | 2 | ~80 |
| stability-audio | models.py | 1 | ~40 |
| decart (image+video) | models/{image,video}.py | 10 | ~200 |
| replicate | models.py | fallback | ~40 |

Total connector migration: ~1500 LOC.

## Risk log

| Risk | Mitigation |
|---|---|
| Breaking existing tests | Keep `create_registry()` opt-in; connector tests assert same cost values post-migration |
| Thread safety on registry mutation | RLock on writes; reads lockless (CPython dict read is atomic) |
| User registers malicious callable | Code-trust boundary; same as subclassing today |
| Pricing regression (wrong number) | Golden-value tests per spec — assert a known `(params, assets) → cost` for each migrated model |
| `param_transformer` ordering confusion | Pipeline order documented; transformer runs after aliases, before coercers |
| Connectors that can't fit the model (observed: none) | Escape hatch: override `normalize_params()` still works |

## Rollback plan

The primitives are additive. If a migration turns out to be incorrect, revert the per-connector PR. Core primitives stay. `_models = _EMPTY_REGISTRY` means no pricing, no behavior change.

## Acceptance criteria

- `make test` green
- `make lint` green
- User can write:
  ```python
  reg = ReplicateProvider.models_default().fork()
  reg.register_pricing("*", per_response_metric(lambda ctx: ctx.provider_payload["replicate"]["predict_time"] * 0.000150))
  provider = ReplicateProvider(models=reg)
  ```
  …and the cost override takes effect.
- `docs/features/model-registry.md` covers all three user levels (global, fork, from_dict)
- Tech-debt tracker item #7 flipped to "Resolved Since Last Review"

## Out of scope

- YAML config loader (use `from_dict` directly; users load YAML themselves)
- Async pricing (pricing must be pure/cheap)
- Multi-step workflow composition (pipeline layer's job)
- Voice catalog fetching (LMNT / ElevenLabs — voice is a param value, server validates)
- C2PA / webhook / resume (tracked elsewhere)

## Execution order (single session, main)

1. Land core primitives + tests
2. Wire BaseProvider (additive)
3. Migrate GMICloud (reference — hardest)
4. Migrate DALL-E (reference — diverse pricing)
5. Migrate remaining 10 connectors
6. Docs (`model-registry.md`, `canonical-params.md`, ARCHITECTURE.md update, tech-debt flip)
7. `make test` + `make lint` — all green before commit

Commit structure: one commit per logical unit for reviewability (core, base, each connector batch, docs).
