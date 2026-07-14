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
- Top-level named fields are supported, including format specs (`{price:.2f}`) and conversions (`{name!r}`)
- Attribute and item traversal is rejected. Pass flattened values instead of templates such as `{user.name}`, `{settings[voice]}`, or `{items[0]}`
- Nested fields inside format specs, such as `{value:{width}}`, are rejected
- Single braces that cannot start a named format field render as written, including quoted-key JSON like `{"name": "{subject}"}` and code with whitespace or punctuation after `{`
- Field-looking literal text must be escaped: write `{{identifier}}` for literal `{identifier}`, `{{name: "cat"}}` for literal `{name: "cat"}`, or `{{{{ value }}}}` for literal `{{ value }}`
- Unrendered templates in `run()` raise `GenblazeError` — use `batch_run(dicts)` or call `.render()` manually

## Migration from `str.format_map`

`PromptTemplate` no longer exposes arbitrary Python formatter traversal. Existing templates that used attribute or item lookup should flatten the data before rendering:

```python
tpl = PromptTemplate(template="Voice: {voice}")
tpl.render(voice=settings["voice"])
```

Format specs and conversions on top-level variables remain supported:

```python
PromptTemplate(template="Price: {price:.2f}").render(price=1.2)
PromptTemplate(template="Name: {name!r}").render(name="Ada")
```

## Serialization

`PromptTemplate` is a Pydantic v2 model with full `model_dump()`/`model_validate()` support.

## Canonical file

`libs/core/genblaze_core/models/prompt_template.py`
