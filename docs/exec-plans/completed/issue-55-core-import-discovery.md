# Issue 55: core import discovery

## Context

`genblaze_core` exposes its public API through `__all__` and a lazy `__getattr__` table. Lazy names are absent from the module dictionary before first access, so the default `dir()` cannot discover them. `RunnableConfig` is also used throughout provider and runnable signatures but is missing from the top-level export surface. The `genblaze` umbrella already implements `__dir__()` by combining `__all__` with module globals.

## Scope

- Add `RunnableConfig` to the core lazy import table.
- Add a core `__dir__()` matching the umbrella behavior.
- Test discovery before first access and top-level import identity.
- Document the public import behavior and add an unreleased changelog entry.

Provider behavior, wire models, canonical hashing, connector packages, and runtime configuration semantics are out of scope.

## Validation

1. Run the two new tests before the implementation and confirm both fail.
2. Run the target core import tests after the implementation.
3. Run the core and meta suites, then the repository `make test` gate.
4. Run `make lint` and verify the changed public imports in a fresh interpreter.

## Outcome

The core package now exposes `RunnableConfig` lazily and reports all names in
`__all__` through `dir()`. Regression tests cover both behaviors. No wire model,
provider, or canonical serialization code changed.
