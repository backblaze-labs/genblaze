<!-- last_verified: 2026-05-04 -->
# Pricing recipes

> **Not maintained.** Prices in this document are snapshots taken at the
> time each connector was migrated to the catalog-decoupled architecture
> in `genblaze-core 0.3.0`. **Verify with the upstream provider before
> relying on them for billing or cost-estimation.** See
> [docs/exec-plans/active/model-registry-decoupling.md](../exec-plans/active/model-registry-decoupling.md)
> for the rationale behind shipping pricing as a one-shot cookbook
> rather than maintained SDK state.

## Why pricing is user-registered

Genblaze SDK ≥ 0.3.0 ships zero hardcoded prices. Reasons:

- **Rot half-life.** Provider pricing changes faster than our release
  cadence. Hardcoded rates silently drift out of sync.
- **Source-of-truth fidelity.** The upstream's pricing page is canonical;
  the SDK was just memorizing snapshots.
- **Surface area.** ~50 pricing-strategy references across 11 connectors
  was a continuous maintenance tax with no compensating value.

`compute_cost()` and `Pipeline.estimated_cost()` return `None` for any
model unless you've registered pricing via
`provider.models.register_pricing(slug, strategy)`. The pricing
strategies in `genblaze_core.providers.pricing` (`per_unit`,
`by_param`, `bucketed_by_duration`, `per_input_chars`,
`per_input_tokens`, `per_response_metric`, `by_model_and_param`,
`tiered`, `first_match`) are unchanged — they're still the building
blocks. You just call them yourself.

## How to use a recipe

Each connector section below has a snippet that registers the
last-known prices at construction time. Copy-paste, then maintain it
against the upstream's docs in your application code, not in the SDK.

```python
# 1. Construct the provider as usual.
provider = LMNTProvider(api_key=...)

# 2. Apply the pricing recipe (see sections below).
register_lmnt_pricing(provider.models)

# 3. compute_cost() now works for the slugs the recipe covers.
```

---

## LMNT

**Source:** module-level constant `_PRICE_PER_CHAR = 0.00015` in
`genblaze_lmnt/provider.py` prior to `genblaze-core 0.3.0`.
**Snapshot date:** 2026-05-04.
**Verify at:** [docs.lmnt.com](https://docs.lmnt.com/).

LMNT bills per character of input text. The rate applies to all model
ids — there is no enumerated catalog.

```python
from genblaze_core.providers import per_input_chars
from genblaze_lmnt import LMNTProvider

provider = LMNTProvider(api_key="...")

# Apply per-character pricing to every slug the user passes. The
# fallback spec carries no model_id list, so we register against any
# concrete slug the application uses.
LMNT_PRICE_PER_CHAR = 0.00015  # USD/char as of 2026-05-04
for slug in ("lmnt-1", "blizzard"):
    provider.models.register_pricing(slug, per_input_chars(LMNT_PRICE_PER_CHAR, per=1))
```

If your application uses a single LMNT model, register once. If you
want a single rule covering any LMNT slug, register against `"*"` (the
fallback spec's id) — the registry routes unknown slugs through the
fallback, so a `register_pricing("*", ...)` applies universally.

```python
provider.models.register_pricing("*", per_input_chars(LMNT_PRICE_PER_CHAR, per=1))
```

---

## Replicate

**Source:** module-level constants ``_COST_PER_SEC = 0.000225`` and
``_compute_time_cost`` in ``genblaze_replicate/provider.py`` prior to
``genblaze-core 0.3.0``.
**Snapshot date:** 2026-05-04.
**Verify at:** [replicate.com/pricing](https://replicate.com/pricing).

Replicate bills based on actual GPU-compute time, reported per
prediction in ``prediction.metrics.predict_time``. The connector
captures this value in ``step.provider_payload["replicate"]["predict_time"]``
during ``fetch_output()``, so any pricing strategy you register can read
it via ``ctx.provider_payload``.

The historical default rate was ``$0.000225/second`` (Nvidia A40/T4
tier). Replicate publishes per-hardware rates that can vary by model;
you may want different rates for different families.

```python
from genblaze_core.providers import per_response_metric
from genblaze_replicate import ReplicateProvider

REPLICATE_COST_PER_SEC = 0.000225  # USD/sec, A40/T4 tier as of 2026-05-04

def compute_time_cost(ctx):
    """Read predict_time from the captured provider payload."""
    payload = ctx.provider_payload.get("replicate") if ctx.provider_payload else None
    if not isinstance(payload, dict):
        return None
    predict_time = payload.get("predict_time")
    if predict_time is None:
        return None
    try:
        return float(predict_time) * REPLICATE_COST_PER_SEC
    except (TypeError, ValueError):
        return None

provider = ReplicateProvider(api_token="...")

# Register against the fallback (catch-all) so every Replicate slug
# inherits the rule. Replace with explicit per-slug registrations if
# you maintain different rates per family.
provider.models.register_pricing("*", per_response_metric(compute_time_cost))
```

For per-slug rate variation (e.g., higher rates on H100-tier models):

```python
H100_RATE_PER_SEC = 0.001  # placeholder; verify with Replicate
H100_MODELS = {"some-owner/h100-model", "another-owner/big-model"}

for slug in H100_MODELS:
    provider.models.register_pricing(
        slug,
        per_response_metric(lambda ctx, rate=H100_RATE_PER_SEC: _compute_with_rate(ctx, rate)),
    )
```

---

## GMICloud

**Source:** `_AUDIO_MODELS`, `_IMAGE_MODELS`, `_VIDEO_MODELS` row-per-slug
data structures in `genblaze_gmicloud/models/*.py` prior to
`genblaze-core 0.3.0`. Two pricing strategies were in use: flat
`per_unit(rate)` for most models, and a per-second `_per_duration_rate(rate)`
for Seedance 2.0.
**Snapshot date:** 2026-05-04.
**Verify at:** [docs.gmicloud.ai](https://docs.gmicloud.ai/).

GMICloud bills per-asset for most models (a fixed rate per generation),
with one per-second-billed model (`seedance-2-0-260128`). The rates
below were the SDK defaults at migration time. Pricing on GMI is
contract-specific — check your account.

```python
from genblaze_core.providers import (
    PricingContext,
    PricingStrategy,
    per_unit,
)
from genblaze_gmicloud import (
    GMICloudAudioProvider,
    GMICloudImageProvider,
    GMICloudVideoProvider,
)


def per_duration(rate: float) -> PricingStrategy:
    """Per-second strategy reading ``duration`` from step.params."""

    def s(ctx: PricingContext) -> float | None:
        dur = ctx.step.params.get("duration")
        if dur is None:
            return None
        try:
            return rate * float(dur) * (ctx.output_count or 1)
        except (TypeError, ValueError):
            return None

    return s


# --- Audio (USD/asset, as of 2026-05-04) ---
GMI_AUDIO_RATES = {
    "ElevenLabs-TTS-v3": 0.10,
    "MiniMax-TTS-Speech-2.6-Turbo": 0.06,
    "MiniMax-Voice-Clone-Speech-2.6-HD": 0.10,
    "Inworld-TTS-1.5-Mini": 0.005,
    "MiniMax-Music-2.5": 0.15,
}

audio = GMICloudAudioProvider(api_key="...")
for slug, rate in GMI_AUDIO_RATES.items():
    audio.models.register_pricing(slug, per_unit(rate))


# --- Image (USD/asset) ---
GMI_IMAGE_RATES = {
    "seedream-5.0-lite": 0.035,
    "gemini-2.5-flash-image": 0.039,
    "flux-kontext-pro": 0.05,
    "seededit-3-0-i2i-250628": 0.03,
    "reve-create-20250915": 0.007,
    "reve-edit-20250915": 0.007,
    "reve-edit-fast-20251030": 0.007,
    "reve-remix-20250915": 0.007,
    "reve-remix-fast-20251030": 0.007,
    "bria-fibo-image-blend": 0.02,
    "bria-fibo-relight": 0.02,
    "bria-fibo-restore": 0.02,
    "bria-genfill": 0.02,
    "bria-eraser": 0.02,
}

image = GMICloudImageProvider(api_key="...")
for slug, rate in GMI_IMAGE_RATES.items():
    image.models.register_pricing(slug, per_unit(rate))
```

> **⚠ Read this before copy-pasting the video block below.** The 2026-04
> reconciliation flagged four GMI video slugs as known-unstable: they
> returned 404 against the live ``/requests`` endpoint at the snapshot
> date. Those slugs are listed in `GMI_VIDEO_UNSTABLE_RATES` separately
> from the live rates so you don't accidentally lock in a rate for a
> slug you'll never successfully invoke. Use
> `provider.validate_model(slug, refresh=True)` to check liveness
> before relying on any of them.

```python
# --- Video — known-live rates (USD/asset, except per-second rows noted) ---
GMI_VIDEO_FLAT_RATES = {
    "seedance-1-0-pro-250528": 0.30,
    "seedance-1-0-pro-fast": 0.022,
    "veo3": 0.40,
    "sora-2-pro": 0.50,
    "kling-image2video-v2.1-master": 0.28,
    "kling-image2video-v1.6-pro": 0.098,
    "kling-text2video-v1.6-pro": 0.098,
    "kling-image2video-v1.5-pro": 0.098,
    "kling-text2video-v1.5-pro": 0.098,
    "pixverse-v5.6-t2v": 0.03,
    "pixverse-v5.6-i2v": 0.03,
    "pixverse-v5.6-transition": 0.03,
    "wan2.6-t2v": 0.15,
    "wan2.6-i2v": 0.15,
    "wan2.6-r2v": 0.15,
    "wan2.7-t2v": 0.15,
    "wan2.7-i2v": 0.15,
    "luma-ray-2": 0.20,
}

# --- Video — UNSTABLE: 404 in 2026-04 reconciliation. Verify before use. ---
# Wrapped in a separate dict so a copy-paste consumer is forced to look
# at it consciously rather than scrolling past a comment.
GMI_VIDEO_UNSTABLE_RATES = {
    "veo3-fast": 0.15,
    "kling-text2video-v2.1-master": 0.28,
    "minimax-hailuo-2.3-fast": 0.032,
    "vidu-q1": 0.10,
}

video = GMICloudVideoProvider(api_key="...")
for slug, rate in GMI_VIDEO_FLAT_RATES.items():
    video.models.register_pricing(slug, per_unit(rate))

# Register unstable rates ONLY after probing each — see warning above.
# for slug, rate in GMI_VIDEO_UNSTABLE_RATES.items():
#     if video.validate_model(slug, refresh=True).is_ok:
#         video.models.register_pricing(slug, per_unit(rate))

# Seedance 2.0 is per-second-billed.
video.models.register_pricing("seedance-2-0-260128", per_duration(0.052))
```

---

<!--
  Subsequent connectors append their sections here as they migrate:
    - nvidia (chat / generative)
    - runway
    - decart
    - elevenlabs
    - openai
    - google
    - luma
    - stability-audio
-->
