<!-- last_verified: 2026-03-17 -->
# Prompt Templates

`PromptTemplate` provides `{variable}` placeholders for reusable, parameterized prompts in batch workflows.

## Usage

```python
from genblaze_core import PromptTemplate

tpl = PromptTemplate(template="A {animal} in {style} style")
tpl.variables    # {"animal", "style"}
tpl.render(animal="cat", style="oil")  # "A cat in oil style"
```

## Pipeline Integration

### Manual render

```python
Pipeline("test").step(provider, model="m", prompt=tpl.render(animal="cat", style="oil")).run()
```

### Batch with dicts

```python
Pipeline("batch").step(provider, model="m", prompt=tpl).batch_run([
    {"animal": "cat", "style": "oil"},
    {"animal": "dog", "style": "watercolor"},
])
```

Dict items render `PromptTemplate` steps; plain string steps keep their original prompt. This works with both `batch_run` and `abatch_run`.

## Behavior

- Missing variables raise `ValueError`
- Extra variables are silently ignored (useful when one dict serves multiple steps with different templates)
- Literal braces use `{{` / `}}` per Python `str.format_map()` convention
- Unrendered templates in `run()` raise `GenblazeError` — use `batch_run(dicts)` or call `.render()` manually

## Serialization

`PromptTemplate` is a Pydantic v2 model with full `model_dump()`/`model_validate()` support.

## Canonical file

`libs/core/genblaze_core/models/prompt_template.py`
