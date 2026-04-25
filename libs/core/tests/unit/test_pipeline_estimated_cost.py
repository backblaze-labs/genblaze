"""Coverage for ``Pipeline.estimated_cost()`` and ``BaseProvider.estimate_cost``.

The estimator must:
- compose per-step costs into a Decimal total,
- return ``None`` when any step's pricing is unknown (so apps show "varies"
  rather than a misleading partial), and
- pass ``params["duration"]`` to per-second pricing strategies.
"""

from __future__ import annotations

from decimal import Decimal

from genblaze_core.models.enums import Modality
from genblaze_core.pipeline.pipeline import Pipeline
from genblaze_core.providers import ModelRegistry, ModelSpec, per_unit
from genblaze_core.providers.pricing import PricingContext
from genblaze_core.testing import MockProvider


def _provider_with_priced_models(rates: dict[str, float]) -> MockProvider:
    """MockProvider whose registry has flat per-unit pricing for each model."""
    p = MockProvider()
    reg = ModelRegistry(
        defaults={
            mid: ModelSpec(model_id=mid, pricing=per_unit(rate)) for mid, rate in rates.items()
        }
    )
    p._models = reg
    return p


def test_provider_estimate_cost_returns_decimal() -> None:
    p = _provider_with_priced_models({"m1": 0.05})
    assert p.estimate_cost("m1") == Decimal("0.05")
    assert p.estimate_cost("m1", n=4) == Decimal("0.2")


def test_provider_estimate_cost_unknown_returns_none() -> None:
    p = _provider_with_priced_models({"m1": 0.05})
    assert p.estimate_cost("missing-slug") is None


def test_pipeline_estimated_cost_sums_steps() -> None:
    a = _provider_with_priced_models({"a1": 0.10})
    b = _provider_with_priced_models({"b1": 0.25})
    pipe = (
        Pipeline("test")
        .step(a, model="a1", modality=Modality.IMAGE)
        .step(b, model="b1", modality=Modality.VIDEO)
    )
    assert pipe.estimated_cost() == Decimal("0.35")


def test_pipeline_estimated_cost_returns_none_when_any_step_unknown() -> None:
    a = _provider_with_priced_models({"a1": 0.10})
    unknown = MockProvider()  # default registry has no priced models
    pipe = (
        Pipeline("test")
        .step(a, model="a1", modality=Modality.IMAGE)
        .step(unknown, model="any", modality=Modality.IMAGE)
    )
    assert pipe.estimated_cost() is None


def test_estimate_cost_passes_duration_to_per_second_pricing() -> None:
    """Per-second strategies should pick up ``duration`` from params."""

    def per_second(rate: float):
        def _strategy(ctx: PricingContext) -> float | None:
            dur = ctx.step.params.get("duration")
            try:
                return rate * float(dur) if dur is not None else None
            except (TypeError, ValueError):
                return None

        return _strategy

    p = MockProvider()
    p._models = ModelRegistry(defaults={"v1": ModelSpec(model_id="v1", pricing=per_second(0.05))})
    assert p.estimate_cost("v1", params={"duration": 10}) == Decimal("0.5")
    assert p.estimate_cost("v1") is None  # missing duration → None
