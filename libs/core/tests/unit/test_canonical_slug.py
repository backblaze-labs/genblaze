"""Tests for ``ModelFamily.canonical_slug`` + registry plumbing.

Closes the 2026-05-23 feedback batch item 2 + the deferred audio/Veo
casing concern from PR-5. The mechanism: a family declares an optional
``canonical_slug: Callable[[str], str]``; ``resolve()`` substitutes
``canonical_slug(input)`` into the spec's ``model_id`` so the wire form
is always correct regardless of how the user typed it. ``validate()``
normalizes via the same callable before the discovery-cache check, and
``known()`` returns canonical forms for documentation honesty.
A one-time INFO log per ``(family, input)`` nudges callers toward the
canonical form when their input gets rewritten.
"""

from __future__ import annotations

import logging
import re

from genblaze_core.models.enums import Modality
from genblaze_core.providers.family import DiscoverySupport, ModelFamily
from genblaze_core.providers.model_registry import ModelRegistry
from genblaze_core.providers.spec import ModelSpec

_FALLBACK = ModelSpec(model_id="*", modality=Modality.AUDIO)


def _family(
    *,
    name: str = "fake-family",
    pattern: str = r"^(?i:Fake-)",
    canonical_slug=None,
    example_slugs: tuple[str, ...] = (),
) -> ModelFamily:
    """Construct a minimal family for these tests.

    Lowercase ``(?i:...)`` group makes the pattern case-insensitive so
    we can hand it ``"FAKE-Foo"`` / ``"fake-foo"`` and exercise the
    canonical-slug rewrite on either casing.
    """
    return ModelFamily(
        name=name,
        pattern=re.compile(pattern),
        spec_template=ModelSpec(model_id="*", modality=Modality.AUDIO),
        description=f"test family {name}",
        canonical_slug=canonical_slug,
        example_slugs=example_slugs,
    )


# ---------------------------------------------------------------------------
# ModelFamily.resolve uses canonical_slug
# ---------------------------------------------------------------------------


class TestResolveUsesCanonicalSlug:
    def test_identity_default_preserves_today_behavior(self):
        """Families without a transform round-trip the caller's input."""
        f = _family()
        spec = f.resolve("Fake-Foo")
        assert spec.model_id == "Fake-Foo"

    def test_rewrites_input_via_transform(self):
        """When canonical_slug is set, the spec's model_id is the wire form."""
        f = _family(canonical_slug=str.lower)
        spec = f.resolve("Fake-FOO")
        assert spec.model_id == "fake-foo"

    def test_transform_called_exactly_once_per_resolve(self):
        """No double-application — the wire form is whatever the transform
        returns for the input, not the transform applied repeatedly."""
        calls: list[str] = []

        def _track(slug: str) -> str:
            calls.append(slug)
            return slug.lower()

        f = _family(canonical_slug=_track)
        f.resolve("Fake-Foo")
        assert calls == ["Fake-Foo"]


# ---------------------------------------------------------------------------
# ModelRegistry.match_family logs INFO on rewrite (once per (family, input))
# ---------------------------------------------------------------------------


class TestMatchFamilyInfoLog:
    def test_no_log_on_identity_rewrite(self, caplog):
        """canonical_slug=str.lower against an already-lowercase input
        produces no rewrite → no log."""
        f = _family(canonical_slug=str.lower)
        reg = ModelRegistry(provider_families=(f,), fallback=_FALLBACK)
        with caplog.at_level(logging.INFO, logger="genblaze.provider.registry"):
            reg.match_family("fake-foo")
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert not infos, "identity rewrite must not log"

    def test_no_log_when_family_has_no_canonical_slug(self, caplog):
        """Families without a transform never log — this is the existing
        behavior for every family pre-0.3.2."""
        f = _family(canonical_slug=None)
        reg = ModelRegistry(provider_families=(f,), fallback=_FALLBACK)
        with caplog.at_level(logging.INFO, logger="genblaze.provider.registry"):
            reg.match_family("Fake-Foo")
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert not infos

    def test_logs_once_on_first_non_canonical_input(self, caplog):
        """A non-canonical input fires the INFO once; the message names
        the family + before/after forms so the caller knows what to update."""
        f = _family(name="fake-family", canonical_slug=str.lower)
        reg = ModelRegistry(provider_families=(f,), fallback=_FALLBACK)
        with caplog.at_level(logging.INFO, logger="genblaze.provider.registry"):
            reg.match_family("Fake-FOO")
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1
        msg = infos[0].getMessage()
        assert "fake-family" in msg
        assert "'Fake-FOO'" in msg
        assert "'fake-foo'" in msg

    def test_dedup_keyed_by_family_and_input(self, caplog):
        """Second call with the same (family, input) does NOT re-log."""
        f = _family(canonical_slug=str.lower)
        reg = ModelRegistry(provider_families=(f,), fallback=_FALLBACK)
        with caplog.at_level(logging.INFO, logger="genblaze.provider.registry"):
            reg.match_family("Fake-FOO")
            reg.match_family("Fake-FOO")
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1

    def test_distinct_inputs_log_independently(self, caplog):
        """Different non-canonical inputs against the same family each
        log once — the dedup key includes the input slug."""
        f = _family(canonical_slug=str.lower)
        reg = ModelRegistry(provider_families=(f,), fallback=_FALLBACK)
        with caplog.at_level(logging.INFO, logger="genblaze.provider.registry"):
            reg.match_family("Fake-FOO")
            reg.match_family("Fake-BAR")
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 2

    def test_fork_carries_dedup_state(self, caplog):
        """``fork()`` copies the dedup set forward so a per-request fork
        pattern doesn't re-log indefinitely (mirrors the existing
        ``_warned_deprecated`` carryover)."""
        f = _family(canonical_slug=str.lower)
        reg = ModelRegistry(provider_families=(f,), fallback=_FALLBACK)
        with caplog.at_level(logging.INFO, logger="genblaze.provider.registry"):
            reg.match_family("Fake-FOO")
            forked = reg.fork()
            forked.match_family("Fake-FOO")
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1, "fork must inherit the dedup state"


# ---------------------------------------------------------------------------
# ModelRegistry.known returns canonical forms
# ---------------------------------------------------------------------------


class TestKnownReturnsCanonicalForms:
    def test_identity_default_passes_examples_through(self):
        f = _family(example_slugs=("Fake-A", "Fake-B"))
        reg = ModelRegistry(provider_families=(f,), fallback=_FALLBACK)
        assert "Fake-A" in reg.known()
        assert "Fake-B" in reg.known()

    def test_canonicalizes_example_slugs(self):
        """When a family declares canonical_slug=str.lower, known() returns
        the lowercase wire forms — that's what docs/autocomplete should show."""
        f = _family(
            canonical_slug=str.lower,
            example_slugs=("Fake-A", "Fake-B"),
        )
        reg = ModelRegistry(provider_families=(f,), fallback=_FALLBACK)
        known = reg.known()
        assert "fake-a" in known
        assert "fake-b" in known
        # And the non-canonical forms are NOT advertised.
        assert "Fake-A" not in known
        assert "Fake-B" not in known


# ---------------------------------------------------------------------------
# ModelRegistry.validate normalizes via canonical_slug before discovery cache
# ---------------------------------------------------------------------------


class TestValidateNormalizesBeforeDiscoveryCache:
    def test_non_canonical_input_matches_canonical_cache_entry(self):
        """A user passing 'Fake-FOO' against a family with
        canonical_slug=str.lower + a discovery cache containing 'fake-foo'
        gets OK_AUTHORITATIVE — not NOT_FOUND. The normalization is what
        makes validate() and submit() agree on slug identity."""
        from genblaze_core.providers.discovery import (
            DiscoveryResult,
            _DiscoveryCache,
        )

        f = _family(canonical_slug=str.lower)
        # Seed the cache with the canonical (lowercase) wire form.
        cache = _DiscoveryCache(lambda: DiscoveryResult.ok({"fake-foo"}))
        # Prime the cache so ``peek()`` returns the seeded entry.
        cache.get()
        reg = ModelRegistry(provider_families=(f,), fallback=_FALLBACK, discovery_cache=cache)

        result = reg.validate("Fake-FOO", discovery_support=DiscoverySupport.NATIVE)
        # Without the normalization fix this would be NOT_FOUND because
        # "Fake-FOO" isn't in the cache's {"fake-foo"} set.
        assert result.is_ok, f"expected OK, got {result!r}"
