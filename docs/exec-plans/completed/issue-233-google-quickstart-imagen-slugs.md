<!-- completed: 2026-09-03 -->
# Issue 233: genblaze-google README/examples quickstart uses delisted imagen-3.0-* slugs; GeminiImageProvider undocumented

## Problem

GitHub issue: https://github.com/backblaze-labs/genblaze/issues/233
Labels: `bug`, `documentation`, `P1`, `providers`

`genblaze-google`'s README and example script quickstart against
`imagen-3.0-*` slugs, but Google delisted those slugs (they moved to
`imagen-4.0-*` in #220). `imagen-3.0-generate-002` now returns HTTP 404 on
`client.models.get`, which the probe grades `DEAD`, so pipeline preflight
raises before any generation happens.

The README is the PyPI long description for `genblaze-google` — it is the
first thing a new user copy-pastes — so every new user following the
documented quickstart hits a hard failure.

Confirmed reproducible both key-free (mocked 404) and end-to-end against a
live Gemini key:

    $ .venv/bin/python examples/imagen_pipeline.py   # with a real GEMINI_API_KEY
    genblaze_core.exceptions.ProviderError: Step 0 (google-imagen): model
    'imagen-3.0-generate-002' not found in upstream catalog. (upstream probe
    returned DEAD) Try `provider.validate_model(slug, refresh=True)` to
    re-probe if the cached verdict may be stale.
    See docs/migration/registry-decoupling.md.

The failure path is `Pipeline.run()` → `_validate_steps()` →
`_validate_models()` → `_handle_validation()` (`pipeline.py:944`), triggered
because `google_models_get_probe` (`_probe.py:33`) returns `DEAD` for the
delisted slug.

Two secondary gaps flagged in the issue:

1. `GeminiImageProvider` (`gemini_image.py`, shipped in #220) is the only
   Imagen-family image path callable on a freshly created Gemini API key —
   because `imagen-4.0-*` is entitlement-gated for new keys — yet it is not
   mentioned in the README, the example, or `docs/features/provider-system.md`.
2. `imagen-4.0-*` is catalog-listed (preflight passes) but can still 404 at
   the `:predict` call on an unentitled key. Docs must state this caveat so a
   swapped-in `imagen-4.0-*` quickstart does not just move the failure one
   step later without explanation.

The source of truth for current slugs is already correct in code —
`GOOGLE_IMAGEN_FAMILY.example_slugs` = `imagen-4.0-generate-001`,
`imagen-4.0-fast-generate-001` (`_families.py:167-170`); only the docs and the
example drifted.

## Fix

Docs/example-only change. No provider logic changes.

1. **`libs/connectors/google/README.md`**
   - Providers/models table: replace `imagen-3.0-generate-002` /
     `imagen-3.0-fast-generate-001` with `imagen-4.0-generate-001` /
     `imagen-4.0-fast-generate-001`.
   - Add a `GeminiImageProvider` row (entry point `google-gemini-image`,
     example slugs `gemini-2.5-flash-image`, `gemini-3.1-flash-image`).
   - Imagen quickstart snippet: update the `model=` slug to
     `imagen-4.0-generate-001` and add a one-line entitlement caveat pointing
     new-key users to `GeminiImageProvider` as the no-entitlement path.
   - Add a short `GeminiImageProvider` quickstart section.
   - Refresh header prose ("Imagen 3" → current) and bump the
     `<!-- last_verified -->` stamp.

2. **`examples/imagen_pipeline.py`**
   - Module docstring `Models:` list and the `model=` argument →
     `imagen-4.0-*` catalog-listed slugs.
   - Add a brief comment noting the entitlement gate and the
     `GeminiImageProvider` alternative for fresh keys.

3. **`docs/features/provider-system.md`**
   - `PARTIAL` connector list: `Google (Veo / Imagen)` →
     `Google (Veo / Imagen / Gemini-image)`.
   - `map_google_error` consumer list (`Veo, Imagen`) → add Gemini-image, so
     the error-mapper consumer table matches the three shipped providers.
   - Bump the doc's `last_verified` stamp.

4. **`ARCHITECTURE.md`, root `README.md`** — where they generically list
   "Veo, Imagen", add the Gemini-native image provider (not broken, but keeps
   the provider inventory complete per the issue's affected-files table).

5. **`CHANGELOG.md`** — one `**Fixed**` bullet under `[Unreleased]` →
   `genblaze-google` documenting the delisted-slug correction.

### Regression test

The existing suite does **not** catch this: `test_catalog_decoupling.py:275`
mocks `models.get` to *succeed*, so `imagen-3.0-generate-002` grades
`OK_AUTHORITATIVE` in CI while it is `DEAD` in reality. Add a doc-drift guard
so the docs can never re-introduce a slug the code has retired:

- New test asserting the slugs used in `examples/imagen_pipeline.py` (and,
  if feasible to parse, the README quickstart) are members of
  `GOOGLE_IMAGEN_FAMILY.example_slugs` / `GOOGLE_GEMINI_IMAGE_FAMILY`, so a
  future delisting fails CI instead of shipping a broken README.

No version bump — package versions bump per release wave, not per fix
(RELEASING.md).

## Risk

Low. Documentation and one example script; no runtime/API surface change.
The only behavioral note is the `imagen-4.0-*` entitlement gate: a new-key
user who copies the updated Imagen quickstart may still hit a `:predict` 404,
which is why the fix documents the caveat and steers new keys to
`GeminiImageProvider`. The new doc-drift test slightly tightens CI.

Out of scope: the entitlement-gating logic itself (`#206`, already shipped)
and the `imagen-4.0` `:predict` behavior — this fix only corrects docs to
match shipped code.

## Files Modified

| File | Change | Notes |
|---|---|---|
| `libs/connectors/google/README.md` | modified | model table → `imagen-4.0-*`, added `GeminiImageProvider` row + quickstart section, added entitlement caveat, `last_verified` bumped |
| `examples/imagen_pipeline.py` | modified | docstring + `model=` slug → `imagen-4.0-generate-001`, added entitlement/caveat comment |
| `docs/features/provider-system.md` | modified | `PARTIAL` list and `map_google_error` consumer list now include Gemini-image, `last_verified` bumped |
| `ARCHITECTURE.md` | modified | provider inventory mentions Gemini-native image |
| `README.md` (root) | modified | provider inventory + credentials table mention Gemini-image |
| `libs/connectors/google/tests/test_catalog_decoupling.py` | modified | added `TestExampleScriptUsesLiveSlugs` doc-drift guard |
| `CHANGELOG.md` | modified | `**Fixed**` bullet, genblaze-google, `[Unreleased]` |
| `docs/exec-plans/completed/issue-233-google-quickstart-imagen-slugs.md` | created | this file |

Not touched (flagged, out of the issue's stated scope): `docs/reference/model-matrix.md`
and `docs/reference/pricing-recipes.md` still list `imagen-3.0-*` in pricing
tables/snippets. Worth a follow-up issue since Imagen pricing was already
phased out of the registry (`TestPricingPhaseOut`), making these doubly stale.

## Acceptance criteria (from the issue)

- [ ] `libs/connectors/google/README.md` quickstart runs against a fresh
      Gemini key (or documents the entitlement caveat if Imagen is retained).
- [ ] `examples/imagen_pipeline.py` uses catalog-listed slugs.
- [ ] `GeminiImageProvider` is discoverable from the README and
      `docs/features/provider-system.md`.

## Verification

Confirmed the new slug clears preflight where the old one raised `DEAD`.
Before the fix (delisted slug, mocked 404 matching the real catalog
response):

    genblaze_core.exceptions.ProviderError: Step 0 (google-imagen): model
    'imagen-3.0-generate-002' not found in upstream catalog. (upstream probe
    returned DEAD) ...

After the fix, same preflight path with the updated slug
(`imagen-4.0-generate-001`, catalog-listed):

    preflight.provisional step=0 provider=google-imagen model=imagen-4.0-generate-001
    family=google-imagen detail=known slug (...) but NOT confirmed callable
    with this API key: imagen-4.0-* require account entitlement ... See #206.
    — liveness unverifiable; failures will surface mid-pipeline
    validate_model outcome: ok_provisional
    preflight: PASSED (no ProviderError raised)

New doc-drift guard test:

    $ cd libs/connectors/google && pytest tests/test_catalog_decoupling.py -v
    ...
    tests/test_catalog_decoupling.py::TestExampleScriptUsesLiveSlugs::test_imagen_example_uses_current_family_slugs PASSED
    24 passed in 0.18s

Full google connector suite:

    $ cd libs/connectors/google && pytest tests/ -q
    140 passed, 10 skipped in 0.22s

Lint:

    $ ruff check libs/ cli/ examples/
    All checks passed!

    $ ruff format --check libs/ cli/ examples/
    17 files would be reformatted, 402 files already formatted

The 17 format findings are pre-existing markdown code-fence drift (confirmed
present in `libs/connectors/google/README.md` on `main` before this change,
via `git show main:... | ruff format --check`); the new
`GeminiImageProvider` quickstart block added here follows the same
already-drifted style as the adjacent Veo/Imagen blocks in that file, so no
new drift was introduced.

    $ cd libs/connectors/google && deptry .
    Success! No dependency issues found.

`make test` (the full 13-package suite) was not re-run: no non-`google`
package files changed, and the full google suite above already covers the
touched code and tests.
