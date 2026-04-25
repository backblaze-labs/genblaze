"""ModelRegistry — layered, thread-safe store of ``ModelSpec`` entries.

Lookup order: user overrides → package defaults → fallback spec. User reads
are lockless (CPython dict reads are atomic); writes take an ``RLock``.

Intended use:
- Each provider class exposes a ``create_registry()`` classmethod returning
  the package defaults.
- Users register extra models or pricing overrides either globally (mutate
  the default) or per-instance (``fork()`` → ``ReplicateProvider(models=...)``).
- Built-in ``prepare_payload(step)`` runs the full parameter pipeline and
  returns the dict to submit.
"""

from __future__ import annotations

import logging
import threading
import warnings
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.pricing import PricingStrategy
from genblaze_core.providers.spec import FALLBACK_SPEC, ModelSpec

logger = logging.getLogger("genblaze.provider.registry")


class ModelRegistry:
    """Layered per-provider registry.

    Args:
        defaults: Package-supplied specs keyed by ``model_id``.
        fallback: Returned from ``get()`` when a model is unknown. Defaults to
            the permissive ``FALLBACK_SPEC`` (no pricing, pass everything).
        strict_params: If True, unknown keys raise instead of being silently
            dropped when an allowlist is set.
    """

    def __init__(
        self,
        defaults: Mapping[str, ModelSpec] | None = None,
        fallback: ModelSpec = FALLBACK_SPEC,
        *,
        strict_params: bool = False,
    ) -> None:
        self._defaults: dict[str, ModelSpec] = dict(defaults or {})
        self._user: dict[str, ModelSpec] = {}
        self._alias_index: dict[str, str] = {}
        self._deprecated_alias_index: dict[str, str] = {}
        # Tracks deprecated slugs already warned about — one warning per slug
        # per registry lifetime, regardless of how many internal callers
        # (submit, prepare_payload, compute_cost) resolve the same model.
        self._warned_deprecated: set[str] = set()
        self._fallback = fallback
        self._strict = strict_params
        self._lock = threading.RLock()
        self._rebuild_alias_index()

    # --- mutation -----------------------------------------------------------

    def register(self, spec: ModelSpec, *, override: bool = True) -> None:
        """Add or replace a spec in the user layer."""
        with self._lock:
            if not override and (spec.model_id in self._user or spec.model_id in self._defaults):
                raise ValueError(f"Model {spec.model_id!r} already registered; pass override=True")
            self._user[spec.model_id] = spec
            self._rebuild_alias_index()

    def register_pricing(self, model_id: str, pricing: PricingStrategy) -> None:
        """Override pricing for a model without touching other fields.

        If the model exists in either layer, a copy with the new pricing is
        written to the user layer. If unknown, registers a fresh spec with only
        pricing set (applies via the fallback path).
        """
        with self._lock:
            existing = self._user.get(model_id) or self._defaults.get(model_id)
            if existing is None:
                existing = ModelSpec(model_id=model_id)
            updated = _replace(existing, pricing=pricing)
            self._user[model_id] = updated

    def extend(self, specs: Iterable[ModelSpec] | Mapping[str, ModelSpec]) -> None:
        """Bulk-register specs (used by sub-registry composition)."""
        iterable = specs.values() if isinstance(specs, Mapping) else specs
        with self._lock:
            for spec in iterable:
                self._user[spec.model_id] = spec
            self._rebuild_alias_index()

    def fork(self) -> ModelRegistry:
        """Shallow copy-on-write clone — per-instance overrides don't touch the parent."""
        with self._lock:
            clone = ModelRegistry(
                defaults={**self._defaults, **self._user},
                fallback=self._fallback,
                strict_params=self._strict,
            )
            return clone

    # --- read ---------------------------------------------------------------

    def get(self, model_id: str) -> ModelSpec:
        """Return the matching spec; falls back to alias then ``fallback``.

        Emits ``DeprecationWarning`` when the lookup resolves via a
        ``deprecated_aliases`` entry. Never returns None.
        """
        spec = self._user.get(model_id) or self._defaults.get(model_id)
        if spec is not None:
            return spec
        canonical = self._alias_index.get(model_id)
        if canonical is not None:
            spec = self._user.get(canonical) or self._defaults.get(canonical)
            if spec is not None:
                return spec
        deprecated_canonical = self._deprecated_alias_index.get(model_id)
        if deprecated_canonical is not None:
            spec = self._user.get(deprecated_canonical) or self._defaults.get(deprecated_canonical)
            if spec is not None:
                if model_id not in self._warned_deprecated:
                    self._warned_deprecated.add(model_id)
                    warnings.warn(
                        f"Model id {model_id!r} is deprecated; "
                        f"use {deprecated_canonical!r} instead.",
                        DeprecationWarning,
                        stacklevel=2,
                    )
                return spec
        return self._fallback

    def resolve_canonical(self, model_id: str) -> str:
        """Return the canonical id the upstream API expects.

        Equivalent to ``get(model_id).model_id`` except the caller-supplied id
        is passed through verbatim when the lookup only matched the fallback
        spec. Emits ``DeprecationWarning`` for deprecated aliases (delegated
        to ``get``). Connectors whose upstream is case-sensitive should call
        this before putting the slug on the wire.
        """
        spec = self.get(model_id)
        if spec is self._fallback:
            return model_id
        return spec.model_id

    def known(self) -> list[str]:
        """All registered model IDs (user ∪ defaults), sorted."""
        seen = set(self._defaults) | set(self._user)
        return sorted(seen)

    def has(self, model_id: str) -> bool:
        """True if the model_id (or alias) maps to a non-fallback spec."""
        return (
            model_id in self._user
            or model_id in self._defaults
            or model_id in self._alias_index
            or model_id in self._deprecated_alias_index
        )

    def items(self) -> Iterator[tuple[str, ModelSpec]]:
        """Iterate over ``(model_id, spec)`` pairs in deterministic order.

        User overrides take precedence over package defaults, matching ``get``.
        Aliases are not yielded — only canonical ids appear.
        """
        for model_id in self.known():
            spec = self._user.get(model_id) or self._defaults.get(model_id)
            if spec is not None:
                yield model_id, spec

    def __iter__(self) -> Iterator[str]:
        """Iterate over registered model ids (sorted, canonical only)."""
        return iter(self.known())

    def __contains__(self, model_id: object) -> bool:
        """``"slug" in registry`` mirrors ``has()``."""
        return isinstance(model_id, str) and self.has(model_id)

    def __len__(self) -> int:
        """Count of registered canonical ids (excludes aliases and fallback)."""
        return len(self.known())

    # --- pipeline -----------------------------------------------------------

    def prepare_payload(
        self,
        step: Step,
        *,
        base_params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the 9-stage parameter pipeline and return the dict to submit.

        ``base_params`` defaults to a merge of top-level Step fields
        (``prompt``, ``negative_prompt``, ``seed``) with ``step.params``. Pass
        a custom dict to override.

        Pipeline order:
            1. aliases (canonical → native, 1:1)
            2. transformer (many-to-one rewrites)
            3. input_mapping (chain inputs, user wins)
            4. coercers (type/value normalization)
            5. defaults (fill missing)
            6. schemas (validate)
            7. required check
            8. cross-field constraints
            9. allowlist filter
        """
        spec = self.get(step.model)
        params = self._initial_params(step, base_params)

        if spec.is_permissive:
            return params

        # 1. aliases — canonical → native (non-destructive when native exists)
        if spec.param_aliases:
            for canonical, native in spec.param_aliases.items():
                if canonical in params and native not in params:
                    params[native] = params.pop(canonical)
                elif canonical in params and native in params:
                    logger.debug(
                        "Both canonical %r and native %r supplied for %s; keeping native",
                        canonical,
                        native,
                        step.model,
                    )
                    params.pop(canonical, None)

        # 2. transformer — many-to-one / arbitrary rewrites
        if spec.param_transformer is not None:
            params = spec.param_transformer(params)

        # 3. input mapping — user params win over chained inputs
        if spec.input_mapping is not None and step.inputs:
            chain = spec.input_mapping(step.inputs)
            for k, v in chain.items():
                params.setdefault(k, v)

        # 4. coercers — per-key type coercion
        for key, coerce in spec.param_coercers.items():
            if key in params:
                try:
                    params[key] = coerce(params[key])
                except Exception as exc:
                    raise ProviderError(
                        f"Failed to coerce {key!r}: {exc}",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    ) from exc

        # 5. defaults — fill missing
        for key, default in spec.param_defaults.items():
            params.setdefault(key, default)

        # 6. schemas — validate present fields
        for key, schema in spec.param_schemas.items():
            if key in params:
                schema.validate(key, params[key])

        # 7. required — after defaults
        missing = spec.param_required - params.keys()
        if missing:
            raise ProviderError(
                f"Missing required parameters for {step.model}: {sorted(missing)}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )

        # 8. cross-field constraints
        for check in spec.param_constraints:
            check(params)

        # 9. allowlist filter
        if spec.param_allowlist is not None:
            extras = [k for k in params if k not in spec.param_allowlist]
            if extras:
                if self._strict:
                    raise ProviderError(
                        f"Unknown parameters for {step.model}: {sorted(extras)}",
                        error_code=ProviderErrorCode.INVALID_INPUT,
                    )
                # WARNING (not INFO) so silently-dropped params surface in
                # production logs — these have caused a class of "looks fine,
                # output is wrong" bugs (Pixverse `quality`, Bria `mask_url`,
                # voice clone `language`). Callers that want hard failure on
                # unknown keys can opt into ``ModelRegistry(strict_params=True)``.
                logger.warning(
                    "Dropping non-allowlisted params for %s: %s", step.model, sorted(extras)
                )
            params = {k: v for k, v in params.items() if k in spec.param_allowlist}

        return params

    # --- internals ----------------------------------------------------------

    def _initial_params(self, step: Step, base: Mapping[str, Any] | None) -> dict[str, Any]:
        if base is not None:
            return dict(base)
        out: dict[str, Any] = {}
        if step.prompt is not None:
            out["prompt"] = step.prompt
        if step.negative_prompt is not None:
            out["negative_prompt"] = step.negative_prompt
        if step.seed is not None:
            out["seed"] = step.seed
        # step.params wins over Step fields when they collide (explicit user intent)
        out.update(step.params)
        return out

    def _rebuild_alias_index(self) -> None:
        idx: dict[str, str] = {}
        dep_idx: dict[str, str] = {}
        for layer in (self._defaults, self._user):
            for model_id, spec in layer.items():
                for alias in spec.aliases:
                    idx[alias] = model_id
                for alias in spec.deprecated_aliases:
                    dep_idx[alias] = model_id
        self._alias_index = idx
        self._deprecated_alias_index = dep_idx


# Small helper: dataclasses.replace but we control imports tightly here.
def _replace(spec: ModelSpec, **changes: Any) -> ModelSpec:
    from dataclasses import replace

    return replace(spec, **changes)


# Module-level empty registry used as a fast default on BaseProvider.
EMPTY_REGISTRY = ModelRegistry()


def compute_cost(
    registry: ModelRegistry,
    step: Step,
    *,
    assets: Sequence[Asset] | None = None,
    provider_payload: Mapping[str, Any] | None = None,
) -> float | None:
    """Compute cost via the spec's pricing strategy. Returns None if no strategy."""
    spec = registry.get(step.model)
    if spec.pricing is None:
        return None
    from genblaze_core.providers.pricing import PricingContext

    ctx = PricingContext(
        step=step,
        assets=tuple(assets if assets is not None else step.assets),
        provider_payload=provider_payload
        if provider_payload is not None
        else step.provider_payload,
    )
    try:
        return spec.pricing(ctx)
    except Exception:
        logger.exception("Pricing strategy for %s raised — cost unavailable", step.model)
        return None
