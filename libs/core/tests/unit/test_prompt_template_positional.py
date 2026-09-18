"""PromptTemplate accepts its template positionally as well as by keyword.

The positional spelling is what `examples/batch_with_templates.py` and the
docs have always shown, but `BaseModel.__init__` is keyword-only, so it used
to raise `TypeError: BaseModel.__init__() takes 1 positional argument but 2
were given`.
"""

from __future__ import annotations

import pytest
from genblaze_core.models.enums import Modality
from genblaze_core.models.prompt_template import PromptTemplate
from genblaze_core.pipeline.pipeline import Pipeline
from genblaze_core.testing import MockProvider
from pydantic import ValidationError


class TestPositionalConstruction:
    def test_positional_template(self):
        tpl = PromptTemplate("A {style} painting of a {subject}")
        assert tpl.template == "A {style} painting of a {subject}"
        assert tpl.variables == {"style", "subject"}
        assert tpl.render(style="watercolor", subject="lighthouse") == (
            "A watercolor painting of a lighthouse"
        )

    def test_positional_and_keyword_are_equivalent(self):
        assert PromptTemplate("A {animal}") == PromptTemplate(template="A {animal}")

    def test_keyword_form_still_works(self):
        tpl = PromptTemplate(template="A {animal}")
        assert tpl.render(animal="cat") == "A cat"

    def test_both_forms_together_raise_type_error(self):
        with pytest.raises(TypeError, match="multiple values for argument 'template'"):
            PromptTemplate("A {animal}", template="A {plant}")

    def test_missing_template_still_raises_validation_error(self):
        with pytest.raises(ValidationError):
            PromptTemplate()

    def test_explicit_none_is_a_validation_error_not_a_missing_field(self):
        """`PromptTemplate(None)` must fail on the type, not report a missing field."""
        with pytest.raises(ValidationError, match="valid string"):
            PromptTemplate(None)

    def test_invalid_template_still_validated(self):
        tpl = PromptTemplate("A {animal.name}")
        with pytest.raises(ValueError, match="Unsupported template field"):
            tpl.variables  # noqa: B018 — property raises on unsupported traversal

    def test_pydantic_surface_unchanged(self):
        tpl = PromptTemplate("A {animal}")
        assert tpl.model_dump() == {"template": "A {animal}"}
        assert PromptTemplate.model_validate({"template": "A {animal}"}) == tpl
        assert tpl.model_copy() == tpl


class TestPositionalTemplateInPipeline:
    def test_batch_run_renders_positional_template(self):
        """The shape used by examples/batch_with_templates.py."""
        template = PromptTemplate("A {style} painting of a {subject} at {time_of_day}")

        results = (
            Pipeline("batch-templates-positional")
            .step(
                MockProvider(),
                model="mock-model",
                prompt=template,
                modality=Modality.IMAGE,
            )
            .batch_run(
                [
                    {
                        "style": "watercolor",
                        "subject": "lighthouse",
                        "time_of_day": "sunset",
                    },
                    {"style": "oil", "subject": "village", "time_of_day": "dawn"},
                ]
            )
        )

        prompts = [result.run.steps[0].prompt for result in results]
        assert prompts == [
            "A watercolor painting of a lighthouse at sunset",
            "A oil painting of a village at dawn",
        ]
