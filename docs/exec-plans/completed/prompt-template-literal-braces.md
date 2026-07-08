<!-- completed: 2026-07-08 -->
# Prompt Template Literal Braces

## Summary

Fix issue #88: `PromptTemplate` should render real `{variable}` placeholders without letting literal braces in JSON, code, dict, or set examples trip Python format parsing.

## Scope

- Replace `str.format_map()` parsing with `{identifier}`-only substitution.
- Keep doubled braces as an escape for placeholder-shaped literal text.
- Add regression coverage for JSON-shaped prompts, lone literal braces, and missing variables.
- Update prompt-template behavior docs and the architecture feature list.

## Verification

- `pytest libs/core/tests/unit/test_prompt_template.py -q`
