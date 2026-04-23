#!/usr/bin/env python3
"""Custom Model Registry: three levels of runtime control over pricing & specs.

Demonstrates how to customize a provider's model handling WITHOUT a library
release — useful when a provider ships a new model faster than genblaze, or
when you have volume-discount pricing, or when you want strict param validation.

Runs with zero API calls — it only exercises the registry, not any provider
submit path. No network, no API keys.

Three scenarios:
  1. Fallback — unknown model works out-of-box with cost_usd=None
  2. Pricing override — add pricing for an existing model (one line)
  3. Full custom spec — register a brand-new model with schema + allowlist

Usage:
    pip install genblaze-core genblaze-openai
    python examples/custom_model_registry.py
"""

from __future__ import annotations

from genblaze_core import Modality
from genblaze_core.models.asset import Asset
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    IntSchema,
    ModelSpec,
    PricingContext,
    per_unit,
)
from genblaze_openai import DalleProvider


def _fake_cost(reg, model_id: str, step: Step, n_assets: int = 1) -> float | None:
    """Invoke the spec's pricing strategy directly against fake assets.

    Normally this runs automatically after fetch_output(). We call it
    manually so the example doesn't need a live API.
    """
    spec = reg.get(model_id)
    if spec.pricing is None:
        return None
    assets = [
        Asset(url=f"file:///tmp/fake-{i}.png", media_type="image/png") for i in range(n_assets)
    ]
    ctx = PricingContext(step=step, assets=assets, provider_payload={})
    return spec.pricing(ctx)


def scenario_1_unknown_model() -> None:
    """Unknown models work — the fallback spec forwards params as-is."""
    print("=" * 70)
    print("Scenario 1: unknown model (zero registration)")
    print("=" * 70)

    provider = DalleProvider()  # default registry, no customization
    reg = provider.models

    # Library has never heard of this model — falls back to permissive spec.
    spec = reg.get("gpt-image-3-unreleased")
    print(f"  spec.model_id    = {spec.model_id!r}  # fallback sentinel")
    print(f"  spec.pricing     = {spec.pricing}    # None → cost_usd stays None")
    print(f"  reg.has('...')   = {reg.has('gpt-image-3-unreleased')}  # unknown")
    print("  Result: request would submit fine, cost_usd is None.")
    print()


def scenario_2_override_pricing() -> None:
    """Override pricing on a known model — one line, no library release."""
    print("=" * 70)
    print("Scenario 2: volume-discount pricing on an existing model")
    print("=" * 70)

    reg = DalleProvider.models_default().fork()
    reg.register_pricing("dall-e-3", per_unit(0.050))  # your negotiated rate
    provider = DalleProvider(models=reg)

    step = Step(
        provider="openai-dalle",
        model="dall-e-3",
        prompt="a sunset over Big Sur",
        params={"quality": "standard", "size": "1024x1024"},
    )

    # Per-instance fork doesn't leak into other DalleProvider instances
    other = DalleProvider()  # default registry
    default_cost = _fake_cost(other.models, "dall-e-3", step, n_assets=1)
    forked_cost = _fake_cost(provider.models, "dall-e-3", step, n_assets=1)

    print(f"  default dall-e-3 cost (1 img): ${default_cost:.4f}")
    print(f"  forked  dall-e-3 cost (1 img): ${forked_cost:.4f}  # your rate")
    print("  Result: library untouched, only this provider instance pays $0.050.")
    print()


def scenario_3_custom_spec() -> None:
    """Register a brand-new model with validation, pricing, and allowlist."""
    print("=" * 70)
    print("Scenario 3: register a brand-new model with full ModelSpec")
    print("=" * 70)

    reg = DalleProvider.models_default().fork()
    reg.register(
        ModelSpec(
            model_id="gpt-image-3-preview",
            modality=Modality.IMAGE,
            pricing=per_unit(0.20),
            param_schemas={"n": IntSchema(min=1, max=4)},
            param_allowlist=frozenset({"prompt", "n", "seed"}),
        )
    )
    provider = DalleProvider(models=reg)

    step = Step(
        provider="openai-dalle",
        model="gpt-image-3-preview",
        prompt="a misty forest at dawn",
        params={"n": 2, "unknown_param": "ignored", "size": "also dropped"},
    )

    payload = provider.prepare_payload(step)
    cost = _fake_cost(provider.models, "gpt-image-3-preview", step, n_assets=2)

    print(f"  raw params   : {step.params}")
    print(f"  forwarded    : {payload}")
    print("                 # allowlist dropped 'unknown_param' and 'size'")
    print(f"  cost (n=2)   : ${cost:.4f}  # $0.20 × 2 images")
    print("  Result: strict validation + pricing on a model library has never seen.")
    print()


def main() -> None:
    print()
    print("Custom Model Registry — runtime control over pricing & specs")
    print()
    scenario_1_unknown_model()
    scenario_2_override_pricing()
    scenario_3_custom_spec()
    print("=" * 70)
    print("See docs/features/model-registry.md for the full ModelSpec surface.")
    print("=" * 70)


if __name__ == "__main__":
    main()
