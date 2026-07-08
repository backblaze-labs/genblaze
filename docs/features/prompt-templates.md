<!-- last_verified: 2026-07-08 -->
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
- Named Python format fields are supported, including format specs (`{price:.2f}`), conversions (`{name!r}`), attributes (`{user.name}`), and item lookups (`{items[0]}`)
- Single braces that do not start a named format field render as written, so JSON, code blocks, dicts, sets, and other literal braces are safe in prompts
- Doubled braces in literal text collapse to single braces, matching Python format escaping; write `{{identifier}}` for literal `{identifier}`, or `{{{{ value }}}}` for literal `{{ value }}`
- Unrendered templates in `run()` raise `GenblazeError` — use `batch_run(dicts)` or call `.render()` manually

## Serialization

`PromptTemplate` is a Pydantic v2 model with full `model_dump()`/`model_validate()` support.

## Canonical file

`libs/core/genblaze_core/models/prompt_template.py`
