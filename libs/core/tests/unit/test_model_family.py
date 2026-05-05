"""Tests for ModelFamily: construction, matching, resolution, invariants."""

from __future__ import annotations

import re

import pytest
from genblaze_core.models.enums import Modality
from genblaze_core.providers.family import (
    DiscoverySupport,
    FamilyMatch,
    LiveProbeResult,
    MAX_PROVIDER_FAMILIES,
    ModelFamily,
)
from genblaze_core.providers.pricing import per_unit
from genblaze_core.providers.spec import ModelSpec


def _spec(**kwargs: object) -> ModelSpec:
    """Build a minimal ModelSpec for tests."""
    base: dict[str, object] = {"model_id": "*", "modality": Modality.IMAGE}
    base.update(kwargs)
    return ModelSpec(**base)  # type: ignore[arg-type]


class TestConstruction:
    def test_minimal_family_constructs(self) -> None:
        fam = ModelFamily(
            name="sdxl",
            pattern=re.compile(r"^stabilityai/stable-diffusion-xl$"),
            spec_template=_spec(),
            description="Stability AI SDXL family.",
        )
        assert fam.name == "sdxl"
        assert fam.example_slugs == ()
        # ``unstable_examples`` was changed from tuple to frozenset for
        # O(1) membership testing. Empty default is now ``frozenset()``.
        assert fam.unstable_examples == frozenset()
        assert fam.probe is None
        assert fam.discovery_required is False

    def test_family_with_examples(self) -> None:
        fam = ModelFamily(
            name="sd3",
            pattern=re.compile(r"^stabilityai/stable-diffusion-3"),
            spec_template=_spec(),
            description="SD3 family",
            example_slugs=(
                "stabilityai/stable-diffusion-3-5-large",
                "stabilityai/stable-diffusion-3-5-medium",
            ),
        )
        assert "stabilityai/stable-diffusion-3-5-large" in fam.example_slugs

    def test_family_with_pricing_template_rejected(self) -> None:
        # Pricing-by-family is the rot vector this plan eliminates.
        with pytest.raises(ValueError, match="pricing must be None"):
            ModelFamily(
                name="bad",
                pattern=re.compile(r"^x$"),
                spec_template=_spec(pricing=per_unit(0.01)),
                description="invalid",
            )

    def test_unsafe_pattern_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="catastrophic backtracking"):
            ModelFamily(
                name="evil",
                pattern=re.compile(r"(a+)+"),
                spec_template=_spec(),
                description="invalid",
            )


class TestMatching:
    def test_anchored_pattern_matches_exact(self) -> None:
        fam = ModelFamily(
            name="sdxl",
            pattern=re.compile(r"^stabilityai/stable-diffusion-xl$"),
            spec_template=_spec(),
            description="SDXL",
        )
        assert fam.matches("stabilityai/stable-diffusion-xl")
        assert not fam.matches("stabilityai/stable-diffusion-3-5-large")
        assert not fam.matches("black-forest-labs/flux.1-dev")

    def test_prefix_pattern_matches_variants(self) -> None:
        fam = ModelFamily(
            name="flux",
            pattern=re.compile(r"^black-forest-labs/flux\.1-(?:dev|schnell|pro)$"),
            spec_template=_spec(),
            description="FLUX.1 family",
        )
        assert fam.matches("black-forest-labs/flux.1-dev")
        assert fam.matches("black-forest-labs/flux.1-schnell")
        assert not fam.matches("black-forest-labs/flux.2-pro")

    def test_no_match_returns_false(self) -> None:
        fam = ModelFamily(
            name="sdxl",
            pattern=re.compile(r"^stabilityai/stable-diffusion-xl$"),
            spec_template=_spec(),
            description="SDXL",
        )
        assert fam.matches("") is False
        assert fam.matches("totally-unrelated-slug") is False


class TestResolve:
    def test_resolve_substitutes_model_id(self) -> None:
        fam = ModelFamily(
            name="sdxl",
            pattern=re.compile(r"^stabilityai/stable-diffusion-xl"),
            spec_template=_spec(modality=Modality.IMAGE),
            description="SDXL",
        )
        spec = fam.resolve("stabilityai/stable-diffusion-xl-turbo")
        assert spec.model_id == "stabilityai/stable-diffusion-xl-turbo"
        assert spec.modality is Modality.IMAGE

    def test_resolve_preserves_other_fields(self) -> None:
        spec_template = _spec(
            modality=Modality.VIDEO,
            param_aliases={"guidance_scale": "cfg_scale"},
            extras={"is_music": False},
        )
        fam = ModelFamily(
            name="cosmos",
            pattern=re.compile(r"^nvidia/cosmos"),
            spec_template=spec_template,
            description="Cosmos family",
        )
        resolved = fam.resolve("nvidia/cosmos-2.0-diffusion-text2world")
        assert resolved.param_aliases == {"guidance_scale": "cfg_scale"}
        assert resolved.extras == {"is_music": False}


class TestEnums:
    def test_discovery_support_values(self) -> None:
        assert DiscoverySupport.NATIVE.value == "native"
        assert DiscoverySupport.PARTIAL.value == "partial"
        assert DiscoverySupport.NONE.value == "none"

    def test_live_probe_result_values(self) -> None:
        assert LiveProbeResult.LIVE.value == "live"
        assert LiveProbeResult.DEAD.value == "dead"
        assert LiveProbeResult.UNKNOWN.value == "unknown"


class TestFamilyMatch:
    def test_family_match_constructs(self) -> None:
        fam = ModelFamily(
            name="sdxl",
            pattern=re.compile(r"^stabilityai/stable-diffusion-xl$"),
            spec_template=_spec(),
            description="SDXL",
        )
        spec = fam.resolve("stabilityai/stable-diffusion-xl")
        match = FamilyMatch(family=fam, spec=spec)
        assert match.family is fam
        assert match.spec is spec
        assert match.spec.model_id == "stabilityai/stable-diffusion-xl"


class TestConstants:
    def test_max_families_cap_is_reasonable(self) -> None:
        # Sanity check: the cap should be high enough for real connectors,
        # low enough to bound linear-scan resolution.
        assert 8 <= MAX_PROVIDER_FAMILIES <= 64
