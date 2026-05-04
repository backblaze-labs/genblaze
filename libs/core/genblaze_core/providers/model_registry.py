"""ModelRegistry — layered, thread-safe store of ``ModelSpec`` entries.

Lookup order (post-decoupling, V2):
1. user spec        — exact-match registration via ``register(spec)``
2. user family      — ``register_family(family)``, prepended (highest priority)
3. provider family  — connector-shipped ``provider_families=(...)`` tuple
4. legacy defaults  — transitional ``defaults={}`` shim (removed in PR #13)
5. discovery cache  — peek-only here (no fetch); NATIVE providers consult it
6. fallback spec    — permissive pass-through

User reads through the family scan are lock-free against the immutable
``_provider_families`` tuple; the user-family list is RLock-guarded with
snapshot reads.

Intended use:
- Each provider class exposes a ``create_registry()`` classmethod returning
  a registry built with ``provider_families=(...)`` and (during the
  migration window) optionally ``defaults={...}``.
- Users register extra slugs / families / pricing either globally (mutate
  the cached default) or per-instance (``fork()`` → ``Provider(models=...)``).
- Built-in ``prepare_payload(step)`` runs the full parameter pipeline.

See ``docs/exec-plans/active/model-registry-decoupling.md``.
"""

from __future__ import annotations

import logging
import re
import threading
import warnings
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.discovery import DiscoveryStatus, _DiscoveryCache
from genblaze_core.providers.family import (
    MAX_PROVIDER_FAMILIES,
    DiscoverySupport,
    FamilyMatch,
    ModelFamily,
)
from genblaze_core.providers.pricing import PricingStrategy
from genblaze_core.providers.spec import FALLBACK_SPEC, ModelSpec
from genblaze_core.providers.validation import (
    ValidationOutcome,
    ValidationResult,
    ValidationSource,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger("genblaze.provider.registry")


class ModelRegistry:
    """Layered per-provider registry.

    Args:
        defaults: **Transitional shim** (removed in PR #13). Connector-shipped
            specs keyed by ``model_id``. Resolves between provider families
            and the discovery cache. New connectors should use
            ``provider_families=`` instead.
        fallback: Returned from ``get()`` when a model is unknown. Defaults to
            the permissive ``FALLBACK_SPEC`` (no pricing, pass everything).
        provider_families: Connector-shipped pattern-keyed param-shape rules.
            Frozen post-construction; ordered from most-specific to
            least-specific (first-match-wins). Capped at
            :data:`~genblaze_core.providers.family.MAX_PROVIDER_FAMILIES`.
        discovery_cache: Optional ``_DiscoveryCache`` for connectors that
            implement ``BaseProvider.discover_models()``. The registry
            consults the cache via :meth:`peek` only — it never issues
            fetches itself; that is the provider's responsibility.
        strict_params: If True, unknown keys raise instead of being silently
            dropped when an allowlist is set.
    """

    def __init__(
        self,
        defaults: Mapping[str, ModelSpec] | None = None,
        fallback: ModelSpec = FALLBACK_SPEC,
        *,
        provider_families: Sequence[ModelFamily] = (),
        discovery_cache: _DiscoveryCache | None = None,
        strict_params: bool = False,
    ) -> None:
        if len(provider_families) > MAX_PROVIDER_FAMILIES:
            raise ValueError(
                f"Provider shipped {len(provider_families)} families; cap is "
                f"{MAX_PROVIDER_FAMILIES}. Consolidate patterns or split the "
                f"registry by modality."
            )
        self._defaults: dict[str, ModelSpec] = dict(defaults or {})
        self._user: dict[str, ModelSpec] = {}
        self._provider_families: tuple[ModelFamily, ...] = tuple(provider_families)
        self._user_families: list[ModelFamily] = []
        self._discovery_cache: _DiscoveryCache | None = discovery_cache
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

    def register_family(self, family: ModelFamily) -> None:
        """Prepend a user-defined family to the resolution chain.

        User families are checked before provider families, so callers can
        override or extend connector-shipped patterns without forking the
        registry. Order within ``_user_families`` is insertion order with
        the most-recent registration at position 0 (highest priority).
        """
        with self._lock:
            self._user_families.insert(0, family)

    def fork(self) -> ModelRegistry:
        """Shallow copy-on-write clone — per-instance overrides don't touch the parent."""
        with self._lock:
            clone = ModelRegistry(
                defaults={**self._defaults, **self._user},
                fallback=self._fallback,
                provider_families=self._provider_families,
                discovery_cache=self._discovery_cache,
                strict_params=self._strict,
            )
            # User families are part of the user layer — copy them over.
            clone._user_families = list(self._user_families)
            return clone

    # --- read ---------------------------------------------------------------

    def get(self, model_id: str) -> ModelSpec:
        """Return the matching spec; falls back to family → alias → ``fallback``.

        Resolution order: user spec → legacy defaults shim → user family →
        provider family → alias → deprecated alias → fallback. Emits
        ``DeprecationWarning`` when the lookup resolves via a
        ``deprecated_aliases`` entry. Never returns ``None``.
        """
        spec = self._user.get(model_id) or self._defaults.get(model_id)
        if spec is not None:
            return spec
        # Family resolution — second tier; pattern match returns the
        # spec_template with model_id substituted.
        match = self.match_family(model_id)
        if match is not None:
            return match.spec
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

    def match_family(self, model_id: str) -> FamilyMatch | None:
        """Return the first matching family or ``None``.

        User families take precedence over provider families. Within each
        layer, families are scanned in order — connectors must list their
        families from most-specific to least-specific.
        """
        # Snapshot the user list under the lock so concurrent registration
        # doesn't tear our scan. Provider families are an immutable tuple,
        # so a lock-free read is safe there.
        with self._lock:
            user_snapshot = list(self._user_families)
        for family in user_snapshot:
            if family.matches(model_id):
                return FamilyMatch(family=family, spec=family.resolve(model_id))
        for family in self._provider_families:
            if family.matches(model_id):
                return FamilyMatch(family=family, spec=family.resolve(model_id))
        return None

    @property
    def families(self) -> tuple[ModelFamily, ...]:
        """All registered families (user first, then provider). Snapshot copy."""
        with self._lock:
            return (*self._user_families, *self._provider_families)

    def validate(
        self,
        model_id: str,
        *,
        discovery_support: DiscoverySupport = DiscoverySupport.NONE,
    ) -> ValidationResult:
        """Non-network validation — what the SDK can say without a fetch.

        The provider's ``validate_model()`` orchestrates network operations
        (discovery refresh, family probe) and may call this method again
        afterwards for the post-network answer. Use directly when you want
        a fast, deterministic check without round-trips.

        See :class:`~genblaze_core.providers.family.DiscoverySupport` for
        the full outcome matrix.
        """
        # 1. user spec — strongest signal regardless of provider class.
        if model_id in self._user:
            return ValidationResult.ok_authoritative(ValidationSource.USER)
        if model_id in self._defaults:
            # Legacy defaults shim: a connector author's curated spec is
            # treated as authoritative — it's the same shape user
            # registration takes after migration.
            return ValidationResult.ok_authoritative(
                ValidationSource.USER,
                detail="from legacy defaults shim (transitional)",
            )

        # 2. Family match → consult discovery cache (peek, no fetch).
        match = self.match_family(model_id)
        cached = self._discovery_cache.peek() if self._discovery_cache else None
        if match is not None:
            family_name = match.family.name
            if (
                discovery_support is DiscoverySupport.NATIVE
                and cached is not None
                and cached.status is DiscoveryStatus.OK
            ):
                if model_id in cached.slugs:
                    return ValidationResult.ok_authoritative(
                        ValidationSource.DISCOVERY,
                        family_name=family_name,
                    )
                return ValidationResult.not_found(
                    ValidationSource.DISCOVERY,
                    family_name=family_name,
                    detail=f"slug not present in fresh upstream catalog",
                    suggested_slugs=_nearest_slugs(model_id, cached.slugs),
                )
            # PARTIAL/NONE without a probe (registry can't probe — provider
            # does that): provisional. The mark-dead unstable_examples
            # signal also lands here so callers see it before the wire.
            detail: str | None = None
            if model_id in match.family.unstable_examples:
                detail = "known_unstable; verify with discover_models()"
            return ValidationResult.ok_provisional(
                family_name=family_name,
                detail=detail,
            )

        # 3. No family match. NATIVE provider with a fresh catalog can
        # still answer NOT_FOUND.
        if (
            discovery_support is DiscoverySupport.NATIVE
            and cached is not None
            and cached.status is DiscoveryStatus.OK
        ):
            if model_id in cached.slugs:
                return ValidationResult.ok_authoritative(
                    ValidationSource.DISCOVERY,
                    detail="discovered without family match",
                )
            return ValidationResult.not_found(
                ValidationSource.DISCOVERY,
                detail="slug not present in fresh upstream catalog",
                suggested_slugs=_nearest_slugs(model_id, cached.slugs),
            )

        # 4. Permissive fallback — we can't say anything authoritative.
        return ValidationResult.unknown_permissive()

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
        """All registered / discoverable model IDs, sorted.

        Includes:
        - user-registered slugs and legacy defaults,
        - ``example_slugs`` from every registered family (documentation hint),
        - the most-recent discovery cache snapshot (if any).

        **Documentation grade, not a contract.** Family-matched slugs not
        in any of these sources still resolve through ``get()`` and
        ``validate()``; this method exists for IDE autocomplete, doc
        generation, and capability advertising.
        """
        seen: set[str] = set(self._defaults) | set(self._user)
        for family in self._provider_families:
            seen.update(family.example_slugs)
        with self._lock:
            for family in self._user_families:
                seen.update(family.example_slugs)
        cached = self._discovery_cache.peek() if self._discovery_cache else None
        if cached is not None and cached.status is DiscoveryStatus.OK:
            seen.update(cached.slugs)
        return sorted(seen)

    def has(self, model_id: str) -> bool:
        """True if the model_id (or alias / family pattern) is non-fallback.

        Coherent with ``__contains__`` and ``validate(...).is_ok``: returns
        ``True`` for any slug that resolves via user spec, legacy defaults,
        family pattern, alias, or deprecated alias. Returns ``False`` for
        the permissive fallback.
        """
        if (
            model_id in self._user
            or model_id in self._defaults
            or model_id in self._alias_index
            or model_id in self._deprecated_alias_index
        ):
            return True
        return self.match_family(model_id) is not None

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


def _nearest_slugs(
    model_id: str,
    candidates: Iterable[str],
    *,
    max_suggestions: int = 3,
) -> tuple[str, ...]:
    """Return up to ``max_suggestions`` slugs from ``candidates`` that look
    similar to ``model_id``. Used to populate ``ValidationResult.suggested_slugs``
    on ``NOT_FOUND`` outcomes so error messages can say "Did you mean…?".

    Uses ``difflib.get_close_matches`` — Levenshtein-ratio shortest-path on
    the candidate set. Cheap (~O(n) for typical n ≤ 100), no heuristics
    beyond what stdlib offers. The result is informational only; callers
    should not depend on specific suggestions.
    """
    import difflib

    return tuple(
        difflib.get_close_matches(model_id, list(candidates), n=max_suggestions, cutoff=0.5)
    )


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
