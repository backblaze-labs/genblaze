"""Pricing context and packaged strategies.

``PricingStrategy`` is ``Callable[[PricingContext], float | None]``. Packaged
helpers cover the common shapes; users write bespoke strategies for anything
else.

All strategies are pure and synchronous — no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from genblaze_core.models.asset import Asset
from genblaze_core.models.step import Step


@dataclass(frozen=True, slots=True)
class PricingContext:
    """Snapshot passed to a ``PricingStrategy``.

    Contains the completed step, the emitted assets (with any duration /
    dimension metadata), and the raw provider payload. Strategies read
    whichever they need.

    Computed fields (``output_count``, ``output_duration_s``) are cheap; a
    frozen slotted dataclass has no ``__dict__`` for ``cached_property``, so
    we keep them as plain properties.
    """

    step: Step
    assets: Sequence[Asset] = field(default_factory=tuple)
    provider_payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def output_count(self) -> int:
        """Number of assets emitted by this step."""
        return len(self.assets)

    @property
    def output_duration_s(self) -> float | None:
        """Sum of asset durations in seconds, or None if no asset has a duration."""
        total = 0.0
        found = False
        for a in self.assets:
            if a.duration is not None:
                total += a.duration
                found = True
        return total if found else None


PricingStrategy = Callable[[PricingContext], float | None]
"""Callable that computes USD cost from a ``PricingContext``."""


# --- Packaged strategies -----------------------------------------------------


def per_unit(rate: float) -> PricingStrategy:
    """Flat rate per emitted asset."""

    def _strategy(ctx: PricingContext) -> float | None:
        return rate * ctx.output_count if ctx.output_count else None

    return _strategy


def per_input_chars(rate: float, per: int = 1000) -> PricingStrategy:
    """Per-character pricing on the input prompt. ``per=1000`` → USD per 1K chars."""

    def _strategy(ctx: PricingContext) -> float | None:
        text = ctx.step.prompt or ""
        if not text:
            return None
        return (len(text) / per) * rate

    return _strategy


def per_output_second(rate: float) -> PricingStrategy:
    """Per-second pricing on total output duration (needs ``Asset.duration``)."""

    def _strategy(ctx: PricingContext) -> float | None:
        dur = ctx.output_duration_s
        return dur * rate if dur is not None else None

    return _strategy


def per_response_metric(extract: Callable[[PricingContext], float | None]) -> PricingStrategy:
    """Extract a number from the response (e.g. Replicate compute time) and return it.

    ``extract`` returns the USD amount directly — the caller multiplies in the
    closure. Example::

        per_response_metric(lambda ctx: ctx.provider_payload.get("predict_time", 0) * 2.25e-4)
    """

    def _strategy(ctx: PricingContext) -> float | None:
        return extract(ctx)

    return _strategy


def tiered(
    table: Mapping[tuple[Hashable, ...], float],
    key: Callable[[PricingContext], tuple[Hashable, ...]],
) -> PricingStrategy:
    """Table lookup keyed by a tuple extracted from the context.

    Example (DALL-E)::

        tiered(
            {("standard", "1024x1024"): 0.040, ("hd", "1024x1024"): 0.080},
            key=lambda ctx: (
                ctx.step.params.get("quality", "standard"),
                ctx.step.params.get("size", "1024x1024"),
            ),
        )

    Result is multiplied by ``output_count``.
    """

    def _strategy(ctx: PricingContext) -> float | None:
        k = key(ctx)
        per_unit_cost = table.get(k)
        if per_unit_cost is None:
            return None
        n = ctx.output_count or 1
        return per_unit_cost * n

    return _strategy


def bucketed_by_duration(
    buckets: Sequence[tuple[tuple[float, float], float]],
) -> PricingStrategy:
    """Bucketed pricing by output duration.

    ``buckets`` is a list of ``((min_inclusive, max_exclusive), price)`` tuples.
    Uses ``output_duration_s``; returns None if unavailable.

    Example (ElevenLabs SFX)::

        bucketed_by_duration([
            ((0.0, 5.0), 0.20),
            ((5.0, 10.0), 0.30),
            ((10.0, 30.1), 0.50),
        ])
    """

    def _strategy(ctx: PricingContext) -> float | None:
        dur = ctx.output_duration_s
        if dur is None:
            # Fall back to requested duration if provided — common for audio SFX
            dur = _float_or_none(ctx.step.params.get("duration_seconds")) or _float_or_none(
                ctx.step.params.get("duration")
            )
            if dur is None:
                return None
        for (lo, hi), price in buckets:
            if lo <= dur < hi:
                return price
        return None

    return _strategy


def by_param(
    param: str,
    table: Mapping[Any, float],
    *,
    default: float | None = None,
    per_output: bool = True,
) -> PricingStrategy:
    """Lookup pricing by a single step.params value. Multiplies by output_count by default."""

    def _strategy(ctx: PricingContext) -> float | None:
        v = ctx.step.params.get(param)
        price = table.get(v, default)
        if price is None:
            return None
        if per_output:
            return price * (ctx.output_count or 1)
        return price

    return _strategy


def by_model_and_param(
    param: str,
    table: Mapping[tuple[str, Any], float],
    *,
    per_output: bool = True,
) -> PricingStrategy:
    """Lookup keyed by ``(step.model, params[param])``. Runway pattern."""

    def _strategy(ctx: PricingContext) -> float | None:
        v = ctx.step.params.get(param)
        price = table.get((ctx.step.model, v))
        if price is None:
            return None
        return price * (ctx.output_count or 1) if per_output else price

    return _strategy


def first_match(*strategies: PricingStrategy) -> PricingStrategy:
    """Return the first non-None cost from a sequence of strategies."""

    def _strategy(ctx: PricingContext) -> float | None:
        for s in strategies:
            v = s(ctx)
            if v is not None:
                return v
        return None

    return _strategy


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
