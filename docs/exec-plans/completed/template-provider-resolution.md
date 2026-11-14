# Resolve only referenced template providers

Issue: https://github.com/backblaze-labs/genblaze/issues/67

## Scope and decisions

Validate nonempty steps before provider discovery. For automatic discovery,
construct each referenced provider once. Preserve missing-provider diagnostics
and constructor error chaining. An explicit provider map remains authoritative,
including an empty map; do not discover or modify supplied providers.

Keep entry-point discovery and credential handling unchanged. Filtering imports
or supplying credentials would change the registry contract beyond this bug.

## Steps and acceptance

- Add SDK regression tests for an unused failing constructor, empty templates,
  missing names, repeated references, required constructor errors, and explicit maps.
- Run the regressions against the unchanged implementation and save the failures.
- Narrow automatic construction in `libs/core/genblaze_core/pipeline/template.py`.
- Update `docs/features/pipeline-templates.md` and the Unreleased changelog.
- Run focused tests, `make test`, `make lint`, and `make typecheck`.
  Compare any unrelated failures with the unchanged base.

The fix is ready for review when a template runs through its synthetic provider
without constructing unrelated providers and explicit-map behavior is preserved.
