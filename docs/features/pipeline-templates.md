<!-- last_verified: 2026-03-17 -->
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

## Serialization

`PipelineTemplate` and `StepTemplate` are Pydantic v2 models. JSON serialization uses `model_dump_json()`/`model_validate_json()`. All fields survive roundtrip: chain, input_from, fallback_models, params, tags, etc.

## Provider resolution

`instantiate()` accepts an explicit `providers` dict or auto-discovers via `discover_providers()` entry points.

## Canonical file

`libs/core/genblaze_core/pipeline/template.py`
