<!-- last_verified: 2026-07-15 -->
# Pipeline Templates

`PipelineTemplate` enables declarative, serializable pipeline definitions that can be saved to JSON, shared, and instantiated with different providers.

## Usage

### Create from scratch

```python
from genblaze_core import PipelineTemplate, StepTemplate, Modality

template = PipelineTemplate(
    name="image-to-video",
    chain=True,
    steps=[
        StepTemplate(provider_name="openai", model="dall-e-3",
                     prompt="cyberpunk cityscape", modality=Modality.IMAGE),
        StepTemplate(provider_name="openai", model="sora-2",
                     prompt="camera slowly pans right", modality=Modality.VIDEO),
    ],
    description="Generate image then animate",
    version="1.0",
)
```

### Export from existing pipeline

```python
template = pipeline.to_template(description="My pipeline", version="1.0")
```

### Save and load

```python
template.save("templates/image-to-video.json")
loaded = PipelineTemplate.from_file("templates/image-to-video.json")
```

### Instantiate

```python
pipeline = loaded.instantiate({"openai": openai_provider})
result = pipeline.run()
```

### Variable substitution

```python
template = PipelineTemplate(
    name="product",
    steps=[StepTemplate(provider_name="openai", model="sora-2",
                        prompt="A {product} in {setting}")],
)
pipeline = template.instantiate(
    {"openai": provider},
    variables={"product": "laptop", "setting": "minimalist studio"},
)
```

`variables=` renders `{placeholder}` substitutions in **both** `prompt` and string values inside `params=` — top-level or nested in `dict`/`list`/`tuple` containers — through the same `PromptTemplate` engine, so missing-variable behavior (raises `ValueError`) and doubled-brace escaping (`{{literal}}`) are identical between the two:

```python
template = PipelineTemplate(
    name="tts",
    steps=[StepTemplate(provider_name="elevenlabs", model="eleven_v3",
                        prompt="Hello",
                        params={"voice": "{locale}_voice", "tags": ["{campaign}"]})],
)
pipeline = template.instantiate(
    {"elevenlabs": provider},
    variables={"locale": "en", "campaign": "launch"},
)
# step.params == {"voice": "en_voice", "tags": ["launch"]}
```

Non-string param values (`int`, `float`, `bool`, `None`) pass through unchanged. Rendering only happens when `variables=` is passed — a template with no `variables=` argument sees its `params` completely unrendered, exactly as before.

## Serialization

`PipelineTemplate` and `StepTemplate` are Pydantic v2 models. JSON serialization uses `model_dump_json()`/`model_validate_json()`. All fields survive roundtrip: chain, input_from, fallback_models, params, tags, etc.

## Provider resolution

`instantiate()` accepts an explicit `providers` dict or auto-discovers via `discover_providers()` entry points.

## Canonical file

`libs/core/genblaze_core/pipeline/template.py`
