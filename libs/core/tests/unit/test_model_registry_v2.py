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
    DiscoverySupport,
    MAX_PROVIDER_FAMILIES,
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

    def test_fork_carries_discovery_cache(self) -> None:
        cache = _DiscoveryCache(lambda: DiscoveryResult.ok({"x"}))
        reg = ModelRegistry(discovery_cache=cache)
        fork = reg.fork()
        # Same cache instance shared across fork — discovery snapshots
        # are per-provider-instance, but fork() is a copy-on-write of
        # specs, not a network surface.
        assert fork._discovery_cache is cache
