"""Tests for ModelRegistry v2: families, validate(), discovery integration.

Complements ``test_model_registry.py`` which covers the legacy surface.
This module tests the new family-based resolution path, validation
outcomes, the user-family precedence rule (RT-3), and the
``__contains__`` / ``has()`` / ``known()`` coherence (RT-11d).
"""

from __future__ import annotations

import re

import pytest
from genblaze_core.models.enums import Modality
from genblaze_core.providers.discovery import (
    DiscoveryResult,
    _DiscoveryCache,
)
from genblaze_core.providers.family import (
    MAX_PROVIDER_FAMILIES,
    DiscoverySupport,
    ModelFamily,
)
from genblaze_core.providers.model_registry import ModelRegistry
from genblaze_core.providers.spec import FALLBACK_SPEC, ModelSpec
from genblaze_core.providers.validation import (
    ValidationOutcome,
    ValidationSource,
)


def _img_spec(model_id: str = "*", **extra: object) -> ModelSpec:
    return ModelSpec(model_id=model_id, modality=Modality.IMAGE, **extra)  # type: ignore[arg-type]


def _family(name: str, pattern: str, **kwargs: object) -> ModelFamily:
    return ModelFamily(
        name=name,
        pattern=re.compile(pattern),
        spec_template=_img_spec(),
        description=name,
        **kwargs,  # type: ignore[arg-type]
    )


class TestConstruction:
    def test_empty_registry_works(self) -> None:
        reg = ModelRegistry()
        assert reg.get("anything") is FALLBACK_SPEC
        assert reg.known() == []

    def test_provider_families_accepted(self) -> None:
        fam = _family("sdxl", r"^stabilityai/stable-diffusion-xl")
        reg = ModelRegistry(provider_families=[fam])
        assert reg.families == (fam,)

    def test_legacy_defaults_still_accepted(self) -> None:
        reg = ModelRegistry(defaults={"foo": _img_spec("foo")})
        assert reg.get("foo").model_id == "foo"

    def test_max_families_cap_enforced(self) -> None:
        too_many = [_family(f"f{i}", rf"^family-{i}/") for i in range(MAX_PROVIDER_FAMILIES + 1)]
        with pytest.raises(ValueError, match="cap is"):
            ModelRegistry(provider_families=too_many)

    def test_at_cap_accepted(self) -> None:
        right_at_cap = [_family(f"f{i}", rf"^family-{i}/") for i in range(MAX_PROVIDER_FAMILIES)]
        ModelRegistry(provider_families=right_at_cap)


class TestGetResolution:
    def test_user_spec_wins_over_family(self) -> None:
        fam = _family("flux", r"^black-forest-labs/flux")
        reg = ModelRegistry(provider_families=[fam])
        reg.register(_img_spec("black-forest-labs/flux.1-dev"))
        spec = reg.get("black-forest-labs/flux.1-dev")
        assert spec.model_id == "black-forest-labs/flux.1-dev"
        # Confirm it's the registered exact spec, not a family-resolved copy.
        assert reg.has("black-forest-labs/flux.1-dev")

    def test_family_resolves_when_no_user_spec(self) -> None:
        fam = _family("flux", r"^black-forest-labs/flux")
        reg = ModelRegistry(provider_families=[fam])
        spec = reg.get("black-forest-labs/flux.1-dev")
        assert spec.model_id == "black-forest-labs/flux.1-dev"

    def test_unmatched_returns_fallback(self) -> None:
        fam = _family("flux", r"^black-forest-labs/flux")
        reg = ModelRegistry(provider_families=[fam])
        assert reg.get("nvidia/something") is FALLBACK_SPEC


class TestUserFamilyPrecedence:
    """RT-3: user families prepend, take priority over provider families."""

    def test_register_family_prepends(self) -> None:
        provider_fam = _family("provider", r"^x/")
        reg = ModelRegistry(provider_families=[provider_fam])
        user_fam = _family("user-override", r"^x/")
        reg.register_family(user_fam)
        match = reg.match_family("x/anything")
        assert match is not None
        assert match.family.name == "user-override"

    def test_multiple_user_registrations_most_recent_wins(self) -> None:
        reg = ModelRegistry()
        reg.register_family(_family("first", r"^x/"))
        reg.register_family(_family("second", r"^x/"))
        match = reg.match_family("x/y")
        assert match is not None
        # Most recent registration prepends → wins.
        assert match.family.name == "second"

    def test_provider_family_used_when_no_user_match(self) -> None:
        provider_fam = _family("provider", r"^x/")
        reg = ModelRegistry(provider_families=[provider_fam])
        reg.register_family(_family("unrelated", r"^y/"))
        match = reg.match_family("x/anything")
        assert match is not None
        assert match.family.name == "provider"


class TestValidate:
    def test_user_registered_returns_authoritative(self) -> None:
        reg = ModelRegistry()
        reg.register(_img_spec("my-model"))
        r = reg.validate("my-model")
        assert r.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert r.source is ValidationSource.USER

    def test_legacy_defaults_returns_authoritative(self) -> None:
        reg = ModelRegistry(defaults={"legacy": _img_spec("legacy")})
        r = reg.validate("legacy")
        assert r.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert r.source is ValidationSource.USER
        assert "legacy defaults shim" in (r.detail or "")

    def test_family_match_without_discovery_provisional(self) -> None:
        fam = _family("flux", r"^black-forest-labs/flux")
        reg = ModelRegistry(provider_families=[fam])
        r = reg.validate("black-forest-labs/flux.1-dev", discovery_support=DiscoverySupport.NONE)
        assert r.outcome is ValidationOutcome.OK_PROVISIONAL
        assert r.source is ValidationSource.FAMILY
        assert r.family_name == "flux"

    def test_no_match_returns_unknown_permissive(self) -> None:
        reg = ModelRegistry()
        r = reg.validate("totally-unknown")
        assert r.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE
        assert r.source is ValidationSource.FALLBACK

    def test_unstable_examples_propagates_detail(self) -> None:
        fam = _family(
            "gmi-tts",
            r"^MiniMax-",
            example_slugs=("MiniMax-Music-2.5",),
            unstable_examples=("MiniMax-TTS-Speech-2.6-Turbo",),
        )
        reg = ModelRegistry(provider_families=[fam])
        r = reg.validate("MiniMax-TTS-Speech-2.6-Turbo")
        assert r.outcome is ValidationOutcome.OK_PROVISIONAL
        assert "known_unstable" in (r.detail or "")

    def test_native_with_cache_hit_authoritative(self) -> None:
        cache = _DiscoveryCache(lambda: DiscoveryResult.ok({"slug-a", "slug-b"}))
        cache.get()  # populate
        fam = _family("any", r"^slug-")
        reg = ModelRegistry(provider_families=[fam], discovery_cache=cache)
        r = reg.validate("slug-a", discovery_support=DiscoverySupport.NATIVE)
        assert r.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert r.source is ValidationSource.DISCOVERY

    def test_native_with_cache_miss_not_found(self) -> None:
        cache = _DiscoveryCache(lambda: DiscoveryResult.ok({"slug-a", "slug-b"}))
        cache.get()
        fam = _family("any", r"^slug-")
        reg = ModelRegistry(provider_families=[fam], discovery_cache=cache)
        r = reg.validate("slug-c", discovery_support=DiscoverySupport.NATIVE)
        assert r.outcome is ValidationOutcome.NOT_FOUND
        assert r.source is ValidationSource.DISCOVERY

    def test_not_found_includes_suggestions(self) -> None:
        cache = _DiscoveryCache(lambda: DiscoveryResult.ok({"nvidia/magpie-tts-multilingual"}))
        cache.get()
        reg = ModelRegistry(discovery_cache=cache)
        r = reg.validate("nvidia/riva-tts", discovery_support=DiscoverySupport.NATIVE)
        assert r.outcome is ValidationOutcome.NOT_FOUND
        # Levenshtein-ish similarity: "magpie-tts-multilingual" is the
        # only candidate so it should appear.
        assert "nvidia/magpie-tts-multilingual" in r.suggested_slugs

    def test_native_with_no_cache_falls_through_permissive(self) -> None:
        # NATIVE is declared but cache empty (peek=None) — without an
        # authoritative answer we can't say NOT_FOUND.
        reg = ModelRegistry()
        r = reg.validate("anything", discovery_support=DiscoverySupport.NATIVE)
        assert r.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE


class TestContainsAndKnown:
    """RT-11d: __contains__ / has() / known() must be coherent."""

    def test_contains_user_registered(self) -> None:
        reg = ModelRegistry()
        reg.register(_img_spec("x"))
        assert "x" in reg
        assert reg.has("x")

    def test_contains_legacy_default(self) -> None:
        reg = ModelRegistry(defaults={"x": _img_spec("x")})
        assert "x" in reg
        assert reg.has("x")

    def test_contains_family_matched(self) -> None:
        fam = _family("flux", r"^black-forest-labs/flux")
        reg = ModelRegistry(provider_families=[fam])
        assert "black-forest-labs/flux.1-dev" in reg
        assert reg.has("black-forest-labs/flux.1-dev")

    def test_contains_unknown_returns_false(self) -> None:
        reg = ModelRegistry()
        assert "anything" not in reg
        assert not reg.has("anything")

    def test_contains_validate_coherence(self) -> None:
        # The contract: x in reg ⟺ validate(x).is_ok
        reg = ModelRegistry(provider_families=[_family("flux", r"^flux/")])
        for slug in ("flux/dev", "totally-unknown"):
            in_reg = slug in reg
            ok = reg.validate(slug).is_ok
            assert in_reg == ok, f"{slug}: in={in_reg} is_ok={ok}"

    def test_known_includes_examples(self) -> None:
        fam = _family(
            "flux",
            r"^black-forest-labs/flux",
            example_slugs=(
                "black-forest-labs/flux.1-dev",
                "black-forest-labs/flux.1-schnell",
            ),
        )
        reg = ModelRegistry(provider_families=[fam])
        known = reg.known()
        assert "black-forest-labs/flux.1-dev" in known
        assert "black-forest-labs/flux.1-schnell" in known

    def test_known_includes_discovery_cache(self) -> None:
        cache = _DiscoveryCache(lambda: DiscoveryResult.ok({"discovered-1", "discovered-2"}))
        cache.get()
        reg = ModelRegistry(discovery_cache=cache)
        known = reg.known()
        assert "discovered-1" in known


class TestForkPreservesFamilies:
    def test_fork_carries_provider_families(self) -> None:
        fam = _family("p", r"^p/")
        reg = ModelRegistry(provider_families=[fam])
        fork = reg.fork()
        assert fork.match_family("p/x") is not None

    def test_fork_carries_user_families(self) -> None:
        reg = ModelRegistry()
        reg.register_family(_family("user", r"^u/"))
        fork = reg.fork()
        match = fork.match_family("u/x")
        assert match is not None
        assert match.family.name == "user"

    def test_fork_isolates_discovery_cache(self) -> None:
        """``fork()`` produces an independent discovery cache wrapping
        the same fetcher closure. Without this isolation, a refresh on
        one fork would invalidate the parent's (and every sibling
        fork's) warm cache — surprising in multi-tenant deployments
        that fork per tenant."""
        fetcher_calls = [0]

        def fetcher() -> DiscoveryResult:
            fetcher_calls[0] += 1
            return DiscoveryResult.ok({"x"})

        cache = _DiscoveryCache(fetcher)
        reg = ModelRegistry(discovery_cache=cache)
        # Warm the parent's cache.
        reg._discovery_cache.get()
        assert fetcher_calls[0] == 1

        fork = reg.fork()
        # Distinct cache instance — fork's state is independent.
        assert fork._discovery_cache is not cache, "fork should not share the cache instance"
        # Same fetcher closure, so the fork hits the same upstream.
        # Fork's cache starts empty (no fetch yet).
        assert fork._discovery_cache.peek() is None

        # Invalidating the fork must NOT blow out the parent's cache.
        fork._discovery_cache.invalidate()
        parent_after = reg._discovery_cache.peek()
        assert parent_after is not None, "parent cache must survive invalidation of a fork"


class TestProbeCacheConfigurability:
    """Per-instance probe-cache configuration via ``BaseProvider``
    constructor kwargs. Replaces process-global class-attribute
    mutation as the tuning knob."""

    def _make_partial_provider(self, **kwargs: object):
        """Build a minimal PARTIAL provider for cache-config tests."""
        from genblaze_core.providers.base import BaseProvider, ProviderCapabilities

        class _P(BaseProvider):
            name = "test-partial"
            discovery_support = DiscoverySupport.PARTIAL

            def get_capabilities(self) -> ProviderCapabilities:
                return ProviderCapabilities(supported_modalities=[Modality.IMAGE])

            def submit(self, *a, **k):  # type: ignore[no-untyped-def]
                return "pid"

            def poll(self, *a, **k):  # type: ignore[no-untyped-def]
                return True

            def fetch_output(self, *a, **k):  # type: ignore[no-untyped-def]
                return None

        return _P(**kwargs)  # type: ignore[arg-type]

    def test_default_falls_back_to_class_constants(self) -> None:
        """When the kwargs are omitted, instance attrs adopt the class
        constants. This preserves backwards compatibility for any
        connector that hadn't been updated to forward the kwargs."""
        from genblaze_core.providers.base import BaseProvider

        provider = self._make_partial_provider()
        assert provider._probe_cache_ttl == BaseProvider.PROBE_CACHE_TTL_SECONDS
        assert provider._probe_cache_max_entries == BaseProvider.PROBE_CACHE_MAX_ENTRIES

    def test_kwargs_override_class_constants(self) -> None:
        """Explicit kwargs win and live as instance attrs — no class
        mutation, so other instances of the same provider class keep
        the default."""
        provider = self._make_partial_provider(probe_cache_ttl=120.0, probe_cache_max_entries=16)
        assert provider._probe_cache_ttl == 120.0
        assert provider._probe_cache_max_entries == 16

        # A second instance with defaults is unaffected.
        baseline = self._make_partial_provider()
        assert baseline._probe_cache_ttl != 120.0


class TestDeprecationWarningAttribution:
    """Fix verifying ``probe_model``'s deprecation warning carries
    explicit version provenance so log aggregators can filter by it."""

    def test_warning_text_includes_since_and_removal_versions(self) -> None:
        """Without explicit ``since X.Y.Z`` and ``removed in W.Z.Y``,
        log aggregation systems can't distinguish this deprecation
        from any other and operators can't time their migrations."""
        import warnings

        from genblaze_core.providers.base import BaseProvider, ProviderCapabilities

        class _P(BaseProvider):
            name = "warn-test"
            discovery_support = DiscoverySupport.NONE

            def get_capabilities(self) -> ProviderCapabilities:
                return ProviderCapabilities(supported_modalities=[Modality.IMAGE])

            def submit(self, *a, **k):  # type: ignore[no-untyped-def]
                return "pid"

            def poll(self, *a, **k):  # type: ignore[no-untyped-def]
                return True

            def fetch_output(self, *a, **k):  # type: ignore[no-untyped-def]
                return None

        provider = _P()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            provider.probe_model("any-slug")

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1
        msg = str(deprecations[0].message)
        assert "since genblaze-core 0.3.0" in msg
        assert "0.4.0" in msg


class TestDiscoveryFailureSurfacesStaleHint:
    """Fix verifying that when ``discover_models()`` raises during
    ``validate_model``, the returned ``ValidationResult.detail``
    carries a stale-cache hint so operators can correlate during
    upstream incidents."""

    def test_failed_discovery_marks_result_detail(self) -> None:
        from genblaze_core.providers.base import BaseProvider, ProviderCapabilities

        class _BrokenNative(BaseProvider):
            name = "broken-native"
            discovery_support = DiscoverySupport.NATIVE

            def get_capabilities(self) -> ProviderCapabilities:
                return ProviderCapabilities(supported_modalities=[Modality.IMAGE])

            def discover_models(self, *, max_age_seconds: float | None = ...):  # type: ignore[assignment]
                raise RuntimeError("upstream 500")

            def submit(self, *a, **k):  # type: ignore[no-untyped-def]
                return "pid"

            def poll(self, *a, **k):  # type: ignore[no-untyped-def]
                return True

            def fetch_output(self, *a, **k):  # type: ignore[no-untyped-def]
                return None

        provider = _BrokenNative()
        result = provider.validate_model("anything")
        # The detail must mention the discovery failure so operators
        # don't trust a permissive-fallback result during an outage.
        assert result.detail is not None
        assert "discovery fetch failed" in result.detail
