"""Unit tests for ModelSpec, ModelRegistry, pricing, input mapping, constraints."""

from __future__ import annotations

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    FALLBACK_SPEC,
    ArraySchema,
    BoolSchema,
    EnumSchema,
    FloatSchema,
    IntSchema,
    ModelRegistry,
    ModelSpec,
    PricingContext,
    StringSchema,
    bucketed_by_duration,
    by_model_and_param,
    by_param,
    chain_routers,
    compute_cost,
    first_match,
    implies,
    mutually_exclusive,
    per_input_chars,
    per_output_second,
    per_response_metric,
    per_unit,
    required_one_of,
    requires_together,
    route_audio,
    route_by_media_type,
    route_images,
    route_keyframes,
    tiered,
)


def _step(model: str = "m", **kw) -> Step:
    return Step(provider="test", model=model, **kw)


# ------------------------------------------------------------------ schemas


class TestSchemas:
    def test_int_enum_ok(self):
        IntSchema(enum=frozenset({1, 2, 3})).validate("x", 2)

    def test_int_enum_reject(self):
        with pytest.raises(ProviderError):
            IntSchema(enum=frozenset({1, 2})).validate("x", 5)

    def test_int_bounds(self):
        IntSchema(min=1, max=10).validate("x", 5)
        with pytest.raises(ProviderError):
            IntSchema(min=1, max=10).validate("x", 11)

    def test_int_rejects_bool(self):
        # bool is an int subclass in Python — schema must reject it
        with pytest.raises(ProviderError):
            IntSchema().validate("x", True)

    def test_float_bounds(self):
        FloatSchema(min=0.0, max=1.0).validate("x", 0.5)
        with pytest.raises(ProviderError):
            FloatSchema(min=0.0, max=1.0).validate("x", 1.1)

    def test_string_enum(self):
        StringSchema(enum=frozenset({"a", "b"})).validate("x", "a")
        with pytest.raises(ProviderError):
            StringSchema(enum=frozenset({"a"})).validate("x", "c")

    def test_string_length(self):
        StringSchema(min_len=2, max_len=5).validate("x", "abc")
        with pytest.raises(ProviderError):
            StringSchema(min_len=2).validate("x", "a")

    def test_string_pattern(self):
        StringSchema(pattern=r"^\d+$").validate("x", "123")
        with pytest.raises(ProviderError):
            StringSchema(pattern=r"^\d+$").validate("x", "abc")

    def test_enum_any(self):
        EnumSchema(values=frozenset({1, "a"})).validate("x", 1)
        with pytest.raises(ProviderError):
            EnumSchema(values=frozenset({1, "a"})).validate("x", 2)

    def test_bool_strict(self):
        BoolSchema().validate("x", True)
        with pytest.raises(ProviderError):
            BoolSchema().validate("x", 1)

    def test_array_item(self):
        ArraySchema(item=StringSchema()).validate("x", ["a", "b"])
        with pytest.raises(ProviderError):
            ArraySchema(item=IntSchema()).validate("x", [1, "bad"])

    def test_array_length(self):
        ArraySchema(min_len=1, max_len=3).validate("x", ["a", "b"])
        with pytest.raises(ProviderError):
            ArraySchema(min_len=1).validate("x", [])


# ------------------------------------------------------------------ constraints


class TestConstraints:
    def test_requires_together(self):
        c = requires_together("a", "b")
        c({"a": 1, "b": 2})
        c({})  # neither present — OK
        with pytest.raises(ProviderError):
            c({"a": 1})

    def test_mutually_exclusive(self):
        c = mutually_exclusive("a", "b")
        c({"a": 1})
        c({})
        with pytest.raises(ProviderError):
            c({"a": 1, "b": 2})

    def test_required_one_of(self):
        c = required_one_of("a", "b")
        c({"a": 1})
        with pytest.raises(ProviderError):
            c({})

    def test_implies(self):
        c = implies("premium", "tier")
        c({})  # predicate absent
        c({"premium": False})  # predicate falsy
        c({"premium": True, "tier": "x"})
        with pytest.raises(ProviderError):
            c({"premium": True})


# ------------------------------------------------------------------ input mapping


class TestInputMapping:
    def _img(self, url="u1") -> Asset:
        return Asset(url=url, media_type="image/png")

    def _aud(self, url="a1") -> Asset:
        return Asset(url=url, media_type="audio/mpeg")

    def test_route_by_media_type(self):
        m = route_by_media_type({"image": "img_url", "audio": "aud_url"})
        assert m([self._img("i"), self._aud("a")]) == {"img_url": "i", "aud_url": "a"}

    def test_route_images_positional(self):
        m = route_images(slots=("first", "last"))
        out = m([self._img("a"), self._img("b"), self._img("c")])
        assert out == {"first": "a", "last": "b"}

    def test_route_images_array(self):
        m = route_images(array_slot="refs")
        assert m([self._img("a"), self._img("b")]) == {"refs": ["a", "b"]}

    def test_route_images_combined(self):
        m = route_images(slots=("image",), array_slot="refs")
        out = m([self._img("a"), self._img("b"), self._img("c")])
        assert out == {"image": "a", "refs": ["b", "c"]}

    def test_route_audio(self):
        m = route_audio(slot="src")
        assert m([self._img(), self._aud("a")]) == {"src": "a"}

    def test_route_keyframes(self):
        m = route_keyframes(frames=("frame0", "frame1"))
        out = m([self._img("x"), self._img("y")])
        assert out == {
            "keyframes": {
                "frame0": {"type": "image", "url": "x"},
                "frame1": {"type": "image", "url": "y"},
            }
        }

    def test_chain_routers(self):
        m = chain_routers(
            route_images(slots=("image",)),
            route_audio(slot="aud"),
        )
        out = m([self._img("i"), self._aud("a")])
        assert out == {"image": "i", "aud": "a"}


# ------------------------------------------------------------------ pricing


class TestPricing:
    def test_per_unit(self):
        assets = [Asset(url="a", media_type="image/png")]
        ctx = PricingContext(step=_step(), assets=assets, provider_payload={})
        assert per_unit(0.05)(ctx) == pytest.approx(0.05)

    def test_per_unit_no_assets(self):
        ctx = PricingContext(step=_step(), assets=[], provider_payload={})
        assert per_unit(0.05)(ctx) is None

    def test_per_input_chars(self):
        step = _step(prompt="hello world")  # 11 chars
        ctx = PricingContext(step=step, assets=[], provider_payload={})
        assert per_input_chars(1.0, per=1000)(ctx) == pytest.approx(11 / 1000)

    def test_per_input_chars_falls_back_to_input_asset_char_count(self):
        """Chain-input step: prompt=None, text lives on the input asset instead."""
        step = _step(
            prompt=None,
            inputs=[
                Asset(
                    url="file:///t.txt",
                    media_type="text/plain",
                    metadata={"char_count": 1234},
                )
            ],
        )
        ctx = PricingContext(step=step, assets=[], provider_payload={})
        assert per_input_chars(1.0, per=1000)(ctx) == pytest.approx(1234 / 1000)

    def test_per_input_chars_sums_multiple_input_assets(self):
        step = _step(
            prompt=None,
            inputs=[
                Asset(url="a", media_type="text/plain", metadata={"char_count": 100}),
                Asset(url="b", media_type="text/plain", metadata={"char_count": 50}),
            ],
        )
        ctx = PricingContext(step=step, assets=[], provider_payload={})
        assert per_input_chars(1.0, per=1000)(ctx) == pytest.approx(150 / 1000)

    def test_per_input_chars_ignores_non_text_inputs_without_char_count(self):
        """Image/audio inputs with no char_count don't block the fallback."""
        step = _step(
            prompt=None,
            inputs=[
                Asset(url="img", media_type="image/png"),
                Asset(url="txt", media_type="text/plain", metadata={"char_count": 42}),
            ],
        )
        ctx = PricingContext(step=step, assets=[], provider_payload={})
        assert per_input_chars(1.0, per=1000)(ctx) == pytest.approx(42 / 1000)

    def test_per_input_chars_zero_char_count_is_a_real_cost_not_unknown(self):
        """char_count=0 is a known value — yields 0.0, not None."""
        step = _step(
            prompt=None,
            inputs=[Asset(url="a", media_type="text/plain", metadata={"char_count": 0})],
        )
        ctx = PricingContext(step=step, assets=[], provider_payload={})
        assert per_input_chars(1.0, per=1000)(ctx) == 0.0

    def test_per_input_chars_no_prompt_no_char_count_is_none(self):
        """No prompt and no usable char_count anywhere: genuinely unknown."""
        step = _step(
            prompt=None,
            inputs=[
                Asset(url="img", media_type="image/png"),
                Asset(url="txt", media_type="text/plain", metadata={"lang": "en"}),
            ],
        )
        ctx = PricingContext(step=step, assets=[], provider_payload={})
        assert per_input_chars(1.0, per=1000)(ctx) is None

    def test_per_input_chars_invalid_char_count_is_ignored(self):
        """Non-numeric char_count on one asset doesn't poison a valid one on another."""
        step = _step(
            prompt=None,
            inputs=[
                Asset(url="a", media_type="text/plain", metadata={"char_count": "not-a-number"}),
                Asset(url="b", media_type="text/plain", metadata={"char_count": 20}),
            ],
        )
        ctx = PricingContext(step=step, assets=[], provider_payload={})
        assert per_input_chars(1.0, per=1000)(ctx) == pytest.approx(20 / 1000)

    def test_per_output_second(self):
        assets = [Asset(url="a", media_type="audio/mp3", duration=3.5)]
        ctx = PricingContext(step=_step(), assets=assets, provider_payload={})
        assert per_output_second(0.02)(ctx) == pytest.approx(0.07)

    def test_per_output_second_no_duration(self):
        ctx = PricingContext(step=_step(), assets=[], provider_payload={})
        assert per_output_second(0.02)(ctx) is None

    def test_per_response_metric(self):
        ctx = PricingContext(step=_step(), assets=[], provider_payload={"predict_time": 12.0})
        f = per_response_metric(lambda c: c.provider_payload["predict_time"] * 0.01)
        assert f(ctx) == pytest.approx(0.12)

    def test_tiered(self):
        assets = [Asset(url="a", media_type="image/png")]
        ctx = PricingContext(
            step=_step(params={"quality": "hd", "size": "1024x1024"}),
            assets=assets,
            provider_payload={},
        )
        t = tiered(
            {("standard", "1024x1024"): 0.04, ("hd", "1024x1024"): 0.08},
            key=lambda c: (c.step.params.get("quality"), c.step.params.get("size")),
        )
        assert t(ctx) == pytest.approx(0.08)

    def test_bucketed_by_duration_from_assets(self):
        assets = [Asset(url="a", media_type="audio/mp3", duration=7.0)]
        ctx = PricingContext(step=_step(), assets=assets, provider_payload={})
        f = bucketed_by_duration([((0, 5), 0.2), ((5, 10), 0.3)])
        assert f(ctx) == pytest.approx(0.3)

    def test_bucketed_by_duration_fallback_to_param(self):
        ctx = PricingContext(
            step=_step(params={"duration_seconds": 4.0}),
            assets=[],
            provider_payload={},
        )
        f = bucketed_by_duration([((0, 5), 0.2), ((5, 10), 0.3)])
        assert f(ctx) == pytest.approx(0.2)

    def test_by_param(self):
        assets = [Asset(url="a", media_type="image/png")]
        ctx = PricingContext(
            step=_step(params={"resolution": "1080p"}),
            assets=assets,
            provider_payload={},
        )
        assert by_param("resolution", {"720p": 0.05, "1080p": 0.10})(ctx) == pytest.approx(0.10)

    def test_by_model_and_param(self):
        assets = [Asset(url="a", media_type="video/mp4")]
        ctx = PricingContext(
            step=_step(model="gen4", params={"duration": 5}),
            assets=assets,
            provider_payload={},
        )
        f = by_model_and_param("duration", {("gen4", 5): 0.5, ("gen4", 10): 1.0})
        assert f(ctx) == pytest.approx(0.5)

    def test_first_match(self):
        ctx = PricingContext(step=_step(), assets=[], provider_payload={})
        f = first_match(per_output_second(0.02), per_unit(0.05))
        # per_output_second returns None (no assets), per_unit returns None (no assets)
        assert f(ctx) is None


# ------------------------------------------------------------------ registry


class TestModelRegistry:
    def test_get_default(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m1", pricing=per_unit(0.10)))
        assert reg.get("m1").model_id == "m1"

    def test_get_unknown_returns_fallback(self):
        reg = ModelRegistry()
        assert reg.get("unknown") is FALLBACK_SPEC

    def test_user_overrides_default(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m1", pricing=per_unit(0.10)))
        reg.register(ModelSpec(model_id="m1", pricing=per_unit(0.20)))
        spec = reg.get("m1")
        ctx = PricingContext(
            step=_step("m1"),
            assets=[Asset(url="u", media_type="image/png")],
            provider_payload={},
        )
        assert spec.pricing(ctx) == pytest.approx(0.20)

    def test_register_pricing_only(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m1"))
        reg.register_pricing("m1", per_unit(0.05))
        ctx = PricingContext(
            step=_step("m1"),
            assets=[Asset(url="u", media_type="image/png")],
            provider_payload={},
        )
        assert reg.get("m1").pricing(ctx) == pytest.approx(0.05)

    def test_register_pricing_preserves_family_param_contracts(self):
        """H4 regression: ``register_pricing`` for a family-matched slug
        not yet in the user layer must clone the family-resolved spec
        (with all its param contracts) and apply pricing on top —
        rather than minting a bare ``ModelSpec`` that drops every
        family attribute the slug would otherwise inherit.
        """
        import re

        from genblaze_core.models.enums import Modality
        from genblaze_core.providers import ModelFamily

        family = ModelFamily(
            name="test-family",
            pattern=re.compile(r"^vendor-line-"),
            spec_template=ModelSpec(
                model_id="*",
                modality=Modality.VIDEO,
                param_aliases={"aspect_ratio": "ratio"},
                param_allowlist=frozenset({"prompt", "ratio"}),
                extras={"is_test": True},
            ),
            description="Test family for H4 regression.",
            example_slugs=("vendor-line-pro",),
        )
        reg = ModelRegistry(provider_families=(family,))

        reg.register_pricing("vendor-line-pro", per_unit(0.42))

        spec = reg.get("vendor-line-pro")
        # Pricing was applied.
        ctx = PricingContext(
            step=_step("vendor-line-pro"),
            assets=[Asset(url="u", media_type="image/png")],
            provider_payload={},
        )
        assert spec.pricing(ctx) == pytest.approx(0.42)
        # And the family's param contracts are preserved.
        assert spec.param_aliases == {"aspect_ratio": "ratio"}
        assert spec.param_allowlist == frozenset({"prompt", "ratio"})
        assert spec.extras == {"is_test": True}
        assert spec.modality is Modality.VIDEO
        # model_id was substituted to the actual slug, not "*".
        assert spec.model_id == "vendor-line-pro"

    def test_register_pricing_unknown_slug_falls_back_to_bare_spec(self):
        """H4: when no family matches and no user spec exists, the
        existing fallback (bare ``ModelSpec`` with only pricing) is
        preserved."""
        reg = ModelRegistry()
        reg.register_pricing("brand-new-slug", per_unit(0.05))
        spec = reg.get("brand-new-slug")
        assert spec.model_id == "brand-new-slug"
        assert spec.pricing is not None
        # No family contracts since no family matched.
        assert spec.param_aliases == {}
        assert spec.param_allowlist is None

    def test_register_pricing_preserves_existing_user_spec(self):
        """H4: if the slug is already in ``_user``, only ``pricing``
        is replaced — the rest of the existing user spec stays."""
        reg = ModelRegistry()
        reg.register(
            ModelSpec(
                model_id="m1",
                pricing=per_unit(0.10),
                param_aliases={"x": "y"},
                extras={"keep": True},
            )
        )
        reg.register_pricing("m1", per_unit(0.99))
        spec = reg.get("m1")
        ctx = PricingContext(
            step=_step("m1"),
            assets=[Asset(url="u", media_type="image/png")],
            provider_payload={},
        )
        assert spec.pricing(ctx) == pytest.approx(0.99)
        assert spec.param_aliases == {"x": "y"}
        assert spec.extras == {"keep": True}

    def test_aliases_resolve(self):
        reg = ModelRegistry()
        reg.register(
            ModelSpec(
                model_id="gpt-image-2",
                aliases=frozenset({"chatgpt-image-latest"}),
            )
        )
        assert reg.get("chatgpt-image-latest").model_id == "gpt-image-2"

    def test_deprecated_alias_resolves_with_warning(self):
        reg = ModelRegistry()
        reg.register(
            ModelSpec(
                model_id="reve-edit-fast-20251030",
                deprecated_aliases=frozenset({"Reve-Edit-Fast"}),
            )
        )
        with pytest.warns(DeprecationWarning, match="Reve-Edit-Fast"):
            spec = reg.get("Reve-Edit-Fast")
        assert spec.model_id == "reve-edit-fast-20251030"

    def test_deprecated_alias_canonical_lookup_does_not_warn(self):
        reg = ModelRegistry()
        reg.register(
            ModelSpec(
                model_id="reve-edit-fast-20251030",
                deprecated_aliases=frozenset({"Reve-Edit-Fast"}),
            )
        )
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning = fail
            spec = reg.get("reve-edit-fast-20251030")
        assert spec.model_id == "reve-edit-fast-20251030"

    def test_deprecated_alias_warning_fires_once_per_slug(self):
        """First lookup warns; subsequent lookups of the same slug stay silent."""
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="new", deprecated_aliases=frozenset({"old"})))
        import warnings

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            reg.get("old")
            reg.get("old")
            reg.get("old")
        dep_warnings = [w for w in captured if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) == 1

    def test_resolve_canonical_known(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="seedream-5.0-lite"))
        assert reg.resolve_canonical("seedream-5.0-lite") == "seedream-5.0-lite"

    def test_resolve_canonical_deprecated_alias(self):
        reg = ModelRegistry()
        reg.register(
            ModelSpec(
                model_id="seedream-5.0-lite",
                deprecated_aliases=frozenset({"Seedream-5.0-Lite"}),
            )
        )
        with pytest.warns(DeprecationWarning):
            assert reg.resolve_canonical("Seedream-5.0-Lite") == "seedream-5.0-lite"

    def test_resolve_canonical_unknown_passes_through(self):
        """Unknown ids match only the fallback; caller input is returned verbatim."""
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="known"))
        assert reg.resolve_canonical("brand-new-model-v99") == "brand-new-model-v99"

    def test_resolve_canonical_custom_fallback_does_not_substitute(self):
        """A user-defined fallback with a real model_id must not silently replace
        the caller's slug on the wire — fallback is identity-checked, not sentinel."""
        reg = ModelRegistry(fallback=ModelSpec(model_id="my-default"))
        reg.register(ModelSpec(model_id="known"))
        assert reg.resolve_canonical("unknown") == "unknown"

    def test_has_recognizes_deprecated_alias(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="new", deprecated_aliases=frozenset({"old"})))
        assert reg.has("new") is True
        assert reg.has("old") is True
        assert reg.has("missing") is False

    def test_fork_isolation(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m1"))
        forked = reg.fork()
        forked.register(ModelSpec(model_id="new", pricing=per_unit(0.5)))
        assert "new" in forked.known()
        assert "new" not in reg.known()

    def test_fork_carries_deprecation_warning_state(self):
        """H6 regression: ``fork()`` must copy ``_warned_deprecated`` so a
        per-request fork in a multi-tenant deployment doesn't re-warn on
        every fork for the same already-warned alias."""
        import warnings

        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="new", deprecated_aliases=frozenset({"old"})))
        # Trigger the once-per-slug warning on the parent.
        with pytest.warns(DeprecationWarning):
            reg.get("old")

        # Forks should NOT re-warn on the same slug — the parent already
        # owns the warning for the lifetime of the registry tree.
        forked = reg.fork()
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning fails the test
            spec = forked.get("old")
        assert spec.model_id == "new"

    def test_fork_warning_state_is_independent(self):
        """H6: forks share the parent's already-warned state at fork time
        but new warnings on a clone don't leak back to the parent."""
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="new", deprecated_aliases=frozenset({"old"})))
        forked = reg.fork()
        # Clone warns first; parent's set should remain empty.
        with pytest.warns(DeprecationWarning):
            forked.get("old")
        # Parent has not warned — its set should still allow a warning.
        with pytest.warns(DeprecationWarning):
            reg.get("old")

    def test_known(self):
        reg = ModelRegistry()
        reg.extend([ModelSpec(model_id="a"), ModelSpec(model_id="b")])
        reg.register(ModelSpec(model_id="c"))
        assert reg.known() == ["a", "b", "c"]

    def test_register_override_false_raises(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m1"))
        with pytest.raises(ValueError):
            reg.register(ModelSpec(model_id="m1"), override=False)

    def test_extend(self):
        reg = ModelRegistry()
        reg.extend([ModelSpec(model_id="x"), ModelSpec(model_id="y")])
        assert reg.known() == ["x", "y"]

    def test_register_family_enforces_user_layer_cap(self):
        """H5: ``register_family`` enforces ``MAX_USER_FAMILIES`` against
        only the user layer — a connector at its provider cap MUST
        NOT block users from registering their own families."""
        import re

        from genblaze_core.providers import (
            MAX_PROVIDER_FAMILIES,
            MAX_USER_FAMILIES,
            ModelFamily,
        )

        # Connector ships at the provider cap.
        provider_families = [
            ModelFamily(
                name=f"p{i}",
                pattern=re.compile(rf"^p{i}-"),
                spec_template=ModelSpec(model_id="*"),
                description=f"Provider family {i}",
            )
            for i in range(MAX_PROVIDER_FAMILIES)
        ]
        reg = ModelRegistry(provider_families=provider_families)

        # User registers MAX_USER_FAMILIES — all succeed.
        for i in range(MAX_USER_FAMILIES):
            reg.register_family(
                ModelFamily(
                    name=f"u{i}",
                    pattern=re.compile(rf"^u{i}-"),
                    spec_template=ModelSpec(model_id="*"),
                    description=f"User family {i}",
                )
            )

        # The next user registration must raise — and the error message
        # must name the user layer (not "provider families") so the
        # cause is clear.
        with pytest.raises(ValueError, match="user families"):
            reg.register_family(
                ModelFamily(
                    name="overflow",
                    pattern=re.compile(r"^overflow-"),
                    spec_template=ModelSpec(model_id="*"),
                    description="One too many",
                )
            )

    def test_register_family_cap_independent_of_provider_layer(self):
        """H5: with no provider families, the user can still register up
        to MAX_USER_FAMILIES — the cap is per-layer."""
        import re

        from genblaze_core.providers import MAX_USER_FAMILIES, ModelFamily

        reg = ModelRegistry()  # no provider families
        for i in range(MAX_USER_FAMILIES):
            reg.register_family(
                ModelFamily(
                    name=f"u{i}",
                    pattern=re.compile(rf"^u{i}-"),
                    spec_template=ModelSpec(model_id="*"),
                    description=f"User family {i}",
                )
            )
        assert len(reg._user_families) == MAX_USER_FAMILIES


# ------------------------------------------------------------------ prepare_payload


class TestPreparePayload:
    def test_permissive_fast_path(self):
        reg = ModelRegistry()
        step = _step(model="unknown", prompt="hi", params={"k": "v"})
        out = reg.prepare_payload(step)
        assert out == {"prompt": "hi", "k": "v"}

    def test_aliases_canonical_to_native(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m", param_aliases={"aspect_ratio": "ratio"}))
        step = _step(model="m", params={"aspect_ratio": "16:9"})
        assert reg.prepare_payload(step) == {"ratio": "16:9"}

    def test_native_wins_over_canonical(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m", param_aliases={"aspect_ratio": "ratio"}))
        step = _step(model="m", params={"aspect_ratio": "16:9", "ratio": "9:16"})
        assert reg.prepare_payload(step) == {"ratio": "9:16"}

    def test_transformer_many_to_one(self):
        def _xform(p: dict) -> dict:
            if "resolution" in p and "aspect_ratio" in p:
                p["size"] = f"{p.pop('resolution')}-{p.pop('aspect_ratio')}"
            return p

        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m", param_transformer=_xform))
        step = _step(model="m", params={"resolution": "1080p", "aspect_ratio": "16:9"})
        out = reg.prepare_payload(step)
        assert out == {"size": "1080p-16:9"}

    def test_coercers(self):
        reg = ModelRegistry()
        reg.register(
            ModelSpec(
                model_id="m",
                param_coercers={"duration": str, "sound": lambda b: "on" if b else "off"},
            )
        )
        step = _step(model="m", params={"duration": 5, "sound": True})
        out = reg.prepare_payload(step)
        assert out == {"duration": "5", "sound": "on"}

    def test_defaults_fill(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m", param_defaults={"duration": 5}))
        assert reg.prepare_payload(_step(model="m")) == {"duration": 5}

    def test_defaults_dont_override_user(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m", param_defaults={"duration": 5}))
        step = _step(model="m", params={"duration": 10})
        assert reg.prepare_payload(step) == {"duration": 10}

    def test_schemas_validate(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m", param_schemas={"duration": IntSchema(min=1, max=10)}))
        reg.prepare_payload(_step(model="m", params={"duration": 5}))
        with pytest.raises(ProviderError):
            reg.prepare_payload(_step(model="m", params={"duration": 100}))

    def test_required_after_defaults(self):
        reg = ModelRegistry()
        reg.register(
            ModelSpec(
                model_id="m",
                param_required=frozenset({"duration"}),
                param_defaults={"duration": 5},
            )
        )
        # default satisfies required
        assert reg.prepare_payload(_step(model="m")) == {"duration": 5}

    def test_required_missing_raises(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m", param_required=frozenset({"x"})))
        with pytest.raises(ProviderError, match="Missing required"):
            reg.prepare_payload(_step(model="m"))

    def test_constraints(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m", param_constraints=(requires_together("a", "b"),)))
        reg.prepare_payload(_step(model="m", params={"a": 1, "b": 2}))
        with pytest.raises(ProviderError):
            reg.prepare_payload(_step(model="m", params={"a": 1}))

    def test_allowlist_drops_unknown(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m", param_allowlist=frozenset({"a"})))
        step = _step(model="m", params={"a": 1, "b": 2})
        assert reg.prepare_payload(step) == {"a": 1}

    def test_allowlist_strict_raises(self):
        reg = ModelRegistry(strict_params=True)
        reg.register(ModelSpec(model_id="m", param_allowlist=frozenset({"a"})))
        with pytest.raises(ProviderError, match="Unknown parameters"):
            reg.prepare_payload(_step(model="m", params={"a": 1, "b": 2}))

    def test_input_mapping_user_wins(self):
        reg = ModelRegistry()
        reg.register(
            ModelSpec(
                model_id="m",
                input_mapping=route_images(slots=("image",)),
            )
        )
        step = _step(
            model="m",
            params={"image": "user_url"},
            inputs=[Asset(url="chain_url", media_type="image/png")],
        )
        out = reg.prepare_payload(step)
        assert out["image"] == "user_url"

    def test_input_mapping_chain_fills(self):
        reg = ModelRegistry()
        reg.register(
            ModelSpec(
                model_id="m",
                input_mapping=route_images(slots=("first_frame",)),
            )
        )
        step = _step(
            model="m",
            inputs=[Asset(url="chain_url", media_type="image/png")],
        )
        out = reg.prepare_payload(step)
        assert out == {"first_frame": "chain_url"}


# ------------------------------------------------------------------ compute_cost


class TestComputeCost:
    def test_no_pricing_returns_none(self):
        reg = ModelRegistry()
        assert compute_cost(reg, _step()) is None

    def test_pricing_applied(self):
        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m", pricing=per_unit(0.10)))
        step = _step(model="m", assets=[Asset(url="u", media_type="image/png")])
        assert compute_cost(reg, step) == pytest.approx(0.10)

    def test_broken_pricing_returns_none(self):
        def bad(_ctx):
            raise RuntimeError("boom")

        reg = ModelRegistry()
        reg.register(ModelSpec(model_id="m", pricing=bad))
        step = _step(model="m")
        assert compute_cost(reg, step) is None  # swallowed, logged


# ------------------------------------------------------------------ BaseProvider wiring


class TestBaseProviderIntegration:
    def test_models_default_lazy_per_class(self):
        from genblaze_core.providers.base import SyncProvider

        class A(SyncProvider):
            name = "a"

            @classmethod
            def create_registry(cls):
                reg = ModelRegistry()
                reg.register(ModelSpec(model_id="a-model"))
                return reg

            def generate(self, step, config=None):
                return step

        class B(SyncProvider):
            name = "b"

            @classmethod
            def create_registry(cls):
                reg = ModelRegistry()
                reg.register(ModelSpec(model_id="b-model"))
                return reg

            def generate(self, step, config=None):
                return step

        assert A.models_default().has("a-model")
        assert not A.models_default().has("b-model")
        assert B.models_default().has("b-model")
        assert not B.models_default().has("a-model")

    def test_instance_override(self):
        from genblaze_core.providers.base import SyncProvider

        class P(SyncProvider):
            name = "p"

            @classmethod
            def create_registry(cls):
                reg = ModelRegistry()
                reg.register(ModelSpec(model_id="m"))
                return reg

            def generate(self, step, config=None):
                return step

        custom = ModelRegistry()
        custom.register(ModelSpec(model_id="custom-m", pricing=per_unit(9.99)))
        p = P(models=custom)
        assert p.models is custom
        assert p.models.has("custom-m")

    def test_prepare_payload_merges_step_fields(self):
        from genblaze_core.providers.base import SyncProvider

        class P(SyncProvider):
            name = "p"

            @classmethod
            def create_registry(cls):
                return ModelRegistry()

            def generate(self, step, config=None):
                return step

        p = P()
        step = _step(prompt="hi", params={"k": "v"}, seed=42)
        out = p.prepare_payload(step)
        assert out == {"prompt": "hi", "k": "v", "seed": 42}
