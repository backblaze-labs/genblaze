"""ModelRegistry — layered, thread-safe store of ``ModelSpec`` entries.

Lookup order:
1. user spec        — exact-match registration via ``register(spec)``
2. user family      — ``register_family(family)``, prepended (highest priority)
3. provider family  — connector-shipped ``provider_families=(...)`` tuple
4. discovery cache  — peek-only here (no fetch); NATIVE providers consult it
5. fallback spec    — permissive pass-through

User reads through the family scan are lock-free against the immutable
``_provider_families`` tuple; the user-family list is RLock-guarded with
snapshot reads.

Intended use:
- Each provider class exposes a ``create_registry()`` classmethod returning
  a registry built with ``provider_families=(...)``.
- Users register extra slugs / families / pricing either globally (mutate
  the cached default) or per-instance (``fork()`` → ``Provider(models=...)``).
- Built-in ``prepare_payload(step)`` runs the full parameter pipeline.

See ``docs/exec-plans/active/model-registry-decoupling.md``.
"""

from __future__ import annotations

import logging
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
    MAX_USER_FAMILIES,
    DiscoverySupport,
    FamilyMatch,
    ModelFamily,
)
from genblaze_core.providers.pricing import PricingStrategy
from genblaze_core.providers.spec import FALLBACK_SPEC, ModelSpec
from genblaze_core.providers.validation import (
    ValidationResult,
    ValidationSource,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger("genblaze.provider.registry")


class ModelRegistry:
    """Layered per-provider registry.

    Args:
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
        unstable_slugs: Slugs the connector flags as known-unstable
            (suspected dead, deprecated upstream, etc.) without
            requiring them to belong to a dedicated ``ModelFamily``.
            Unioned with ``family.unstable_examples`` on construction
            into a single O(1)-lookup ``frozenset``. ``validate()``
            surfaces these as ``OK_PROVISIONAL`` with
            ``detail="known_unstable"``. Replaces the historical
            "catch-all family carrying unstable_examples" pattern (the
            family was a code smell — its only purpose was to carry the
            list).
        strict_params: If True, unknown keys raise instead of being silently
            dropped when an allowlist is set.
    """

    def __init__(
        self,
        fallback: ModelSpec = FALLBACK_SPEC,
        *,
        provider_families: Sequence[ModelFamily] = (),
        discovery_cache: _DiscoveryCache | None = None,
        unstable_slugs: Iterable[str] = (),
        strict_params: bool = False,
    ) -> None:
        if len(provider_families) > MAX_PROVIDER_FAMILIES:
            raise ValueError(
                f"Provider shipped {len(provider_families)} families; cap is "
                f"{MAX_PROVIDER_FAMILIES}. Consolidate patterns or split the "
                f"registry by modality."
            )
        # Union the registry-level unstable_slugs with every family's own
        # unstable_examples so callers have a single O(1) source of truth.
        # Both routes (registry-level and family-level) produce the same
        # OK_PROVISIONAL/known_unstable signal in validate().
        _unstable: set[str] = set(unstable_slugs)
        for family in provider_families:
            _unstable.update(family.unstable_examples)
        self._unstable_slugs: frozenset[str] = frozenset(_unstable)
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
        # Mirror of ``_warned_deprecated`` for the ``ModelFamily.canonical_slug``
        # rewrite path. Keyed by ``(family.name, input_slug)`` so the same
        # nudge fires once per non-canonical input per registry lifetime.
        # Carried through ``with_user_overlay`` so per-request forks inherit
        # the dedup state (otherwise a multi-tenant deployment forking on
        # every request would re-log indefinitely).
        self._warned_canonical_rewrite: set[tuple[str, str]] = set()
        self._fallback = fallback
        self._strict = strict_params
        self._lock = threading.RLock()
        self._rebuild_alias_index()

    # --- mutation -----------------------------------------------------------

    def register(self, spec: ModelSpec, *, override: bool = True) -> None:
        """Add or replace a spec in the user layer."""
        with self._lock:
            if not override and spec.model_id in self._user:
                raise ValueError(f"Model {spec.model_id!r} already registered; pass override=True")
            self._user[spec.model_id] = spec
            self._rebuild_alias_index()

    def register_pricing(self, model_id: str, pricing: PricingStrategy) -> None:
        """Override pricing for a model without touching other fields.

        Resolution order for the base spec (before applying pricing):

        1. **User layer** — if the slug is already registered, the
           existing user spec is the base; only ``pricing`` is replaced.
        2. **Family resolution** — if the slug matches a connector
           family but isn't yet user-registered, the family-resolved
           spec (with all its param contracts: aliases, schemas,
           coercers, allowlist, input_mapping, extras) is the base.
           This preserves the family's parameter shape — the user gets
           pricing layered onto the family's full pipeline rather than
           a bare permissive spec.
        3. **Bare ``ModelSpec``** — if neither user nor family covers
           the slug, a fresh spec with only ``pricing`` set is
           registered. The slug then resolves through the registry's
           fallback path with this user-supplied pricing.

        Without step 2, calling ``register_pricing("nvidia/cosmos-...")``
        on a PARTIAL connector would silently drop the family's param
        contracts, because the user layer always wins over family
        resolution in ``get()``.
        """
        with self._lock:
            existing = self._user.get(model_id)
            if existing is None:
                # Try family resolution to preserve param contracts.
                # ``match_family`` returns a ``FamilyMatch`` whose
                # ``spec`` is the family's ``spec_template`` with
                # ``model_id`` substituted; if no family matches,
                # fall back to a bare spec.
                match = self.match_family(model_id)
                existing = match.spec if match is not None else ModelSpec(model_id=model_id)
            updated = _replace(existing, pricing=pricing)
            self._user[model_id] = updated
            self._rebuild_alias_index()

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

        ``family.unstable_examples`` is unioned into ``_unstable_slugs``
        so a family registered post-construction still surfaces the
        ``known_unstable`` hint via ``validate()``. Without this union
        the registry would observe stale state — the family would carry
        unstable_examples but ``validate()`` wouldn't see them.

        **Per-layer cap.** ``MAX_USER_FAMILIES`` (default 32) bounds
        the user layer's linear-scan cost. The connector-shipped cap
        (``MAX_PROVIDER_FAMILIES``) is *not* counted here — a connector
        at its provider cap does not block users from registering
        their own families. Total scan cost stays under
        ``MAX_PROVIDER_FAMILIES + MAX_USER_FAMILIES`` patterns.

        Raises:
            ValueError: when the user-family count is already at
                ``MAX_USER_FAMILIES``. The error message names the
                user layer explicitly so the cause is clear.
        """
        with self._lock:
            if len(self._user_families) >= MAX_USER_FAMILIES:
                raise ValueError(
                    f"Registry already has {len(self._user_families)} user families; "
                    f"cap is {MAX_USER_FAMILIES}. Consolidate patterns or call "
                    f"fork() to start fresh."
                )
            self._user_families.insert(0, family)
            if family.unstable_examples:
                self._unstable_slugs = self._unstable_slugs | frozenset(family.unstable_examples)

    def fork(self) -> ModelRegistry:
        """Shallow copy-on-write clone — per-instance overrides don't touch the parent.

        Carries forward ``_unstable_slugs`` so clones surface the same
        ``known_unstable`` hints as the parent. Without this, slugs added
        via the parent's ``unstable_slugs=`` constructor kwarg (i.e.,
        registry-level orphans not in any family) would be silently
        dropped by the fork.
        """
        with self._lock:
            clone = ModelRegistry(
                fallback=self._fallback,
                provider_families=self._provider_families,
                # Fork the discovery cache rather than sharing by reference:
                # a refresh on the clone must not blow out the parent's (or
                # sibling forks') warm cache. The fetcher closure is shared,
                # so the clone hits the same upstream with the same auth —
                # only cache *state* is isolated.
                discovery_cache=(self._discovery_cache.fork() if self._discovery_cache else None),
                strict_params=self._strict,
                # Pass the unioned set; the constructor will re-union with
                # family.unstable_examples (idempotent — frozenset union).
                unstable_slugs=self._unstable_slugs,
            )
            # Carry the user layer over. ``extend`` performs a single bulk
            # write under its own lock and does one alias-index rebuild —
            # cheaper than N ``register()`` calls. Direct dict assignment
            # would skip the rebuild entirely; extend is the right shape.
            if self._user:
                clone.extend(self._user.values())
            # User families are part of the user layer — copy them over.
            # Their unstable_examples are already in self._unstable_slugs
            # which we passed above, so this re-population doesn't lose
            # any signal.
            clone._user_families = list(self._user_families)
            # Carry the once-per-slug deprecation-warning state. Without
            # this, a fork-per-request multi-tenant deployment would
            # re-warn on every request for the same deprecated alias —
            # exactly the spam ``_warned_deprecated`` was designed to
            # prevent. The set is shallow-copied so future warnings on
            # the clone don't leak back to the parent.
            clone._warned_deprecated = set(self._warned_deprecated)
            # Same fork-carryover for the canonical_slug INFO dedup — without
            # this, a fork-per-request multi-tenant deployment would re-log
            # on every request for the same non-canonical input.
            clone._warned_canonical_rewrite = set(self._warned_canonical_rewrite)
            return clone

    # --- read ---------------------------------------------------------------

    def get(self, model_id: str) -> ModelSpec:
        """Return the matching spec; falls back to family → alias → ``fallback``.

        Resolution order: user spec → user family → provider family → alias
        → deprecated alias → fallback. Emits ``DeprecationWarning`` when the
        lookup resolves via a ``deprecated_aliases`` entry. Never returns
        ``None``.
        """
        spec = self._user.get(model_id)
        if spec is not None:
            return spec
        # Family resolution — second tier; pattern match returns the
        # spec_template with model_id substituted.
        match = self.match_family(model_id)
        if match is not None:
            return match.spec
        canonical = self._alias_index.get(model_id)
        if canonical is not None:
            spec = self._user.get(canonical)
            if spec is not None:
                return spec
        deprecated_canonical = self._deprecated_alias_index.get(model_id)
        if deprecated_canonical is not None:
            spec = self._user.get(deprecated_canonical)
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

        When the matched family declares a ``canonical_slug`` transform
        and the rewrite changes the input, emits a one-time INFO log
        per ``(family, input)`` so callers know they're using a non-
        canonical form (e.g. lowercase ``"veo3"`` against a PascalCase
        wire family). Dedup is instance-level via ``_warned_canonical_rewrite``
        and is fork-safe (the set is shallow-copied on ``with_user_overlay``).
        """
        # Snapshot the user list under the lock so concurrent registration
        # doesn't tear our scan. Provider families are an immutable tuple,
        # so a lock-free read is safe there.
        with self._lock:
            user_snapshot = list(self._user_families)
        for family in user_snapshot:
            if family.matches(model_id):
                spec = family.resolve(model_id)
                self._maybe_log_canonical_rewrite(family, model_id, spec.model_id)
                return FamilyMatch(family=family, spec=spec)
        for family in self._provider_families:
            if family.matches(model_id):
                spec = family.resolve(model_id)
                self._maybe_log_canonical_rewrite(family, model_id, spec.model_id)
                return FamilyMatch(family=family, spec=spec)
        return None

    def _maybe_log_canonical_rewrite(
        self, family: ModelFamily, input_slug: str, wire_slug: str
    ) -> None:
        """Emit a one-time INFO when ``canonical_slug`` rewrote the caller's
        input. No-op when the family has no transform or when the rewrite
        is the identity. Dedup is keyed by ``(family.name, input_slug)``
        so the same migrate-your-call-site nudge doesn't spam the logs.
        """
        if input_slug == wire_slug:
            return
        key = (family.name, input_slug)
        if key in self._warned_canonical_rewrite:
            return
        self._warned_canonical_rewrite.add(key)
        logger.info(
            "%s canonical-slug rewrite: %r → %r. Update call sites to the "
            "canonical form to avoid this log line.",
            family.name,
            input_slug,
            wire_slug,
        )

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

        # 2. Family match → consult discovery cache (peek, no fetch).
        match = self.match_family(model_id)
        cached = self._discovery_cache.peek() if self._discovery_cache else None
        if match is not None:
            family_name = match.family.name
            # When the family declares a ``canonical_slug`` transform,
            # normalize the input before comparing against the discovery
            # cache. Otherwise a user passing ``"veo3"`` against a family
            # whose wire form is ``"Veo3"`` would get NOT_FOUND from the
            # cache check even though ``submit()`` would happily resolve
            # via ``resolve_canonical()`` and succeed on the wire. The
            # cache is normalized to wire forms; comparison must be too.
            normalized = (
                match.family.canonical_slug(model_id) if match.family.canonical_slug else model_id
            )
            if (
                discovery_support is DiscoverySupport.NATIVE
                and cached is not None
                and cached.status is DiscoveryStatus.OK
            ):
                if normalized in cached.slugs:
                    return ValidationResult.ok_authoritative(
                        ValidationSource.DISCOVERY,
                        family_name=family_name,
                    )
                return ValidationResult.not_found(
                    ValidationSource.DISCOVERY,
                    family_name=family_name,
                    detail="slug not present in fresh upstream catalog",
                    suggested_slugs=_nearest_slugs(normalized, cached.slugs),
                )
            # PARTIAL/NONE without a probe (registry can't probe — provider
            # does that): provisional. The mark-dead unstable_examples
            # signal also lands here so callers see it before the wire.
            # _unstable_slugs is the unioned set (family.unstable_examples
            # ∪ registry-level unstable_slugs) — O(1) frozenset lookup.
            detail: str | None = None
            if model_id in self._unstable_slugs:
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

        # 4. Permissive fallback. If the slug is registry-level unstable
        # but didn't match any family, still surface the hint — preflight
        # then emits a known_unstable WARN rather than a generic
        # UNKNOWN_PERMISSIVE one.
        if model_id in self._unstable_slugs:
            return ValidationResult.unknown_permissive(
                detail="known_unstable; verify with discover_models()"
            )
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
        - user-registered slugs,
        - ``example_slugs`` from every registered family (documentation hint),
        - the most-recent discovery cache snapshot (if any).

        **Documentation grade, not a contract.** Family-matched slugs not
        in any of these sources still resolve through ``get()`` and
        ``validate()``; this method exists for IDE autocomplete, doc
        generation, and capability advertising.
        """
        seen: set[str] = set(self._user)
        # Apply ``canonical_slug`` to each family's ``example_slugs`` so
        # the surface returned matches the wire form a user actually
        # needs to pass. User-registered slugs in ``self._user`` are
        # treated as authoritative (caller chose that exact string) and
        # are NOT rewritten — only the family's editorial examples are.
        for family in self._provider_families:
            seen.update(_canonicalize_family_examples(family))
        with self._lock:
            for family in self._user_families:
                seen.update(_canonicalize_family_examples(family))
        cached = self._discovery_cache.peek() if self._discovery_cache else None
        if cached is not None and cached.status is DiscoveryStatus.OK:
            seen.update(cached.slugs)
        return sorted(seen)

    def has(self, model_id: str) -> bool:
        """True if the model_id (or alias / family pattern) is non-fallback.

        Coherent with ``__contains__`` and ``validate(...).is_ok``: returns
        ``True`` for any slug that resolves via user spec, family pattern,
        alias, or deprecated alias. Returns ``False`` for the permissive
        fallback.
        """
        if (
            model_id in self._user
            or model_id in self._alias_index
            or model_id in self._deprecated_alias_index
        ):
            return True
        return self.match_family(model_id) is not None

    def items(self) -> Iterator[tuple[str, ModelSpec]]:
        """Iterate over ``(model_id, spec)`` pairs in deterministic order.

        Aliases are not yielded — only canonical ids appear. Family-only
        slugs surfaced via ``known()`` (e.g., ``example_slugs`` or discovery
        cache entries that aren't user-registered) are intentionally
        skipped: ``items()`` returns concrete ``ModelSpec`` objects, and
        family resolution requires a slug to construct one. Callers needing
        the family-resolved spec should call ``get(slug)`` directly.
        """
        for model_id in self.known():
            spec = self._user.get(model_id)
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
        for model_id, spec in self._user.items():
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


def _canonicalize_family_examples(family: ModelFamily) -> tuple[str, ...]:
    """Return a family's ``example_slugs`` rewritten through its
    ``canonical_slug`` (no-op when the family doesn't declare one).

    Used by :meth:`ModelRegistry.known` so the returned surface — which
    drives IDE autocomplete and capability advertising — matches the
    wire form a user must actually pass. Identity-default preserves
    today's behavior for families that don't ship a transform.
    """
    if family.canonical_slug is None:
        return family.example_slugs
    return tuple(family.canonical_slug(s) for s in family.example_slugs)


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
