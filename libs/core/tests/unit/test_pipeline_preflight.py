"""Tests for Pipeline model-preflight phase.

Coverage:
- ``preflight=True`` (default): raises on NOT_FOUND, WARNs on
  OK_PROVISIONAL / UNKNOWN_PERMISSIVE, silent on OK_AUTHORITATIVE.
- ``preflight=False`` skips the path entirely (RT-11c).
- WARN dedup via ``_warned_preflight`` (one per provider/slug).
- Suggestions surface in NOT_FOUND error messages.
- ``ThreadPoolExecutor`` parallelism — preflight runs validate_model
  on every step, sync codebase, no asyncio.run_until_complete (RT-2).
"""

from __future__ import annotations

import logging
import re

import pytest
from genblaze_core import Pipeline
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import Modality
from genblaze_core.providers.base import BaseProvider, ProviderCapabilities
from genblaze_core.providers.discovery import (
    DiscoveryResult,
    _DiscoveryCache,
)
from genblaze_core.providers.family import DiscoverySupport, ModelFamily
from genblaze_core.providers.model_registry import ModelRegistry
from genblaze_core.providers.spec import ModelSpec


def _make_native_provider(slugs: set[str]) -> BaseProvider:
    """A NATIVE provider whose discovery cache returns ``slugs``."""

    class _NativeProvider(BaseProvider):
        name = "native-test"
        discovery_support = DiscoverySupport.NATIVE

        @classmethod
        def create_registry(cls) -> ModelRegistry:
            cache = _DiscoveryCache(lambda: DiscoveryResult.ok(slugs))
            return ModelRegistry(discovery_cache=cache)

        def discover_models(
            self,
            *,
            max_age_seconds: float | None = ...,  # type: ignore[assignment]
        ) -> DiscoveryResult:
            cache = self._models._discovery_cache
            assert cache is not None
            if max_age_seconds is ...:  # type: ignore[comparison-overlap]
                return cache.get()
            return cache.get(max_age_seconds=max_age_seconds)

        def get_capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(supported_modalities=[Modality.IMAGE], models=[])

        def submit(self, step, config=None):  # type: ignore[no-untyped-def]
            return "pid"

        def poll(self, prediction_id, config=None):  # type: ignore[no-untyped-def]
            return True

        def fetch_output(self, prediction_id, step):  # type: ignore[no-untyped-def]
            return step

    # Ensure each test gets a fresh class-level cache.
    return _NativeProvider()


def _make_family_provider(family: ModelFamily) -> BaseProvider:
    """A NONE-discovery provider with one family."""

    class _FamilyProvider(BaseProvider):
        name = "family-test"
        discovery_support = DiscoverySupport.NONE

        @classmethod
        def create_registry(cls) -> ModelRegistry:
            return ModelRegistry(provider_families=[family])

        def get_capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(supported_modalities=[Modality.IMAGE], models=[])

        def submit(self, step, config=None):  # type: ignore[no-untyped-def]
            return "pid"

        def poll(self, prediction_id, config=None):  # type: ignore[no-untyped-def]
            return True

        def fetch_output(self, prediction_id, step):  # type: ignore[no-untyped-def]
            return step

    return _FamilyProvider()


class TestNotFoundRaises:
    def test_native_missing_slug_raises(self) -> None:
        p = _make_native_provider({"live-slug-1", "live-slug-2"})
        pipe = Pipeline("t").step(p, model="dead-slug", modality=Modality.IMAGE, prompt="hi")
        with pytest.raises(ProviderError) as exc:
            pipe._validate_steps()
        assert "not found" in str(exc.value).lower()
        assert "dead-slug" in str(exc.value)

    def test_error_message_includes_suggestions(self) -> None:
        p = _make_native_provider({"nvidia/magpie-tts-multilingual"})
        pipe = Pipeline("t").step(p, model="nvidia/riva-tts", modality=Modality.IMAGE, prompt="hi")
        with pytest.raises(ProviderError) as exc:
            pipe._validate_steps()
        assert "nvidia/magpie-tts-multilingual" in str(exc.value)


class TestOkAuthoritativeSilent:
    def test_native_present_slug_silent(self, caplog) -> None:
        p = _make_native_provider({"live-slug"})
        pipe = Pipeline("t").step(p, model="live-slug", modality=Modality.IMAGE, prompt="hi")
        with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
            pipe._validate_steps()
        # No preflight WARNs emitted for OK_AUTHORITATIVE.
        preflight_warns = [r for r in caplog.records if "preflight." in r.getMessage()]
        assert preflight_warns == []


class TestOkProvisionalWarns:
    def test_family_match_no_probe_warns_once(self, caplog) -> None:
        fam = ModelFamily(
            name="fake-fam",
            pattern=re.compile(r"^fake/"),
            spec_template=ModelSpec(model_id="*", modality=Modality.IMAGE),
            description="fake",
        )
        p = _make_family_provider(fam)
        pipe = (
            Pipeline("t")
            .step(p, model="fake/a", modality=Modality.IMAGE, prompt="hi")
            .step(p, model="fake/a", modality=Modality.IMAGE, prompt="hi")
        )
        with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
            pipe._validate_steps()

        # Two steps, same slug — one WARN expected (dedup).
        provisional_warns = [
            r for r in caplog.records if "preflight.provisional" in r.getMessage()
        ]
        assert len(provisional_warns) == 1


class TestUnknownPermissiveWarns:
    def test_no_match_no_discovery_warns_once(self, caplog) -> None:
        # NONE provider, no families, no user spec — UNKNOWN_PERMISSIVE.
        class _NoCatalogProvider(BaseProvider):
            name = "no-catalog"
            discovery_support = DiscoverySupport.NONE

            def get_capabilities(self) -> ProviderCapabilities:
                return ProviderCapabilities(supported_modalities=[Modality.IMAGE], models=[])

            def submit(self, step, config=None):  # type: ignore[no-untyped-def]
                return "pid"

            def poll(self, prediction_id, config=None):  # type: ignore[no-untyped-def]
                return True

            def fetch_output(self, prediction_id, step):  # type: ignore[no-untyped-def]
                return step

        p = _NoCatalogProvider()
        pipe = Pipeline("t").step(p, model="random-slug", modality=Modality.IMAGE, prompt="hi")
        with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
            pipe._validate_steps()

        unknown_warns = [r for r in caplog.records if "preflight.unknown" in r.getMessage()]
        assert len(unknown_warns) == 1


class TestPreflightOptOut:
    """RT-11c: preflight=False must skip the validation path entirely."""

    def test_preflight_false_skips_not_found(self) -> None:
        p = _make_native_provider({"live"})
        pipe = Pipeline("t", preflight=False).step(
            p, model="not-live", modality=Modality.IMAGE, prompt="hi"
        )
        # Should NOT raise — preflight is off.
        pipe._validate_steps()

    def test_preflight_false_skips_warns(self, caplog) -> None:
        p = _make_native_provider({"live"})
        pipe = Pipeline("t", preflight=False).step(
            p, model="not-live", modality=Modality.IMAGE, prompt="hi"
        )
        with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
            pipe._validate_steps()
        preflight_records = [r for r in caplog.records if "preflight." in r.getMessage()]
        assert preflight_records == []

    def test_preflight_method_toggles(self) -> None:
        p = _make_native_provider({"live"})
        pipe = (
            Pipeline("t")
            .preflight(False)
            .step(p, model="not-live", modality=Modality.IMAGE, prompt="hi")
        )
        # toggle method propagates: no raise.
        pipe._validate_steps()


class TestValidatorExceptionHandling:
    def test_validator_exception_falls_through(self, caplog) -> None:
        # A provider whose validate_model raises must not break preflight.
        class _BrokenProvider(BaseProvider):
            name = "broken"
            discovery_support = DiscoverySupport.NONE

            def validate_model(self, model_id: str, *, refresh: bool = False):  # type: ignore[no-untyped-def,override]
                raise RuntimeError("validate exploded")

            def get_capabilities(self) -> ProviderCapabilities:
                return ProviderCapabilities(supported_modalities=[Modality.IMAGE], models=[])

            def submit(self, step, config=None):  # type: ignore[no-untyped-def]
                return "pid"

            def poll(self, prediction_id, config=None):  # type: ignore[no-untyped-def]
                return True

            def fetch_output(self, prediction_id, step):  # type: ignore[no-untyped-def]
                return step

        p = _BrokenProvider()
        pipe = Pipeline("t").step(p, model="anything", modality=Modality.IMAGE, prompt="hi")
        # Must NOT raise — broken validator degrades to UNKNOWN_PERMISSIVE.
        with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
            pipe._validate_steps()

        # The broken validator surfaces as UNKNOWN_PERMISSIVE → preflight.unknown.
        unknown_warns = [r for r in caplog.records if "preflight.unknown" in r.getMessage()]
        assert len(unknown_warns) == 1
