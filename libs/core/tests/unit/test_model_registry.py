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
        reg = ModelRegistry(defaults={"m1": ModelSpec(model_id="m1", pricing=per_unit(0.10))})
        assert reg.get("m1").model_id == "m1"

    def test_get_unknown_returns_fallback(self):
        reg = ModelRegistry()
        assert reg.get("unknown") is FALLBACK_SPEC

    def test_user_overrides_default(self):
        reg = ModelRegistry(defaults={"m1": ModelSpec(model_id="m1", pricing=per_unit(0.10))})
        reg.register(ModelSpec(model_id="m1", pricing=per_unit(0.20)))
        spec = reg.get("m1")
        ctx = PricingContext(
            step=_step("m1"),
            assets=[Asset(url="u", media_type="image/png")],
            provider_payload={},
        )
        assert spec.pricing(ctx) == pytest.approx(0.20)

    def test_register_pricing_only(self):
        reg = ModelRegistry(defaults={"m1": ModelSpec(model_id="m1")})
        reg.register_pricing("m1", per_unit(0.05))
        ctx = PricingContext(
            step=_step("m1"),
            assets=[Asset(url="u", media_type="image/png")],
            provider_payload={},
        )
        assert reg.get("m1").pricing(ctx) == pytest.approx(0.05)

    def test_aliases_resolve(self):
        reg = ModelRegistry(
            defaults={
                "gpt-image-2": ModelSpec(
                    model_id="gpt-image-2",
                    aliases=frozenset({"chatgpt-image-latest"}),
                )
            }
        )
        assert reg.get("chatgpt-image-latest").model_id == "gpt-image-2"

    def test_fork_isolation(self):
        reg = ModelRegistry(defaults={"m1": ModelSpec(model_id="m1")})
        forked = reg.fork()
        forked.register(ModelSpec(model_id="new", pricing=per_unit(0.5)))
        assert "new" in forked.known()
        assert "new" not in reg.known()

    def test_known(self):
        reg = ModelRegistry(defaults={"a": ModelSpec(model_id="a"), "b": ModelSpec(model_id="b")})
        reg.register(ModelSpec(model_id="c"))
        assert reg.known() == ["a", "b", "c"]

    def test_register_override_false_raises(self):
        reg = ModelRegistry(defaults={"m1": ModelSpec(model_id="m1")})
        with pytest.raises(ValueError):
            reg.register(ModelSpec(model_id="m1"), override=False)

    def test_extend(self):
        reg = ModelRegistry()
        reg.extend([ModelSpec(model_id="x"), ModelSpec(model_id="y")])
        assert reg.known() == ["x", "y"]


# ------------------------------------------------------------------ prepare_payload


class TestPreparePayload:
    def test_permissive_fast_path(self):
        reg = ModelRegistry()
        step = _step(model="unknown", prompt="hi", params={"k": "v"})
        out = reg.prepare_payload(step)
        assert out == {"prompt": "hi", "k": "v"}

    def test_aliases_canonical_to_native(self):
        reg = ModelRegistry(
            defaults={"m": ModelSpec(model_id="m", param_aliases={"aspect_ratio": "ratio"})}
        )
        step = _step(model="m", params={"aspect_ratio": "16:9"})
        assert reg.prepare_payload(step) == {"ratio": "16:9"}

    def test_native_wins_over_canonical(self):
        reg = ModelRegistry(
            defaults={"m": ModelSpec(model_id="m", param_aliases={"aspect_ratio": "ratio"})}
        )
        step = _step(model="m", params={"aspect_ratio": "16:9", "ratio": "9:16"})
        assert reg.prepare_payload(step) == {"ratio": "9:16"}

    def test_transformer_many_to_one(self):
        def _xform(p: dict) -> dict:
            if "resolution" in p and "aspect_ratio" in p:
                p["size"] = f"{p.pop('resolution')}-{p.pop('aspect_ratio')}"
            return p

        reg = ModelRegistry(defaults={"m": ModelSpec(model_id="m", param_transformer=_xform)})
        step = _step(model="m", params={"resolution": "1080p", "aspect_ratio": "16:9"})
        out = reg.prepare_payload(step)
        assert out == {"size": "1080p-16:9"}

    def test_coercers(self):
        reg = ModelRegistry(
            defaults={
                "m": ModelSpec(
                    model_id="m",
                    param_coercers={"duration": str, "sound": lambda b: "on" if b else "off"},
                )
            }
        )
        step = _step(model="m", params={"duration": 5, "sound": True})
        out = reg.prepare_payload(step)
        assert out == {"duration": "5", "sound": "on"}

    def test_defaults_fill(self):
        reg = ModelRegistry(
            defaults={"m": ModelSpec(model_id="m", param_defaults={"duration": 5})}
        )
        assert reg.prepare_payload(_step(model="m")) == {"duration": 5}

    def test_defaults_dont_override_user(self):
        reg = ModelRegistry(
            defaults={"m": ModelSpec(model_id="m", param_defaults={"duration": 5})}
        )
        step = _step(model="m", params={"duration": 10})
        assert reg.prepare_payload(step) == {"duration": 10}

    def test_schemas_validate(self):
        reg = ModelRegistry(
            defaults={
                "m": ModelSpec(model_id="m", param_schemas={"duration": IntSchema(min=1, max=10)})
            }
        )
        reg.prepare_payload(_step(model="m", params={"duration": 5}))
        with pytest.raises(ProviderError):
            reg.prepare_payload(_step(model="m", params={"duration": 100}))

    def test_required_after_defaults(self):
        reg = ModelRegistry(
            defaults={
                "m": ModelSpec(
                    model_id="m",
                    param_required=frozenset({"duration"}),
                    param_defaults={"duration": 5},
                )
            }
        )
        # default satisfies required
        assert reg.prepare_payload(_step(model="m")) == {"duration": 5}

    def test_required_missing_raises(self):
        reg = ModelRegistry(
            defaults={"m": ModelSpec(model_id="m", param_required=frozenset({"x"}))}
        )
        with pytest.raises(ProviderError, match="Missing required"):
            reg.prepare_payload(_step(model="m"))

    def test_constraints(self):
        reg = ModelRegistry(
            defaults={
                "m": ModelSpec(model_id="m", param_constraints=(requires_together("a", "b"),))
            }
        )
        reg.prepare_payload(_step(model="m", params={"a": 1, "b": 2}))
        with pytest.raises(ProviderError):
            reg.prepare_payload(_step(model="m", params={"a": 1}))

    def test_allowlist_drops_unknown(self):
        reg = ModelRegistry(
            defaults={"m": ModelSpec(model_id="m", param_allowlist=frozenset({"a"}))}
        )
        step = _step(model="m", params={"a": 1, "b": 2})
        assert reg.prepare_payload(step) == {"a": 1}

    def test_allowlist_strict_raises(self):
        reg = ModelRegistry(
            defaults={"m": ModelSpec(model_id="m", param_allowlist=frozenset({"a"}))},
            strict_params=True,
        )
        with pytest.raises(ProviderError, match="Unknown parameters"):
            reg.prepare_payload(_step(model="m", params={"a": 1, "b": 2}))

    def test_input_mapping_user_wins(self):
        reg = ModelRegistry(
            defaults={
                "m": ModelSpec(
                    model_id="m",
                    input_mapping=route_images(slots=("image",)),
                )
            }
        )
        step = _step(
            model="m",
            params={"image": "user_url"},
            inputs=[Asset(url="chain_url", media_type="image/png")],
        )
        out = reg.prepare_payload(step)
        assert out["image"] == "user_url"

    def test_input_mapping_chain_fills(self):
        reg = ModelRegistry(
            defaults={
                "m": ModelSpec(
                    model_id="m",
                    input_mapping=route_images(slots=("first_frame",)),
                )
            }
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
        reg = ModelRegistry(defaults={"m": ModelSpec(model_id="m", pricing=per_unit(0.10))})
        step = _step(model="m", assets=[Asset(url="u", media_type="image/png")])
        assert compute_cost(reg, step) == pytest.approx(0.10)

    def test_broken_pricing_returns_none(self):
        def bad(_ctx):
            raise RuntimeError("boom")

        reg = ModelRegistry(defaults={"m": ModelSpec(model_id="m", pricing=bad)})
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
                return ModelRegistry(defaults={"a-model": ModelSpec(model_id="a-model")})

            def generate(self, step, config=None):
                return step

        class B(SyncProvider):
            name = "b"

            @classmethod
            def create_registry(cls):
                return ModelRegistry(defaults={"b-model": ModelSpec(model_id="b-model")})

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
                return ModelRegistry(defaults={"m": ModelSpec(model_id="m")})

            def generate(self, step, config=None):
                return step

        custom = ModelRegistry(
            defaults={"custom-m": ModelSpec(model_id="custom-m", pricing=per_unit(9.99))}
        )
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
