# Issue 90: GMICloud duration validation

## Issue

GitHub issue: https://github.com/backblaze-labs/genblaze/issues/90

GMICloud video specs coerced `duration` with `int` before schema validation. Fractional
seconds such as `5.5` were silently truncated to `5`, and fractional strings exposed
the raw `int()` error instead of a typed invalid-input message.

## Plan

1. Add regression tests covering fractional numeric and string durations.
2. Replace the lossy `int` coercer with a whole-second coercer that preserves
   invalid values for typed schema validation.
3. Add a real `IntSchema` for GMICloud video `duration` on family specs and the
   fallback spec.
4. Update video parameter docs to note GMICloud's whole-second requirement.
5. Run the focused GMICloud tests, `make test`, and `make lint`.

## Verification

- `cd libs/connectors/gmicloud && pytest tests/test_gmicloud_provider.py tests/test_catalog_decoupling.py -v`
- `cd libs/connectors/gmicloud && pytest -v`
- `make test`
- `make lint`
