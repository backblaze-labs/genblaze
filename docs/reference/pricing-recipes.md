<!-- last_verified: 2026-06-15 -->
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

## Hume

**Source:** user-registered (the SDK ships no prices for Hume).
**Snapshot date:** TODO — fill in when you read the rate.
**Verify at:** [hume.ai/pricing](https://www.hume.ai/pricing) /
[platform.hume.ai](https://platform.hume.ai/).

Hume Octave TTS bills per character of input text. The same rate applies to
`octave-1` and `octave-2` (the model is selected via the request `version`
field, not separate slugs). Replace `RATE` below with the current
per-character USD rate from Hume's pricing page.

```python
from genblaze_core.providers import per_input_chars
from genblaze_hume import HumeTTSProvider

provider = HumeTTSProvider(api_key="...")

# Octave is priced per 1,000 characters of input text.
HUME_PRICE_PER_1K_CHARS = RATE  # TODO: confirm at hume.ai/pricing
for slug in ("octave-1", "octave-2"):
    provider.models.register_pricing(
        slug, per_input_chars(HUME_PRICE_PER_1K_CHARS, per=1000)
    )
```

To cover any Octave slug with one rule, register against `"*"` (the fallback
spec's id) — the registry routes unmatched slugs through the fallback:

```python
provider.models.register_pricing("*", per_input_chars(HUME_PRICE_PER_1K_CHARS, per=1000))
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

## Runway

**Source:** module-level constant `_RUNWAY_PRICING: dict[tuple[str, Any],
float]` keyed by `(model, duration)` in `genblaze_runway/provider.py`
prior to `genblaze-core 0.3.0`.
**Snapshot date:** 2026-05-05.
**Verify at:** [docs.runwayml.com](https://docs.runwayml.com/).

Runway bills per-generation, with rates depending on the model variant
and the requested duration (5s vs 10s).

```python
from genblaze_core.providers import by_model_and_param
from genblaze_runway import RunwayProvider

# (model, duration_seconds) → USD per generation, snapshot 2026-05-05.
RUNWAY_RATES: dict = {
    ("gen4_turbo", 5): 0.50,
    ("gen4_turbo", 10): 1.00,
    ("gen3a_turbo", 5): 0.25,
    ("gen3a_turbo", 10): 0.50,
}

provider = RunwayProvider(api_secret="...")
for slug in ("gen4_turbo", "gen3a_turbo"):
    provider.models.register_pricing(
        slug, by_model_and_param("duration", RUNWAY_RATES)
    )
```

Future Runway variants (Gen-5, Gen-4a, etc.) match the
`runway-gen-video` family pattern automatically but won't have rates
in this recipe — extend `RUNWAY_RATES` and re-register as new slugs
ship.

---

## Decart

**Source:** module-level constants `_VIDEO_PRICING: dict[str, float]` (keyed
by resolution) in `genblaze_decart/provider.py` and `_IMAGE_PRICE = 0.02`
in `genblaze_decart/image.py` prior to `genblaze-core 0.3.0`.
**Snapshot date:** 2026-05-05.
**Verify at:** [docs.platform.decart.ai](https://docs.platform.decart.ai/).

Decart Lucy bills per-generation, with video pricing keyed off the
`resolution` parameter (480p vs 720p) and image pricing flat across
all variants.

```python
from genblaze_core.providers import by_param, per_unit
from genblaze_decart import DecartImageProvider, DecartVideoProvider

# --- Video (USD/asset, keyed by resolution) ---
DECART_VIDEO_RATES: dict = {"480p": 0.04, "720p": 0.08}

video = DecartVideoProvider(api_key="...")
for slug in (
    "lucy-pro-t2v",
    "lucy-pro-i2v",
    "lucy-pro-v2v",
    "lucy-2-v2v",
    "lucy-fast-v2v",
    "lucy-motion",
    "lucy-dev-i2v",
    "lucy-restyle-v2v",
):
    video.models.register_pricing(
        slug,
        by_param("resolution", DECART_VIDEO_RATES, default=DECART_VIDEO_RATES["480p"]),
    )

# --- Image (flat USD/asset) ---
DECART_IMAGE_PRICE = 0.02

image = DecartImageProvider(api_key="...")
for slug in ("lucy-pro-t2i", "lucy-pro-i2i"):
    image.models.register_pricing(slug, per_unit(DECART_IMAGE_PRICE))
```

Future Lucy variants match the family pattern automatically but won't
have rates in this recipe — extend the dicts and re-register as new
slugs ship.

---

## ElevenLabs

**Source:** module-level constants `_ELEVENLABS_PER_1K_RATES` (per-1K-character
rates per TTS model tier) in `genblaze_elevenlabs/provider.py` and
`_SFX_DURATION_BUCKETS` (duration-keyed flat rates) in
`genblaze_elevenlabs/sfx.py` prior to `genblaze-core 0.3.0`.
**Snapshot date:** 2026-05-05.
**Verify at:** [elevenlabs.io/pricing](https://elevenlabs.io/pricing).

ElevenLabs TTS bills per character of input text, with rates varying by
model tier (Flash is cheapest; Multilingual / V3 most expensive). SFX
bills bucketed by duration.

```python
from genblaze_core.providers import bucketed_by_duration, per_input_chars
from genblaze_elevenlabs import ElevenLabsSFXProvider, ElevenLabsTTSProvider

# --- TTS (USD per 1K input chars, per model tier) ---
ELEVENLABS_TTS_RATES_PER_1K: dict[str, float] = {
    "eleven_v3": 0.30,
    "eleven_multilingual_v2": 0.30,
    "eleven_flash_v2_5": 0.08,
    "eleven_turbo_v2_5": 0.15,
}

tts = ElevenLabsTTSProvider(api_key="...")
for slug, rate in ELEVENLABS_TTS_RATES_PER_1K.items():
    tts.models.register_pricing(slug, per_input_chars(rate, per=1000))


# --- SFX (USD bucketed by output duration) ---
# Buckets: ≤5s = $0.10, ≤15s = $0.20, ≤30s = $0.30.
ELEVENLABS_SFX_BUCKETS: list[tuple[tuple[float, float], float]] = [
    ((0.0, 5.0 + 1e-9), 0.10),
    ((5.0 + 1e-9, 15.0 + 1e-9), 0.20),
    ((15.0 + 1e-9, 30.0 + 1e-9), 0.30),
]

sfx = ElevenLabsSFXProvider(api_key="...")
sfx.models.register_pricing(
    "eleven_text_to_sound_v2", bucketed_by_duration(ELEVENLABS_SFX_BUCKETS)
)
```

Future ElevenLabs TTS variants (`eleven_v4`, etc.) match the
`elevenlabs-tts` family pattern automatically but won't have rates in
this recipe — extend `ELEVENLABS_TTS_RATES_PER_1K` and re-register as
new slugs ship. Probe via `tts.discover_models()` to confirm new slugs
are live before registering rates against them.

---

## OpenAI

**Source:** `_TTS_PER_1M_RATES` in `genblaze_openai/tts.py` plus the
per-model `pricing={(quality, size): rate}` tables in
`genblaze_openai/dalle.py` prior to `genblaze-core 0.3.0`. Sora pricing
was always `None` (correct formula requires per-second `(model, size,
seconds)` billing; a flat dict misreports 10x+).
**Snapshot date:** 2026-05-05.
**Verify at:** [openai.com/pricing](https://openai.com/pricing).

OpenAI bills three image-/audio-/video-generation surfaces with three
different shapes:

- **TTS**: per-1M-character rates per model tier.
- **Image** (DALL-E + GPT-Image): tiered by `(quality, size)` per model.
- **Sora** (video): per-second rates that depend on `(model, size,
  seconds)`. **The SDK ships no Sora pricing recipe** because a flat
  table can't represent the per-second formula honestly. Use the
  upstream's published rate calculator until OpenAI exposes a stable
  programmatic shape.

```python
from genblaze_core.providers import per_input_chars, tiered
from genblaze_openai import DalleProvider, OpenAITTSProvider

# --- TTS (USD per 1M input chars) ---
OPENAI_TTS_RATES_PER_1M: dict[str, float] = {
    "tts-1": 15.00,
    "tts-1-hd": 30.00,
    "gpt-4o-mini-tts": 12.00,
}

tts = OpenAITTSProvider(api_key="...")
for slug, rate in OPENAI_TTS_RATES_PER_1M.items():
    tts.models.register_pricing(slug, per_input_chars(rate, per=1_000_000))


# --- Image (USD per generation, tiered by (quality, size)) ---
def image_key(ctx):
    p = ctx.step.params
    quality = p.get("quality")
    size = p.get("size", "1024x1024")
    if quality is None:
        # Prefer "auto" if the table has it, else "standard".
        # (Caller can adjust the default per their workload.)
        return ("standard", size)
    return (quality, size)


OPENAI_DALLE3_RATES: dict = {
    ("standard", "1024x1024"): 0.040,
    ("standard", "1024x1792"): 0.080,
    ("standard", "1792x1024"): 0.080,
    ("hd", "1024x1024"): 0.080,
    ("hd", "1024x1792"): 0.120,
    ("hd", "1792x1024"): 0.120,
}

OPENAI_DALLE2_RATES: dict = {
    ("standard", "256x256"): 0.016,
    ("standard", "512x512"): 0.018,
    ("standard", "1024x1024"): 0.020,
}

OPENAI_GPT_IMAGE_1_RATES: dict = {
    ("low", "1024x1024"): 0.011,
    ("low", "1024x1536"): 0.016,
    ("low", "1536x1024"): 0.016,
    ("low", "auto"): 0.011,
    ("medium", "1024x1024"): 0.042,
    ("medium", "1024x1536"): 0.063,
    ("medium", "1536x1024"): 0.063,
    ("medium", "auto"): 0.042,
    ("high", "1024x1024"): 0.167,
    ("high", "1024x1536"): 0.250,
    ("high", "1536x1024"): 0.250,
    ("high", "auto"): 0.167,
}

# (Similar tables exist for gpt-image-1.5 and gpt-image-1-mini.
# gpt-image-2 has no published rates — leave its pricing None.)

dalle = DalleProvider(api_key="...")
dalle.models.register_pricing("dall-e-3", tiered(OPENAI_DALLE3_RATES, key=image_key))
dalle.models.register_pricing("dall-e-2", tiered(OPENAI_DALLE2_RATES, key=image_key))
dalle.models.register_pricing("gpt-image-1", tiered(OPENAI_GPT_IMAGE_1_RATES, key=image_key))
```

OpenAI ships new image variants frequently. Match the family pattern
via `provider.discover_models(refresh=True)`, then extend the rate
tables and register the new slugs as they appear in the catalog.

---

## Google

**Source:** `_VEO_PER_SECOND_RATES` in `genblaze_google/provider.py` and
`_IMAGEN_PER_IMAGE_RATES` in `genblaze_google/imagen.py` prior to
`genblaze-core 0.3.0`. Standalone `genblaze_google.chat()` retains its
own per-token table — chat is out of scope for the catalog-decoupling
effort because it isn't a Pipeline-Step provider.
**Snapshot date:** 2026-05-05.
**Verify at:** [ai.google.dev/pricing](https://ai.google.dev/pricing)
and [cloud.google.com/vertex-ai/generative-ai/pricing](https://cloud.google.com/vertex-ai/generative-ai/pricing).

Veo bills per-second of generated video — multiply the rate by the
requested `duration_seconds` and the number of returned clips.
Imagen bills flat per-image.

```python
from genblaze_core.providers import PricingContext, PricingStrategy, per_unit
from genblaze_google import ImagenProvider, VeoProvider

# --- Veo (USD per second of generated video) ---
VEO_PER_SECOND_RATES: dict[str, float] = {
    "veo-2.0-generate-001": 0.35,
    "veo-3.0-generate-001": 0.50,
    "veo-3.0-fast-generate-001": 0.25,
}


def per_second_by_model(rate: float) -> PricingStrategy:
    """Per-second of requested duration × asset count.

    Reads ``duration_seconds`` from ``step.params`` (Veo native, string
    form), falling back to ``duration`` (canonical alias), then to 4s.
    """

    def _strategy(ctx: PricingContext) -> float | None:
        raw = ctx.step.params.get("duration_seconds") or ctx.step.params.get("duration")
        try:
            dur = int(raw) if raw is not None else 4
        except (TypeError, ValueError):
            dur = 4
        count = ctx.output_count or 1
        return rate * dur * count

    return _strategy


veo = VeoProvider(api_key="...")
for slug, rate in VEO_PER_SECOND_RATES.items():
    veo.models.register_pricing(slug, per_second_by_model(rate))


# --- Imagen (flat USD per generated image) ---
IMAGEN_PER_IMAGE_RATES: dict[str, float] = {
    "imagen-3.0-generate-002": 0.04,
    "imagen-3.0-fast-generate-001": 0.02,
}

imagen = ImagenProvider(api_key="...")
for slug, rate in IMAGEN_PER_IMAGE_RATES.items():
    imagen.models.register_pricing(slug, per_unit(rate))
```

Future `veo-N` and `imagen-N` slugs match the family patterns
automatically; extend the rate dictionaries and re-register as new
models ship. `client.models.get(slug)` is the authoritative liveness
check — preflight surfaces dead/unauthorized slugs as `NOT_FOUND`
before submission.

---

## Luma

**Source:** never shipped — Luma bills by duration and the SDK
intentionally left ``cost_usd`` ``None`` rather than misreport with a
flat per-generation rate. The recipe below documents the formula so
users opting into cost tracking can register it themselves.
**Snapshot date:** 2026-05-05.
**Verify at:** [lumalabs.ai/pricing](https://lumalabs.ai/dream-machine/pricing).

Luma generations bill per-second of generated video. ``duration`` is
typically passed as a string (e.g. ``"5s"``) on Luma's API; the
strategy strips a trailing ``s`` defensively.

```python
from genblaze_core.providers import PricingContext, PricingStrategy
from genblaze_luma import LumaProvider

# (model → USD per second). Sample rates — verify against your billing.
LUMA_PER_SECOND_RATES: dict[str, float] = {
    "ray-2": 0.40,
    "ray-flash-2": 0.20,
}


def per_second_by_model(rate: float) -> PricingStrategy:
    def _strategy(ctx: PricingContext) -> float | None:
        raw = ctx.step.params.get("duration")
        if isinstance(raw, str) and raw.endswith("s"):
            raw = raw[:-1]
        try:
            dur = int(raw) if raw is not None else 5
        except (TypeError, ValueError):
            dur = 5
        count = ctx.output_count or 1
        return rate * dur * count

    return _strategy


luma = LumaProvider(auth_token="...")
for slug, rate in LUMA_PER_SECOND_RATES.items():
    luma.models.register_pricing(slug, per_second_by_model(rate))
```

Future ``ray-N`` slugs match the family pattern automatically; extend
the rate dictionary and re-register as new models ship. The lumaai
SDK exposes no per-slug liveness probe that doesn't enqueue a
billable generation, so unknown slugs surface at submission time
rather than at preflight.

---

## Stability Audio

**Source:** `_PRICE_PER_SEC = 0.01` and the bespoke
`_per_second_with_param_fallback` strategy in
`genblaze_stability_audio/provider.py` prior to `genblaze-core 0.3.0`.
**Snapshot date:** 2026-05-05.
**Verify at:** [platform.stability.ai/pricing](https://platform.stability.ai/pricing).

Stable Audio bills per-second of generated audio. The connector probes
the rendered file's actual duration via mutagen; when the probe
returns `None` (e.g. test fixtures with fake bytes) the strategy falls
back to the requested `step.params["duration"]`.

```python
from genblaze_core.providers import PricingContext, PricingStrategy
from genblaze_stability_audio import StabilityAudioProvider

STABILITY_AUDIO_RATES_PER_SEC: dict[str, float] = {
    "stable-audio-2.5": 0.01,
}


def per_second_with_param_fallback(rate: float) -> PricingStrategy:
    """Per-second pricing using probed asset duration, falling back to params.

    Mirrors the previous SDK shipping behavior: the Stability HTTP
    response carries the audio bytes only, so we probe the file
    duration after writing it. When the probe returns ``None``, the
    requested ``duration`` param is the next-best signal.
    """

    def _strategy(ctx: PricingContext) -> float | None:
        dur = ctx.output_duration_s
        if dur is None:
            raw = ctx.step.params.get("duration")
            try:
                dur = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                dur = None
        if dur is None:
            return None
        return dur * rate

    return _strategy


stability_audio = StabilityAudioProvider(api_key="...")
for slug, rate in STABILITY_AUDIO_RATES_PER_SEC.items():
    stability_audio.models.register_pricing(slug, per_second_with_param_fallback(rate))
```

Future `stable-audio-N` slugs match the family pattern automatically;
extend the rate dictionary and re-register as new models ship.

---

<!--
  Subsequent connectors append their sections here as they migrate:
    - nvidia (chat / generative)
-->
