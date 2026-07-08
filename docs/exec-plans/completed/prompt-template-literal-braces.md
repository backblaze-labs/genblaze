<!-- completed: 2026-07-08 -->
# Prompt Template Literal Braces

## Summary

Fix issue #88: `PromptTemplate` should render real `{variable}` placeholders without letting literal braces in JSON, code, dict, or set examples trip Python format parsing.

## Scope

- Replace whole-string `str.format_map()` parsing with field-by-field rendering.
- Preserve named Python format fields, including format specs, conversions, attributes, and item lookups.
- Keep doubled braces as an escape for placeholder-shaped literal text.
- Add regression coverage for JSON-shaped prompts, lone literal braces, missing variables, format fields, and placeholders adjacent to literal braces.
- Update prompt-template behavior docs and the architecture feature list.

## Verification

- `pytest libs/core/tests/unit/test_prompt_template.py -q`
