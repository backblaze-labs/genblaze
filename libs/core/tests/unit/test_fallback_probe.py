"""Tests for ``ModelRegistry.fallback_probe`` and its use in
``BaseProvider.validate_model()`` (#248).

Before this, a slug matching no ``ModelFamily`` could never be probed —
``validate_model()`` only consulted a liveness probe when
``match_family()`` returned a hit. For PARTIAL/NONE providers whose
families cover only a subset of real slugs (the common shape: a few
narrow families plus a permissive ``"*"`` fallback), every unmatched
slug graded ``UNKNOWN_PERMISSIVE`` regardless of whether it was a real,
callable model or a fabricated string — no signal either way.

``fallback_probe`` is the liveness counterpart to the registry's
``fallback`` param-shape spec: when no family matched, and a
``fallback_probe`` is configured, it's now consulted the same way a
family probe is (same cache, same single-flight, same LiveProbeResult
grading).
"""

from __future__ import annotations

import re
from unittest.mock import patch

from genblaze_core.models.enums import Modality
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    DiscoverySupport,
    LiveProbeResult,
    ModelFamily,
    ModelRegistry,
    ModelSpec,
)
from genblaze_core.providers.base import SyncProvider
from genblaze_core.providers.validation import ValidationOutcome, ValidationSource

_MATCHED_FAMILY = ModelFamily(
    name="matched",
    pattern=re.compile(r"^matched-"),
    spec_template=ModelSpec(model_id="*", modality=Modality.IMAGE),
    description="A family with its own probe, to pin that fallback_probe "
    "never overrides a family match.",
    example_slugs=("matched-1",),
    probe=lambda slug, **kw: LiveProbeResult.DEAD,
)


class _FallbackProbeProvider(SyncProvider):
    """PARTIAL provider whose registry has one family plus a fallback_probe.

    ``_invoke_family_probe`` forwards straight to the probe callable
    (family or fallback — the framework doesn't distinguish at this
    layer) so tests can swap behavior via ``fallback_probe_result``.
    """

    name = "fallback-probe-test"
    discovery_support = DiscoverySupport.PARTIAL

    def __init__(self, *, fallback_probe_result: LiveProbeResult | None) -> None:
        # Build the registry per-instance and pass it via ``models=``
        # rather than overriding ``create_registry()`` — that classmethod
        # is cached once per *class* (``models_default()``), so an
        # override closing over per-instance state would leak into every
        # other instance of this same test class.
        def _fallback_probe(slug: str, **kwargs: object) -> LiveProbeResult:
            assert fallback_probe_result is not None, (
                "fallback_probe invoked but no result configured for this test"
            )
            return fallback_probe_result

        registry = ModelRegistry(
            provider_families=(_MATCHED_FAMILY,), fallback_probe=_fallback_probe
        )
        super().__init__(models=registry)

    def _invoke_family_probe(self, probe, model_id):  # type: ignore[override]
        return probe(model_id)

    def generate(self, step: Step, config=None) -> Step:  # pragma: no cover
        return step


class TestFallbackProbeGrading:
    def test_unmatched_slug_live_grades_ok_authoritative(self) -> None:
        provider = _FallbackProbeProvider(fallback_probe_result=LiveProbeResult.LIVE)
        result = provider.validate_model("totally-unmatched-slug")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert result.source is ValidationSource.PROBE
        assert result.family_name is None

    def test_unmatched_slug_dead_grades_not_found(self) -> None:
        provider = _FallbackProbeProvider(fallback_probe_result=LiveProbeResult.DEAD)
        result = provider.validate_model("totally-fabricated-slug")
        assert result.outcome is ValidationOutcome.NOT_FOUND
        assert result.source is ValidationSource.PROBE

    def test_unmatched_slug_unknown_stays_permissive(self) -> None:
        provider = _FallbackProbeProvider(fallback_probe_result=LiveProbeResult.UNKNOWN)
        result = provider.validate_model("some-slug")
        assert result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE

    def test_matched_slug_uses_family_probe_not_fallback(self) -> None:
        """A family match takes the family's own probe (DEAD, per
        ``_MATCHED_FAMILY``) even though the registry also carries a
        ``fallback_probe`` — the fallback path only fires when no family
        matched at all."""
        provider = _FallbackProbeProvider(fallback_probe_result=None)
        result = provider.validate_model("matched-1")
        assert result.outcome is ValidationOutcome.NOT_FOUND
        assert result.family_name == "matched"


class TestFallbackProbeCaching:
    def test_probe_fires_once_across_repeated_calls(self) -> None:
        provider = _FallbackProbeProvider(fallback_probe_result=LiveProbeResult.LIVE)
        with patch.object(
            _FallbackProbeProvider,
            "_invoke_family_probe",
            wraps=provider._invoke_family_probe,
        ) as spy:
            provider.validate_model("unmatched-a")
            provider.validate_model("unmatched-a")
            provider.validate_model("unmatched-a")
        assert spy.call_count == 1

    def test_refresh_forces_reprobe(self) -> None:
        provider = _FallbackProbeProvider(fallback_probe_result=LiveProbeResult.LIVE)
        with patch.object(
            _FallbackProbeProvider,
            "_invoke_family_probe",
            wraps=provider._invoke_family_probe,
        ) as spy:
            provider.validate_model("unmatched-b")
            provider.validate_model("unmatched-b", refresh=True)
        assert spy.call_count == 2


class TestNoFallbackProbeConfigured:
    def test_registries_without_fallback_probe_are_unaffected(self) -> None:
        """Default construction (no ``fallback_probe=``) preserves today's
        behavior byte-for-byte: an unmatched slug is UNKNOWN_PERMISSIVE,
        no probe is ever consulted."""

        class _NoFallbackProbeProvider(SyncProvider):
            name = "no-fallback-probe-test"
            discovery_support = DiscoverySupport.PARTIAL

            @classmethod
            def create_registry(cls) -> ModelRegistry:
                return ModelRegistry(provider_families=(_MATCHED_FAMILY,))

            def generate(self, step: Step, config=None) -> Step:  # pragma: no cover
                return step

        provider = _NoFallbackProbeProvider()
        result = provider.validate_model("anything-unmatched")
        assert result.outcome is ValidationOutcome.UNKNOWN_PERMISSIVE


class TestFallbackProbeDoesNotOverrideUserSpecs:
    """Regression tests for a pre-pr-check finding on #248: the fallback
    probe must never overrule a USER-registered spec — directly, or via an
    alias to one — even though ``match_family()`` returns ``None`` for both
    (aliases and user exact-matches aren't family patterns).

    ``ValidationSource.USER`` is documented as "strongest signal regardless
    of provider class" (``model_registry.py``). Before the fix, the
    fallback-probe branch fired on any ``match is None`` case and probed
    the caller-supplied string verbatim — which is wrong for an alias (the
    literal alias string is usually not itself a real wire slug; only its
    canonical resolution is) and, under ``refresh=True``, could downgrade
    a USER exact match's cached ``OK_AUTHORITATIVE`` straight to
    ``NOT_FOUND`` if the probe happened to 404 that same literal string.
    """

    @staticmethod
    def _make_provider(*, fallback_probe_result: LiveProbeResult, spec: ModelSpec) -> SyncProvider:
        calls: list[str] = []

        def _fallback_probe(slug: str, **kwargs: object) -> LiveProbeResult:
            calls.append(slug)
            return fallback_probe_result

        registry = ModelRegistry(fallback_probe=_fallback_probe)
        registry.register(spec)

        class _P(SyncProvider):
            name = "user-spec-fallback-test"
            discovery_support = DiscoverySupport.PARTIAL

            def _invoke_family_probe(self, probe, model_id):  # type: ignore[override]
                return probe(model_id)

            def generate(self, step: Step, config=None) -> Step:  # pragma: no cover
                return step

        provider = _P(models=registry)
        provider._probe_calls = calls  # type: ignore[attr-defined]
        return provider

    def test_alias_resolves_via_user_source_not_probed(self) -> None:
        """An alias whose literal string the probe would grade DEAD must
        still resolve to its registered canonical spec's OK_AUTHORITATIVE —
        the probe must never even see the raw alias string."""
        spec = ModelSpec(model_id="real-wire-slug", modality=Modality.IMAGE, aliases=("friendly",))
        provider = self._make_provider(fallback_probe_result=LiveProbeResult.DEAD, spec=spec)
        result = provider.validate_model("friendly")
        assert result.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert result.source is ValidationSource.USER
        assert provider._probe_calls == []  # type: ignore[attr-defined]

    def test_unmatched_fabricated_slug_still_probed(self) -> None:
        """Sanity check alongside the alias case: a slug that truly isn't
        registered under any name still goes through the fallback probe
        and can still grade NOT_FOUND."""
        spec = ModelSpec(model_id="real-wire-slug", modality=Modality.IMAGE, aliases=("friendly",))
        provider = self._make_provider(fallback_probe_result=LiveProbeResult.DEAD, spec=spec)
        result = provider.validate_model("totally-unregistered-slug")
        assert result.outcome is ValidationOutcome.NOT_FOUND
        assert result.source is ValidationSource.PROBE
        assert provider._probe_calls == ["totally-unregistered-slug"]  # type: ignore[attr-defined]

    def test_user_exact_match_refresh_true_not_overridden(self) -> None:
        """``refresh=True`` on a USER-registered exact-match slug must not
        let the fallback probe downgrade it, even when the probe would
        grade that literal slug DEAD."""
        spec = ModelSpec(model_id="user-only-slug", modality=Modality.IMAGE)
        provider = self._make_provider(fallback_probe_result=LiveProbeResult.DEAD, spec=spec)
        r1 = provider.validate_model("user-only-slug")
        assert r1.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert r1.source is ValidationSource.USER
        r2 = provider.validate_model("user-only-slug", refresh=True)
        assert r2.outcome is ValidationOutcome.OK_AUTHORITATIVE
        assert r2.source is ValidationSource.USER
        assert provider._probe_calls == []  # type: ignore[attr-defined]


class TestFallbackProbeConstruction:
    def test_stored_on_registry(self) -> None:
        probe = lambda slug, **kw: LiveProbeResult.LIVE  # noqa: E731
        reg = ModelRegistry(fallback_probe=probe)
        assert reg._fallback_probe is probe

    def test_defaults_to_none(self) -> None:
        reg = ModelRegistry()
        assert reg._fallback_probe is None

    def test_fork_carries_fallback_probe(self) -> None:
        probe = lambda slug, **kw: LiveProbeResult.LIVE  # noqa: E731
        reg = ModelRegistry(fallback_probe=probe)
        fork = reg.fork()
        assert fork._fallback_probe is probe
