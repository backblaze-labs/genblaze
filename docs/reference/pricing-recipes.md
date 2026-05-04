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

<!--
  Subsequent connectors append their sections here as they migrate:
    - replicate
    - nvidia (chat / generative)
    - gmicloud
    - runway
    - decart
    - elevenlabs
    - openai
    - google
    - luma
    - stability-audio
-->
