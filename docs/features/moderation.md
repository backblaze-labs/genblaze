<!-- last_verified: 2026-03-17 -->
# Moderation Hooks

`ModerationHook` provides pre/post-step content screening for prompt text and generated outputs.

## Usage

```python
from genblaze_core import Pipeline, ModerationHook, ModerationResult, Modality

class MyModerationHook(ModerationHook):
    def check_prompt(self, prompt, params):
        if "forbidden" in (prompt or ""):
            return ModerationResult(
                allowed=False, reason="Forbidden content",
                flagged_categories=["policy"],
            )
        return ModerationResult(allowed=True)

    def check_output(self, assets):
        return ModerationResult(allowed=True)

result = (
    Pipeline("moderated", moderation=MyModerationHook())
    .step(provider, model="m", prompt="hello", modality=Modality.IMAGE)
    .run()
)
```

## Execution order

1. **Pre-step moderation** — `check_prompt()` before generation. Rejected prompts skip the provider entirely.
2. **Cache lookup** — only reached if moderation passes.
3. **Provider invoke** — with fallback model support.
4. **Post-step moderation** — `check_output()` after generation. Rejected outputs are not cached.
5. **Cache write** — only for SUCCEEDED steps.

## Failure behavior

- Failed moderation sets `step.status=FAILED`, `error_code=INVALID_INPUT`
- `step.metadata["moderation"]` carries structured details: `stage`, `reason`, `flagged_categories`
- Moderation hook exceptions are caught and fail the step with `error_code=UNKNOWN`
- Works with `fail_fast=True` (stops pipeline) and `fail_fast=False` (continues)
- Steps with `prompt=None` skip pre-step moderation

## Async support

Override `acheck_prompt()` and `acheck_output()` for native async. Defaults wrap sync methods via `asyncio.to_thread()`.

## Canonical file

`libs/core/genblaze_core/pipeline/moderation.py`
