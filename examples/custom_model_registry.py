#!/usr/bin/env python3
"""Custom Model Registry: three levels of runtime control over the SDK.

Demonstrates the three power-user surfaces the 0.3.0 registry exposes:
adding pricing to a slug the SDK doesn't price, teaching the SDK about
a vendor model line it doesn't ship, and registering a one-off spec
with full param validation.

Runs with zero API calls — exercises only the registry, no provider
submit path. No network, no API keys required.

Three scenarios:
  1. Register pricing on a family-matched slug — preserves the family's
     param contracts (aliases, allowlist, schemas, extras) and layers
     your pricing strategy on top.
  2. Register a new family for a vendor model line — teaches the SDK
     to route a whole pattern of slugs without per-slug registration.
  3. Register a brand-new model with full ModelSpec — schema validation,
     allowlist filtering, custom pricing.

Usage:
    pip install genblaze-core genblaze-openai
    python examples/custom_model_registry.py
"""

from __future__ import annotations

import re

from genblaze_core import Modality
from genblaze_core.models.asset import Asset
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    IntSchema,
    ModelFamily,
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


def scenario_1_register_pricing_on_family_slug() -> None:
    """Add pricing to a slug already covered by a connector family.

    As of 0.3.0 the SDK ships zero hardcoded prices — every published
    slug returns ``cost_usd=None`` until the user registers a pricing
    strategy. ``register_pricing()`` is family-aware: when the slug
    is covered by a connector family (here, OpenAI's ``^dall-e-``
    family), the family's param contracts (aliases, allowlist,
    constraints, extras) are preserved and pricing is layered on top.
    """
    print("=" * 70)
    print("Scenario 1: register pricing on a family-matched slug")
    print("=" * 70)

    reg = DalleProvider.models_default().fork()

    # Before: no pricing shipped for dall-e-3.
    before = reg.get("dall-e-3")
    print(f"  before: dall-e-3 pricing = {before.pricing}    # SDK ships no pricing")

    # Register your rate. The OpenAI dall-e family's param shape
    # (validation, constraints, extras) is preserved automatically —
    # register_pricing() clones the family-resolved spec under the hood
    # so you don't have to redeclare param_aliases or constraints.
    reg.register_pricing("dall-e-3", per_unit(0.040))

    after = reg.get("dall-e-3")
    step = Step(
        provider="openai-dalle",
        model="dall-e-3",
        prompt="a sunset over Big Sur",
        params={"quality": "standard", "size": "1024x1024"},
    )
    cost = _fake_cost(reg, "dall-e-3", step, n_assets=1)
    pricing_label = getattr(after.pricing, "__name__", "registered")
    print(f"  after:  dall-e-3 pricing = {pricing_label}")
    print(f"          dall-e-3 cost (1 img) = ${cost:.4f}")
    print("  Result: pricing applied without touching the family's contracts.")
    print("  See docs/reference/pricing-recipes.md for canonical per-provider rate sheets.")
    print()


def scenario_2_register_family_for_vendor_line() -> None:
    """Teach the SDK about a vendor model line it doesn't ship.

    When a vendor releases a new model family the connector hasn't been
    updated for — or a private-preview line, or an internal fork —
    register a ``ModelFamily`` to route every matching slug through a
    shared spec. One registration covers every current and future
    member of the line.

    Demonstrates ``register_family()``, the headline 0.3.0 power-user
    surface. User families take precedence over connector-shipped
    families; future provider releases that add an overlapping family
    won't override your customization.
    """
    print("=" * 70)
    print("Scenario 2: register a new family for a private-preview line")
    print("=" * 70)

    reg = DalleProvider.models_default().fork()

    reg.register_family(
        ModelFamily(
            name="my-private-gpt-image",
            pattern=re.compile(r"^my-private-gpt-image-"),
            spec_template=ModelSpec(
                model_id="*",  # substituted to the actual slug at resolution time
                modality=Modality.IMAGE,
                pricing=None,  # set per-slug via register_pricing if needed
                param_allowlist=frozenset({"prompt", "size", "n"}),
                param_schemas={"n": IntSchema(min=1, max=4)},
                extras={"private_preview": True},
            ),
            description="Private-preview gpt-image variants from my vendor.",
            example_slugs=("my-private-gpt-image-2025q4", "my-private-gpt-image-experimental"),
        )
    )

    # Every slug matching the pattern resolves through the new family
    # without per-slug registration.
    for slug in ("my-private-gpt-image-2025q4", "my-private-gpt-image-experimental"):
        match = reg.match_family(slug)
        assert match is not None
        print(f"  {slug:42s} → family={match.family.name!r}")

    # Param contracts apply uniformly to every matched slug.
    sample_spec = reg.get("my-private-gpt-image-2025q4")
    print(f"  shared allowlist : {sorted(sample_spec.param_allowlist)}")
    print(f"  shared extras    : {sample_spec.extras}")
    print("  Result: one family declaration covers the whole vendor line.")
    print()


def scenario_3_custom_spec() -> None:
    """Register a brand-new model with validation, pricing, and allowlist.

    For one-off models that don't fit any existing pattern, ``register()``
    takes a full ``ModelSpec`` with per-slug pricing, schemas, and
    allowlist. Use when the model is genuinely unique (a one-time research
    snapshot, a benchmark fixture) rather than part of a vendor line.
    """
    print("=" * 70)
    print("Scenario 3: register a brand-new model with full ModelSpec")
    print("=" * 70)

    reg = DalleProvider.models_default().fork()
    reg.register(
        ModelSpec(
            model_id="my-research-snapshot-v1",
            modality=Modality.IMAGE,
            pricing=per_unit(0.20),
            param_schemas={"n": IntSchema(min=1, max=4)},
            param_allowlist=frozenset({"prompt", "n", "seed"}),
        )
    )
    provider = DalleProvider(models=reg)

    step = Step(
        provider="openai-dalle",
        model="my-research-snapshot-v1",
        prompt="a misty forest at dawn",
        params={"n": 2, "unknown_param": "ignored", "size": "also dropped"},
    )

    payload = provider.prepare_payload(step)
    cost = _fake_cost(provider.models, "my-research-snapshot-v1", step, n_assets=2)

    print(f"  raw params   : {step.params}")
    print(f"  forwarded    : {payload}")
    print("                 # allowlist dropped 'unknown_param' and 'size'")
    print(f"  cost (n=2)   : ${cost:.4f}  # $0.20 × 2 images")
    print("  Result: strict validation + pricing on a one-off model.")
    print()


def main() -> None:
    print()
    print("Custom Model Registry — three levels of runtime control")
    print()
    scenario_1_register_pricing_on_family_slug()
    scenario_2_register_family_for_vendor_line()
    scenario_3_custom_spec()
    print("=" * 70)
    print("See docs/features/model-registry.md for the full surface.")
    print("See docs/guides/migrating-to-0.3.md if upgrading from 0.2.x.")
    print("=" * 70)


if __name__ == "__main__":
    main()
