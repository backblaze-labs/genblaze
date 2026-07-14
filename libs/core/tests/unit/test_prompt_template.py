"""Tests for PromptTemplate model and pipeline integration."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from genblaze_core.exceptions import GenblazeError
from genblaze_core.models.enums import Modality
from genblaze_core.models.prompt_template import PromptTemplate
from genblaze_core.pipeline.pipeline import Pipeline
from genblaze_core.testing import MockProvider

# ---------------------------------------------------------------------------
# PromptTemplate unit tests
# ---------------------------------------------------------------------------


class TestPromptTemplate:
    def test_render_basic(self):
        tpl = PromptTemplate(template="A {animal} in {style} style")
        assert tpl.render(animal="cat", style="watercolor") == "A cat in watercolor style"

    def test_render_multiple_vars(self):
        tpl = PromptTemplate(template="{a} {b} {c}")
        assert tpl.render(a="1", b="2", c="3") == "1 2 3"

    def test_render_missing_var_raises(self):
        tpl = PromptTemplate(template="A {animal} in {style} style")
        with pytest.raises(ValueError, match="Missing template variables.*animal"):
            tpl.render(style="oil")

    def test_render_extra_vars_ignored(self):
        """Extra variables are ignored — useful when one dict serves multiple steps."""
        tpl = PromptTemplate(template="A {animal}")
        assert tpl.render(animal="dog", unused="extra") == "A dog"

    def test_variables_property(self):
        tpl = PromptTemplate(template="A {animal} in {style} style")
        assert tpl.variables == {"animal", "style"}

    def test_variables_empty(self):
        tpl = PromptTemplate(template="static prompt")
        assert tpl.variables == set()

    def test_render_no_variables(self):
        tpl = PromptTemplate(template="static prompt")
        assert tpl.render() == "static prompt"

    def test_render_repeated_variable(self):
        tpl = PromptTemplate(template="{x} and {x}")
        assert tpl.render(x="hi") == "hi and hi"
        assert tpl.variables == {"x"}

    def test_render_escaped_literal_braces(self):
        """Doubled braces preserve a literal placeholder-like string."""
        tpl = PromptTemplate(template="{{literal}} {var}")
        assert tpl.render(var="test") == "{literal} test"

    def test_render_single_literal_braces(self):
        tpl = PromptTemplate(template="cost is 5} dollars for {item}")
        assert tpl.variables == {"item"}
        assert tpl.render(item="x") == "cost is 5} dollars for x"

    def test_render_json_template_with_variable(self):
        tpl = PromptTemplate(template='Return JSON like {"name": "{subject}"}')
        assert tpl.variables == {"subject"}
        assert tpl.render(subject="cat") == 'Return JSON like {"name": "cat"}'

    def test_render_missing_var_ignores_json_keys(self):
        tpl = PromptTemplate(template='Return JSON like {"name": "{subject}"}')
        with pytest.raises(ValueError, match="Missing template variables: subject"):
            tpl.render()

    def test_render_format_spec(self):
        tpl = PromptTemplate(template="Price: {price:.2f}")
        assert tpl.variables == {"price"}
        assert tpl.render(price=1.2) == "Price: 1.20"

    def test_render_alignment_format_spec(self):
        tpl = PromptTemplate(template="Name: {name:>10}")
        assert tpl.variables == {"name"}
        assert tpl.render(name="Ada") == "Name:        Ada"

    def test_render_conversion(self):
        tpl = PromptTemplate(template="Name: {name!r}")
        assert tpl.variables == {"name"}
        assert tpl.render(name="Ada") == "Name: 'Ada'"

    @pytest.mark.parametrize(
        ("template", "kwargs"),
        [
            ("User: {user.api_key}", {"user": SimpleNamespace(api_key="secret")}),
            ("User: {user._token}", {"user": SimpleNamespace(_token="secret")}),
            (
                "User: {user.__dict__[api_key]}",
                {"user": SimpleNamespace(api_key="secret")},
            ),
            ("Voice: {settings[voice]}", {"settings": {"voice": "secret"}}),
            ("First: {items[0]}", {"items": ["cat", "dog"]}),
        ],
    )
    def test_render_rejects_attribute_and_item_lookup(self, template, kwargs):
        tpl = PromptTemplate(template=template)
        with pytest.raises(ValueError, match="Unsupported template field"):
            _ = tpl.variables
        with pytest.raises(ValueError, match="Unsupported template field"):
            tpl.render(**kwargs)

    def test_render_placeholder_before_literal_closing_brace(self):
        tpl = PromptTemplate(template='Return JSON {"count": {count}}')
        assert tpl.variables == {"count"}
        assert tpl.render(count=3) == 'Return JSON {"count": 3}'

    def test_render_invalid_field_conversion_raises_value_error(self):
        tpl = PromptTemplate(template="Name: {name!z}")
        with pytest.raises(ValueError, match="Unsupported template conversion"):
            tpl.render(name="Ada")

    def test_render_unclosed_field_raises_value_error(self):
        tpl = PromptTemplate(template="Name: {name")
        with pytest.raises(ValueError, match="Invalid template field"):
            tpl.render(name="Ada")

    def test_render_nested_format_spec_raises_value_error(self):
        tpl = PromptTemplate(template="Value: {value:{width}}")
        with pytest.raises(ValueError, match="Nested template fields"):
            _ = tpl.variables
        with pytest.raises(ValueError, match="Nested template fields"):
            tpl.render(value=7, width=3)

    def test_render_large_malformed_field_raises_value_error(self):
        tpl = PromptTemplate(template="Value: {value:" + ("{" * 300) + "}")
        with pytest.raises(ValueError, match="exceeds maximum length"):
            _ = tpl.variables
        with pytest.raises(ValueError, match="exceeds maximum length"):
            tpl.render(value=7)

    def test_serialization_roundtrip(self):
        tpl = PromptTemplate(template="A {animal} in {style} style")
        data = tpl.model_dump()
        restored = PromptTemplate.model_validate(data)
        assert restored.template == tpl.template

    def test_json_roundtrip(self):
        tpl = PromptTemplate(template="A {x}")
        json_str = tpl.model_dump_json()
        restored = PromptTemplate.model_validate_json(json_str)
        assert restored.template == tpl.template


# ---------------------------------------------------------------------------
# Pipeline integration tests
# ---------------------------------------------------------------------------


class TestPromptTemplatePipeline:
    def test_step_accepts_prompt_template_with_no_vars(self):
        """Template with no variables works directly in run()."""
        tpl = PromptTemplate(template="static prompt")
        # run() should NOT raise because _build_step gets a PromptTemplate
        # with no variables — but our guard rejects any PromptTemplate.
        # Users should call .render() or use batch_run with dicts.
        with pytest.raises(GenblazeError, match="PromptTemplate.*not rendered"):
            Pipeline("test").step(
                MockProvider(), model="m", prompt=tpl, modality=Modality.IMAGE
            ).run()

    def test_unrendered_template_in_run_raises(self):
        tpl = PromptTemplate(template="A {x}")
        with pytest.raises(GenblazeError, match="PromptTemplate.*not rendered"):
            Pipeline("test").step(
                MockProvider(), model="m", prompt=tpl, modality=Modality.IMAGE
            ).run()

    def test_batch_run_with_dicts(self):
        tpl = PromptTemplate(template="A {animal} in {style} style")
        provider = MockProvider()
        results = (
            Pipeline("test")
            .step(provider, model="m", prompt=tpl, modality=Modality.IMAGE)
            .batch_run(
                [
                    {"animal": "cat", "style": "oil"},
                    {"animal": "dog", "style": "watercolor"},
                ]
            )
        )
        assert len(results) == 2
        assert provider.call_count == 2
        # Verify rendered prompts reached the provider
        assert provider.received_steps[0].prompt == "A cat in oil style"
        assert provider.received_steps[1].prompt == "A dog in watercolor style"

    def test_batch_run_strings_still_works(self):
        """Existing list[str] usage is unchanged."""
        provider = MockProvider()
        results = (
            Pipeline("test")
            .step(provider, model="m", prompt="ignored", modality=Modality.IMAGE)
            .batch_run(["prompt A", "prompt B"])
        )
        assert len(results) == 2
        assert provider.received_steps[0].prompt == "prompt A"
        assert provider.received_steps[1].prompt == "prompt B"

    def test_batch_run_dicts_mixed_prompts(self):
        """Dict mode renders templates but keeps plain string steps unchanged."""
        tpl = PromptTemplate(template="A {x}")
        mock1 = MockProvider(name="mock1")
        mock2 = MockProvider(name="mock2")
        results = (
            Pipeline("test")
            .step(mock1, model="m1", prompt=tpl, modality=Modality.IMAGE)
            .step(mock2, model="m2", prompt="fixed prompt", modality=Modality.IMAGE)
            .batch_run([{"x": "cat"}, {"x": "dog"}])
        )
        assert len(results) == 2
        # Template step was rendered
        assert mock1.received_steps[0].prompt == "A cat"
        assert mock1.received_steps[1].prompt == "A dog"
        # Plain string step kept its original prompt
        assert mock2.received_steps[0].prompt == "fixed prompt"
        assert mock2.received_steps[1].prompt == "fixed prompt"

    def test_batch_run_dict_missing_var_raises(self):
        tpl = PromptTemplate(template="A {animal} in {style} style")
        with pytest.raises(ValueError, match="Missing template variables"):
            Pipeline("test").step(
                MockProvider(), model="m", prompt=tpl, modality=Modality.IMAGE
            ).batch_run([{"animal": "cat"}])  # missing "style"

    def test_abatch_run_with_dicts(self):
        tpl = PromptTemplate(template="A {x}")
        provider = MockProvider()
        results = asyncio.run(
            Pipeline("test")
            .step(provider, model="m", prompt=tpl, modality=Modality.IMAGE)
            .abatch_run([{"x": "cat"}, {"x": "dog"}])
        )
        assert len(results) == 2
        # abatch_run executes items concurrently (asyncio.gather), so the order
        # the shared provider observes submissions is non-deterministic. Assert
        # set-equality: both dicts must render their template correctly.
        assert {s.prompt for s in provider.received_steps} == {"A cat", "A dog"}

    def test_rendered_template_in_step(self):
        """User can render manually and pass string to step()."""
        tpl = PromptTemplate(template="A {x}")
        provider = MockProvider()
        Pipeline("test").step(
            provider, model="m", prompt=tpl.render(x="cat"), modality=Modality.IMAGE
        ).run()
        assert provider.received_steps[0].prompt == "A cat"
