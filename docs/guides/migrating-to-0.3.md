---
type: Library
title: Migrating to genblaze-core 0.3.0
description: Upgrade guide for genblaze-core 0.2.x callers.
status: stable
stale_after: 2027-05-16T00:00:00Z
owner: "@jeronimodeleon"
---
# Migrating to genblaze-core 0.3.0

> **Audience.** This guide is for callers of `genblaze-core` 0.2.x who are
> upgrading to 0.3.0. If you are writing a brand-new connector, read
> [`docs/guides/new-provider.md`](new-provider.md) instead.

---

## 30-second TL;DR

- **Most callers don't have to change anything.** If your code only uses the
  `Pipeline` / `Step` / `Run` API, your provider's `submit()` / `poll()` /
  `fetch_output()`, or `provider.invoke(step)`, you upgrade in place. The
  `Step.model` field still accepts the slug you've always passed.
- **`cost_usd` is `None` more often.** The SDK no longer ships hardcoded
  pricing tables. To get cost back, register a strategy from
  [`docs/reference/pricing-recipes.md`](../reference/pricing-recipes.md) — one
  copy-paste per provider.
- **`ModelRegistry(defaults={...})` is gone.** Use `register()` / `extend()`
  on the constructed registry instead.
- **Preflight validation (auto-run inside `Pipeline.run()`) may surface
  `NOT_FOUND` for slugs that pass on the wire today.** If that happens,
  register the slug or its family — see
  [Validation outcomes](#validation-outcomes--what-each-means) below.

---

## What's NOT changing

To keep the upgrade calibrated, here's what stays exactly the same:

- The shape of `Pipeline`, `Step`, `Run`, `Asset`, `ChatResponse`, the
  manifest, and every storage backend (S3 / B2).
- Provider classes, their constructors (apart from new optional
  `probe_cache_*` kwargs), and their public methods (`submit`, `poll`,
  `fetch_output`, `invoke`, `generate`, `estimate_cost`,
  `get_capabilities`).
- The `Step.model` slug you pass — every slug that worked in 0.2.x still
  routes correctly in 0.3.0.
- Param normalization, asset metadata, retry policy, error classification
  (`ProviderError` + `ProviderErrorCode`), and tracing hooks.
- `provider.models_default().fork()` — still the recommended per-instance
  customization pattern.

---

## What IS changing — the architecture in one paragraph

The SDK no longer ships a hardcoded slug-to-spec dictionary per connector.
Each connector now ships **`ModelFamily` rules** — pattern-keyed
parameter-shape templates. A slug like `veo-3.0-generate-001` matches the
`google-veo` family's regex (`^veo-`), inherits the family's
`spec_template`, and resolves without the slug ever appearing in SDK
source. Future `veo-N` slugs work the day they ship upstream — no SDK
release required. Each connector also declares its **`DiscoverySupport`**:
`NATIVE` (we can list models authoritatively), `PARTIAL` (we can probe
per-slug), or `NONE` (no upstream catalog — submit-time errors surface
mistakes). The pipeline's preflight phase reads this signal to decide
what `validate_model()` returns: `OK_AUTHORITATIVE`, `OK_PROVISIONAL`,
`UNKNOWN_PERMISSIVE`, or `NOT_FOUND`.

The full design trail is in
[`docs/exec-plans/completed/model-registry-decoupling.md`](../exec-plans/completed/model-registry-decoupling.md).

---

## Before / after — common patterns

### Pattern 1 — Override pricing on a known model

The most common 0.2.x recipe ("my volume rate is lower than the
list price") is unchanged in shape, but in 0.3.0 there is no list
price to override unless the user (you) registered one. So this is
now a one-step register, not a two-step override.

```python
# 0.2.x
from genblaze_core.providers import per_unit
from genblaze_openai import DalleProvider

reg = DalleProvider.models_default().fork()
reg.register_pricing("dall-e-3", per_unit(0.050))   # your volume rate
provider = DalleProvider(models=reg)
```

```python
# 0.3.0 — pricing is user-registered; the recipe is published
from genblaze_core.providers import tiered
from genblaze_openai import DalleProvider

# The (quality, size) → rate table for DALL-E 3 lives in
# docs/reference/pricing-recipes.md. Copy what fits your billing.
DALLE3_RATES = {
    ("standard", "1024x1024"): 0.040,
    ("standard", "1024x1792"): 0.080,
    ("hd", "1024x1024"): 0.080,
}
def image_key(ctx):
    p = ctx.step.params
    return (p.get("quality", "standard"), p.get("size", "1024x1024"))

reg = DalleProvider.models_default().fork()
reg.register_pricing("dall-e-3", tiered(DALLE3_RATES, key=image_key))
provider = DalleProvider(models=reg)
```

### Pattern 2 — Register a newly released model

Same shape — `register()` writes to the user layer. The difference is
that in 0.3.0 your registration is the *first* spec for that slug; the
SDK no longer ships a hardcoded one to override.

```python
# 0.3.0
from genblaze_core.providers import EnumSchema, IntSchema, ModelSpec, per_unit
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
    )
)
provider = GMICloudVideoProvider(models=reg)
```

If your slug fits an existing family pattern (e.g. `kling-text2video-v1.7`
matches `^kling-text2video-`), `provider.invoke(step)` already works
without any registration — the family's `spec_template` carries the
param shape. `register()` is for *overriding* the family-derived spec
or for adding a slug whose pattern doesn't exist yet.

### Pattern 3 — Fork a registry per-instance

Identical to 0.2.x. `fork()` carries forward families, user specs,
unstable-slug hints, and forks the discovery cache so per-instance
state doesn't leak across providers.

```python
reg = ReplicateProvider.models_default().fork()
reg.register_pricing("black-forest-labs/flux-schnell", ...)
provider = ReplicateProvider(models=reg)
```

### Pattern 4 — Build a registry from scratch

The dropped kwarg shows up here. **The constructor no longer accepts
`defaults={}`.** Use `register()` for one spec, `extend()` for many.

```python
# 0.2.x — TypeError in 0.3.0
my_registry = ModelRegistry(
    defaults={"my-model": ModelSpec(model_id="my-model", pricing=per_unit(0.01))},
)
```

```python
# 0.3.0
my_registry = ModelRegistry()
my_registry.register(ModelSpec(model_id="my-model", pricing=per_unit(0.01)))

# or, for several specs at once (one alias-index rebuild)
my_registry = ModelRegistry()
my_registry.extend([
    ModelSpec(model_id="my-model-a", pricing=per_unit(0.01)),
    ModelSpec(model_id="my-model-b", pricing=per_unit(0.02)),
])
```

If you only need pattern-keyed routing (no per-slug specs), use
`provider_families=`:

```python
from genblaze_core.providers import ModelFamily, ModelSpec, ModelRegistry
from genblaze_core.models.enums import Modality
import re

family = ModelFamily(
    name="my-vendor-line",
    pattern=re.compile(r"^my-vendor-"),
    spec_template=ModelSpec(model_id="*", modality=Modality.VIDEO),
    description="My vendor's video models.",
    example_slugs=("my-vendor-pro", "my-vendor-lite"),
)
reg = ModelRegistry(provider_families=(family,))
```

---

## Validation outcomes — what each means

Preflight validation runs **automatically inside `Pipeline.run()`** —
you don't call it directly. To check a single slug manually, use
`provider.validate_model(slug)`, which returns a `ValidationResult`
whose `outcome` is one of these four enum values (defined in
[`libs/core/genblaze_core/providers/validation.py`](../../libs/core/genblaze_core/providers/validation.py)):

| Outcome | What it means | Default behavior |
|---|---|---|
| `OK_AUTHORITATIVE` | Upstream catalog (NATIVE) or per-slug probe (PARTIAL) or user registration confirmed this slug. | Step proceeds. |
| `OK_PROVISIONAL` | The slug matches a family pattern but the SDK can't authoritatively confirm liveness (PARTIAL with no probe, NATIVE with stale cache, NONE provider). Includes the `known_unstable` case — slug listed in the family's `unstable_examples` set. | Step proceeds; one-per-Pipeline-instance WARN logged. |
| `UNKNOWN_PERMISSIVE` | No family match, no user spec, no discovery hit — but the connector's permissive fallback spec is still applied. The slug goes through to the upstream which decides. | Step proceeds; one-per-Pipeline-instance WARN logged. |
| `NOT_FOUND` | The slug is conclusively unknown and the registry refuses to apply a fallback (e.g. NATIVE provider with a fresh discovery cache that doesn't list the slug). | Step is skipped at preflight; `MODEL_ERROR` raised. |

The `ValidationResult` also carries a `source: ValidationSource`
(`USER`, `FAMILY`, `DISCOVERY`, `PROBE`, or `FALLBACK`) and a
human-readable `detail` string. For the unstable-slug case, the
detail starts with `"known_unstable"` and currently reads
`"known_unstable; verify with discover_models()"`. (A future release
may carry a per-family replacement-slug hint; today the detail just
flags the slug as unstable and points users to a discovery refresh.)

> **Note on `Pipeline.preflight()`.** This is a fluent setter on
> `Pipeline` (`pipeline.preflight(False)` returns the same Pipeline)
> that toggles whether the validation phase runs at all. It is
> **not** a function that returns `ValidationResult`s. Use
> `provider.validate_model()` for that.

---

## What to do when you hit `NOT_FOUND` for a slug you know exists

The 0.3.0 SDK is opinionated about preflight gates, so a `NOT_FOUND`
sometimes surprises users on slugs that work fine on the wire today.
Here is the decision order — try each in turn before reaching for the
last-resort opt-out.

### 1. Register the family — the slug doesn't match any shipped pattern

If your slug is from a vendor line the connector doesn't yet recognize
(e.g. a private preview, an enterprise variant), register a family. This
is the production-grade fix; one registration covers every slug in that
line.

```python
import re
from genblaze_core.providers import ModelFamily, ModelSpec
from genblaze_core.models.enums import Modality

reg = MyProvider.models_default().fork()
reg.register_family(
    ModelFamily(
        name="my-private-line",
        pattern=re.compile(r"^my-vendor-private-"),
        spec_template=ModelSpec(model_id="*", modality=Modality.VIDEO),
        description="Private-preview models from my vendor.",
        example_slugs=("my-vendor-private-2025-q1",),
    )
)
provider = MyProvider(models=reg)
```

### 2. Force a discovery refresh — your NATIVE provider's cache is stale

If the slug *is* listed upstream but the SDK's cached discovery snapshot
predates its launch, refresh:

```python
result = provider.validate_model("just-shipped-slug", refresh=True)
if result.is_ok:
    # cache now contains the new slug; subsequent steps are authoritative
    ...
```

`refresh=True` bypasses the cache and re-runs the connector's discovery
fetcher. Use sparingly — every refresh hits the upstream model-list
endpoint.

### 3. Register the slug directly — one-off override

If neither of the above applies (e.g. an unannounced variant the vendor
hasn't published), `register()` accepts the spec directly. The user
layer wins over family resolution for exact matches.

```python
reg = MyProvider.models_default().fork()
reg.register(ModelSpec(model_id="my-custom-slug", modality=Modality.VIDEO))
provider = MyProvider(models=reg)
```

### Last resort — disable preflight

If you need to ship and triage later, `Pipeline(preflight=False)` skips
the validation gate entirely. **Use sparingly.** Disabling preflight
means dead slugs surface as mid-pipeline `404`s rather than upfront
`NOT_FOUND`s, and your monitoring loses the ability to distinguish
"unknown slug" from "transient API error". The recommended path is to
fix the registration; this opt-out exists for hotfixes and demos.

```python
from genblaze_core import Pipeline

run = (
    Pipeline("my-run", preflight=False)
    .step(provider="my-vendor", model="bleeding-edge", prompt="...")
    .run()
)
```

---

## Pricing tables moved

Every 0.2.x `_PRICING` / `_RATES` / `_PRICE_PER_*` table that used to
ship inside a connector module has been moved to
[`docs/reference/pricing-recipes.md`](../reference/pricing-recipes.md).
Each section is a copy-pasteable Python block scoped to one provider —
register what you need, skip what you don't.

If your code currently relies on `cost_usd` being non-`None` after
`provider.invoke(step)`, expect `None` until you've registered a
strategy. The recipes are tagged with their snapshot date and the
upstream pricing URL — verify before using in production.

---

## What you'll see calling a `DiscoverySupport.NONE` provider

`DiscoverySupport.NONE` is the connector author's signal that no
authoritative liveness check is available — typically because the
vendor SDK doesn't expose a `/models` endpoint and per-slug probing
would enqueue a billable generation. End users see:

- Preflight returns `OK_PROVISIONAL` for any slug that matches a shipped
  family pattern, with `source = ValidationSource.FAMILY`.
- Preflight returns `UNKNOWN_PERMISSIVE` (with `source = ValidationSource.FALLBACK`)
  for slugs that match no family but the connector's permissive
  fallback spec applies. The slug goes through to the upstream which
  decides.
- One WARN-level log line per `(provider, slug)` pair per Pipeline
  instance — the `_warned_preflight` set deduplicates so a busy
  pipeline doesn't spam the log.

Connectors currently classified `NONE`: Decart, Runway, Luma, LMNT,
Stability-Audio. ElevenLabs SFX is `NONE` for the SFX modality only;
ElevenLabs TTS is `NATIVE`.

---

## Common gotchas

1. **`provider.discover_models()` returns `DiscoveryResult.unsupported(...)`
   on a `NONE` provider** — expected. NONE means there's no upstream
   catalog to enumerate; the result carries an explanatory `detail`
   string rather than an empty slug list. Check
   `result.status is DiscoveryStatus.UNSUPPORTED` rather than treating
   it as an error or iterating empty slugs. Use
   `provider.validate_model(slug)` to check individual slugs.
2. **Pipeline preflight raising `MODEL_ERROR` after upgrade** — usually
   means a slug your code passes is now `NOT_FOUND` because the
   connector dropped its hardcoded list and the slug doesn't fit any
   shipped family. Apply the
   [decision order above](#what-to-do-when-you-hit-not_found-for-a-slug-you-know-exists).
3. **Tests asserting on `cost_usd`** — flat per-asset cost is no longer
   automatic. Either register a strategy in your test fixture (recipe),
   set `expects_cost = False` on the connector compliance harness, or
   assert `cost_usd is None`.
4. **Custom `ModelRegistry` subclasses** — if you subclassed
   `ModelRegistry` and called `super().__init__(defaults=...)`, that
   call now `TypeError`s. Switch to `super().__init__()` and append via
   `extend()` in your subclass body.

---

## Deprecation horizon

These APIs are deprecated in 0.3.0 and will be **removed in
`genblaze-core` 0.4.0**. Each emits a runtime `DeprecationWarning`
at use; pin your imports against the new surface to silence them and
prepare for the next major.

| Deprecated in 0.3.0 | Replacement | Removed in |
|---|---|---|
| `BaseProvider.probe_model(model_id) -> ProbeResult` | `BaseProvider.validate_model(model_id) -> ValidationResult` (richer outcome + source + suggested_slugs) | 0.4.0 |
| `ModelRegistry(defaults={...})` constructor kwarg | `reg = ModelRegistry(); reg.register(spec)` or `reg.extend([...])` | already removed in 0.3.0 (raises `TypeError`) |

If your code calls `probe_model()` directly, migrate now —
`validate_model()` returns the same liveness signal plus a typed
outcome that distinguishes "live", "dead", "unknown", and "unstable"
without you having to interpret a `ProbeResult` enum.

---

## Reporting issues

Filed as a regression? Open an issue at
[`backblaze-labs/genblaze`](https://github.com/backblaze-labs/genblaze/issues)
and include:

- Provider package + version (`pip show genblaze-<provider>`)
- The slug your code passes
- The `ValidationResult.outcome` you see at preflight
- The minimal reproduction (constructor + `invoke` call)

The conformance test suite at `libs/core/tests/conformance/` is the
fastest gate — if your case slips past those, that's the bar to raise
for the next release.
