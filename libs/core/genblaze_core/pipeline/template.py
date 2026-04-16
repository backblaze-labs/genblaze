"""Pipeline templates — serializable, reusable pipeline definitions."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from genblaze_core.exceptions import GenblazeError
from genblaze_core.models.enums import Modality, StepType

if TYPE_CHECKING:
    from genblaze_core.pipeline.pipeline import Pipeline
    from genblaze_core.providers.base import BaseProvider


class StepTemplate(BaseModel):
    """Serializable definition of a single pipeline step.

    References providers by name (string) rather than instance,
    enabling JSON serialization and sharing.
    """

    provider_name: str
    model: str
    prompt: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    modality: Modality = Modality.IMAGE
    step_type: StepType = StepType.GENERATE
    fallback_models: list[str] = Field(default_factory=list)
    input_from: list[int] | None = None


class PipelineTemplate(BaseModel):
    """Declarative pipeline definition that can be saved, shared, and instantiated.

    Example::

        template = PipelineTemplate(
            name="image-to-video",
            chain=True,
            steps=[
                StepTemplate(provider_name="openai", model="dall-e-3",
                             prompt="cyberpunk cityscape", modality=Modality.IMAGE),
                StepTemplate(provider_name="openai", model="sora-2",
                             prompt="camera slowly pans right", modality=Modality.VIDEO),
            ],
        )

        # Save and load
        template.save("templates/image-to-video.json")
        loaded = PipelineTemplate.from_file("templates/image-to-video.json")

        # Instantiate with providers
        pipeline = loaded.instantiate({"openai": OpenAIProvider(...)})
        result = pipeline.run()
    """

    name: str | None = None
    steps: list[StepTemplate]
    chain: bool = False
    max_concurrency: int | None = None
    description: str | None = None
    version: str | None = None
    tags: list[str] = Field(default_factory=list)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize to JSON string."""
        return self.model_dump_json(indent=indent)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return self.model_dump()

    @classmethod
    def from_json(cls, json_str: str) -> PipelineTemplate:
        """Deserialize from JSON string."""
        return cls.model_validate_json(json_str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PipelineTemplate:
        """Deserialize from dict."""
        return cls.model_validate(data)

    @classmethod
    def from_file(cls, path: str | Path) -> PipelineTemplate:
        """Load from a JSON file."""
        path = Path(path)
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: str | Path) -> None:
        """Save to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    def instantiate(
        self,
        providers: dict[str, BaseProvider] | None = None,
        *,
        variables: dict[str, str] | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> Pipeline:
        """Create a ready-to-run Pipeline from this template.

        Args:
            providers: Dict mapping provider name → provider instance.
                If None, uses discover_providers() to auto-discover.
            variables: Optional dict of {variable} placeholders to render
                in step prompts via str.format_map().
            tenant_id: Optional tenant ID for the pipeline.
            project_id: Optional project ID for the pipeline.

        Returns:
            A configured Pipeline ready for .run() or .arun().

        Raises:
            GenblazeError: If a referenced provider is not found.
        """
        # Lazy imports to avoid circular dependencies
        from genblaze_core.pipeline.pipeline import Pipeline

        if providers is None:
            from genblaze_core.providers.registry import discover_providers

            discovered = discover_providers()
            providers = {}
            for name, cls in discovered.items():
                try:
                    providers[name] = cls()
                except Exception as exc:
                    raise GenblazeError(
                        f"Failed to instantiate provider '{name}'. "
                        f"Pass providers= explicitly if constructor requires arguments: {exc}"
                    ) from exc

        if not self.steps:
            raise GenblazeError("Template has no steps")

        pipe = Pipeline(
            self.name,
            tenant_id=tenant_id,
            project_id=project_id,
            chain=self.chain,
            max_concurrency=self.max_concurrency,
        )

        for st in self.steps:
            provider = providers.get(st.provider_name)
            if provider is None:
                available = ", ".join(sorted(providers.keys()))
                raise GenblazeError(
                    f"Provider '{st.provider_name}' not found. Available: [{available}]"
                )

            # Render prompt variables if provided
            prompt = st.prompt
            if prompt and variables:
                from genblaze_core.models.prompt_template import PromptTemplate

                prompt = PromptTemplate(template=prompt).render(**variables)

            pipe.step(
                provider,
                model=st.model,
                prompt=prompt,
                modality=st.modality,
                step_type=st.step_type,
                fallback_models=st.fallback_models,
                input_from=st.input_from,
                **st.params,
            )

        return pipe
