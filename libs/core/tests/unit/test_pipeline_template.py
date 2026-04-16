"""Tests for PipelineTemplate and StepTemplate."""

from __future__ import annotations

import pytest
from genblaze_core.exceptions import GenblazeError
from genblaze_core.models.enums import Modality, StepStatus, StepType
from genblaze_core.pipeline.pipeline import Pipeline
from genblaze_core.pipeline.template import PipelineTemplate, StepTemplate
from genblaze_core.testing import MockProvider

# ---------------------------------------------------------------------------
# Serialization tests
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_json_roundtrip(self):
        template = PipelineTemplate(
            name="test",
            steps=[
                StepTemplate(provider_name="mock", model="m1", prompt="hello"),
                StepTemplate(
                    provider_name="mock",
                    model="m2",
                    prompt="world",
                    modality=Modality.VIDEO,
                    step_type=StepType.GENERATE,
                ),
            ],
            chain=True,
            description="A test template",
            version="1.0",
            tags=["test"],
        )
        json_str = template.to_json()
        restored = PipelineTemplate.from_json(json_str)
        assert restored.name == "test"
        assert len(restored.steps) == 2
        assert restored.chain is True
        assert restored.description == "A test template"
        assert restored.version == "1.0"
        assert restored.tags == ["test"]
        assert restored.steps[0].prompt == "hello"
        assert restored.steps[1].modality == Modality.VIDEO

    def test_dict_roundtrip(self):
        template = PipelineTemplate(
            name="test",
            steps=[StepTemplate(provider_name="mock", model="m1")],
        )
        data = template.to_dict()
        restored = PipelineTemplate.from_dict(data)
        assert restored.name == template.name
        assert len(restored.steps) == len(template.steps)

    def test_save_and_load(self, tmp_path):
        template = PipelineTemplate(
            name="saved",
            steps=[StepTemplate(provider_name="mock", model="m1", prompt="test")],
        )
        path = tmp_path / "template.json"
        template.save(path)
        loaded = PipelineTemplate.from_file(path)
        assert loaded.name == "saved"
        assert loaded.steps[0].prompt == "test"

    def test_chain_and_input_from_survive_serialization(self):
        template = PipelineTemplate(
            name="complex",
            chain=True,
            steps=[
                StepTemplate(provider_name="v", model="vid", modality=Modality.VIDEO),
                StepTemplate(provider_name="a", model="aud", modality=Modality.AUDIO),
                StepTemplate(
                    provider_name="c",
                    model="mux",
                    modality=Modality.VIDEO,
                    input_from=[0, 1],
                ),
            ],
        )
        json_str = template.to_json()
        restored = PipelineTemplate.from_json(json_str)
        assert restored.chain is True
        assert restored.steps[2].input_from == [0, 1]

    def test_fallback_models_survive_serialization(self):
        template = PipelineTemplate(
            name="fallback",
            steps=[
                StepTemplate(
                    provider_name="mock",
                    model="m1",
                    fallback_models=["m2", "m3"],
                ),
            ],
        )
        restored = PipelineTemplate.from_json(template.to_json())
        assert restored.steps[0].fallback_models == ["m2", "m3"]


# ---------------------------------------------------------------------------
# Instantiation tests
# ---------------------------------------------------------------------------


class TestInstantiation:
    def test_instantiate_with_mock_provider(self):
        template = PipelineTemplate(
            name="test",
            steps=[
                StepTemplate(provider_name="mock", model="m1", prompt="hello"),
            ],
        )
        provider = MockProvider()
        pipeline = template.instantiate({"mock": provider})
        result = pipeline.run()
        assert result.run.steps[0].status == StepStatus.SUCCEEDED
        assert provider.received_steps[0].prompt == "hello"

    def test_instantiate_missing_provider_raises(self):
        template = PipelineTemplate(
            name="test",
            steps=[StepTemplate(provider_name="missing", model="m1")],
        )
        with pytest.raises(GenblazeError, match="Provider 'missing' not found"):
            template.instantiate({"mock": MockProvider()})

    def test_instantiate_empty_steps_raises(self):
        template = PipelineTemplate(name="empty", steps=[])
        with pytest.raises(GenblazeError, match="no steps"):
            template.instantiate({"mock": MockProvider()})

    def test_instantiate_with_variables(self):
        template = PipelineTemplate(
            name="var-test",
            steps=[
                StepTemplate(
                    provider_name="mock",
                    model="m1",
                    prompt="A {subject} in {style} style",
                ),
            ],
        )
        provider = MockProvider()
        pipeline = template.instantiate(
            {"mock": provider},
            variables={"subject": "cat", "style": "oil"},
        )
        pipeline.run()
        assert provider.received_steps[0].prompt == "A cat in oil style"

    def test_instantiate_with_chain(self):
        template = PipelineTemplate(
            name="chain",
            chain=True,
            steps=[
                StepTemplate(provider_name="mock", model="m1", prompt="step 1"),
                StepTemplate(provider_name="mock", model="m2", prompt="step 2"),
            ],
        )
        provider = MockProvider()
        pipeline = template.instantiate({"mock": provider})
        result = pipeline.run()
        assert len(result.run.steps) == 2
        assert all(s.status == StepStatus.SUCCEEDED for s in result.run.steps)

    def test_instantiate_with_tenant_and_project(self):
        template = PipelineTemplate(
            name="tenant",
            steps=[StepTemplate(provider_name="mock", model="m1", prompt="hi")],
        )
        pipeline = template.instantiate(
            {"mock": MockProvider()},
            tenant_id="t1",
            project_id="p1",
        )
        result = pipeline.run()
        assert result.run.tenant_id == "t1"
        assert result.run.project_id == "p1"

    def test_instantiate_with_params(self):
        template = PipelineTemplate(
            name="params",
            steps=[
                StepTemplate(
                    provider_name="mock",
                    model="m1",
                    prompt="test",
                    params={"duration": 10, "resolution": "1080p"},
                ),
            ],
        )
        provider = MockProvider()
        pipeline = template.instantiate({"mock": provider})
        pipeline.run()
        # Params should be passed through to the step
        assert provider.received_steps[0].params.get("duration") == 10


# ---------------------------------------------------------------------------
# Pipeline.to_template() tests
# ---------------------------------------------------------------------------


class TestToTemplate:
    def test_to_template_basic(self):
        provider = MockProvider()
        pipeline = (
            Pipeline("my-pipe")
            .step(provider, model="m1", prompt="hello", modality=Modality.IMAGE)
            .step(provider, model="m2", prompt="world", modality=Modality.VIDEO)
        )
        template = pipeline.to_template()
        assert template.name == "my-pipe"
        assert len(template.steps) == 2
        assert template.steps[0].provider_name == "mock"
        assert template.steps[0].model == "m1"
        assert template.steps[0].prompt == "hello"
        assert template.steps[1].modality == Modality.VIDEO

    def test_to_template_roundtrip(self):
        """Pipeline -> template -> JSON -> template -> pipeline -> run."""
        provider = MockProvider()
        original = Pipeline("roundtrip").step(
            provider, model="m1", prompt="test", modality=Modality.IMAGE
        )
        template = original.to_template(description="test", version="1.0")
        json_str = template.to_json()
        restored = PipelineTemplate.from_json(json_str)
        pipeline = restored.instantiate({"mock": provider})
        result = pipeline.run()
        assert result.run.steps[0].status == StepStatus.SUCCEEDED

    def test_to_template_preserves_chain(self):
        provider = MockProvider()
        pipeline = Pipeline("chain", chain=True).step(provider, model="m1", prompt="hi")
        template = pipeline.to_template()
        assert template.chain is True

    def test_to_template_preserves_fallback_models(self):
        provider = MockProvider()
        pipeline = Pipeline("fb").step(
            provider,
            model="m1",
            prompt="hi",
            fallback_models=["m2", "m3"],
        )
        template = pipeline.to_template()
        assert template.steps[0].fallback_models == ["m2", "m3"]

    def test_to_template_with_prompt_template(self):
        """PromptTemplate prompt is preserved as template string."""
        from genblaze_core.models.prompt_template import PromptTemplate

        tpl = PromptTemplate(template="A {x} scene")
        provider = MockProvider()
        pipeline = Pipeline("tpl").step(
            provider,
            model="m1",
            prompt=tpl,
            modality=Modality.IMAGE,
        )
        template = pipeline.to_template()
        assert template.steps[0].prompt == "A {x} scene"
