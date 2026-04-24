<!-- completed: 2026-04-24 -->
# Plan: TypeScript Types for the Language-Neutral Spec

> **Shipped.** Phase 1a (committed `.d.ts` at `libs/spec/ts/genblaze.d.ts`, `make ts-types` target, bidirectional conformance test at `libs/core/tests/unit/test_spec_conformance.py`, CI drift guard) landed in 0.2.3. Phase 1b (npm publish) shipped as `@genblaze/spec` 0.3.0 in 0.2.3 and 0.3.1 in 0.2.4. Phases 2 (`@genblaze/spec/zod`) and 3 (`@genblaze/manifest` with cross-language canonical hash) remain explicit follow-ups — track under a new plan when demand materializes.

## Motivation

genblaze ships Python Pydantic models but no TypeScript types. Consumers
that render or parse manifests from TS (studio UIs, Node backends, etc.)
hand-write the shapes against `libs/spec/schemas/manifest/v1/*.json` and
drift — inventing fields that do not exist (`b2_key`, `videoRun`) and
omitting fields that do (`asset_id`, `metadata`, `width/height`).

This plan ships the `libs/spec/` surface as a typed TypeScript artifact
generated from the authoritative JSON Schemas, plus drift guards that
keep Pydantic models and schemas honest.

## Scope

In-scope:
- Phase 0 — Schema/Pydantic drift audit + reconciliation
- Phase 1a — Committed `libs/spec/ts/genblaze.d.ts` generated from schemas;
  reproducible via `make ts-types`; CI drift guard; semantic doc pass
  on `Asset.url`
- Test surface: bidirectional conformance test in `libs/core/tests/unit/`

Out-of-scope (tracked for later):
- Phase 1b — Publish `@genblaze/spec` to npm with provenance, dual ESM/CJS
- Phase 2 — `@genblaze/spec/zod` subpath for runtime validation
- Phase 3 — `@genblaze/manifest` with pure-TS canonical hash verification
- Full npm-genblaze runtime parity (rejected: provider adapters don't
  belong client-side; canonical-hash parity is a significant invariant risk)

## Decisions

- **Source of truth: hand-written `libs/spec/schemas/*.json`.**
  Pydantic's `model_json_schema()` — even `mode='serialization'` — treats
  fields with `default_factory` as optional. That conflates "absent on
  input" with "absent on the wire," which produces TS types where every
  always-present field shows up as `| undefined`. The hand-written
  schemas encode the wire contract correctly (`run_id`, `steps`,
  `created_at`, `canonical_hash` marked required). Pattern matches
  Stripe/GitHub: hand-shaped spec, runtime validates against it.
- **Tool: `json-schema-to-typescript`** via `npx --yes ...@<pinned>`.
  Avoids adding `package.json` / `node_modules` to the Python monorepo
  in phase 1a. A single Makefile target is the only Node touchpoint.
- **Drift prevention: bidirectional conformance test.** Forward-only
  validation (Pydantic dump → schema.validate) misses phantom-schema-
  field drift. Test also asserts `schema.properties ⊆ model.fields` and
  that every field has a non-empty description (enforces JSDoc flowing
  into TS output).
- **Enum style: string-literal unions**, not TS `enum`. Zero runtime
  cost, tree-shakeable, structurally compatible with wire strings.
  Config: `enableConstEnums: false`.
- **Commit the generated `.d.ts`.** Makes schema impact visible in PRs.
  CI regenerates and fails if the committed file would change.
- **Versioning (phase 1b+): lockstep with `genblaze-core`.** Simpler
  than a compatibility matrix; empty republishes acceptable.

## Phases

### Phase 0 — Drift audit + reconciliation
- Audit output (2026-04-23): ONE drift — `policy.schema.json`
  `prompt_visibility` enum is `["public", "private", "redacted"]` but
  `PromptVisibility` has 4 values including `"encrypted"`.
- Fix: add `"encrypted"` to the schema enum. Pydantic is ground truth;
  semantic question of "does `encrypted` make sense for EmbedPolicy?"
  is tracked in `tech-debt-tracker.md` for a future refactor — not
  blocking this PR.

### Phase 1a — Codegen + drift guard
1. `libs/core/tests/unit/test_spec_conformance.py` — bidirectional
   conformance test (field-set equality, required-fields on wire,
   enum equivalence, non-empty descriptions). Runs under `make test`.
2. `Makefile` target `ts-types`: runs `npx --yes json-schema-to-typescript@<pin>`
   over `libs/spec/schemas/manifest/v1/*.schema.json`, emits
   `libs/spec/ts/genblaze.d.ts`.
3. CI drift guard: `make ts-types` in CI; fail if git diff non-empty.
4. Semantic doc pass on `Asset.url` — move the durable-URL-is-the-handle
   invariant from `transfer.py:338-342` comment into `Field(description=...)`
   so it flows into JSON Schema → TS JSDoc.
5. `libs/spec/README.md` — contract description, regeneration workflow,
   phase-1a consumption via git URL.

### Phase 1b — npm publish (follow-up)
- Register `@genblaze` npm org (verify availability)
- `libs/spec/package.json` with dual ESM/CJS, `"sideEffects": false`,
  raw schemas shipped under `schemas/`, generated types under `ts/`
- Release workflow: `npm publish --provenance` on tag push, OIDC trust
- Version lockstep with `genblaze-core`

### Phase 2 — Zod subpath (on demand)
- `json-schema-to-zod` emits `@genblaze/spec/zod` exports
- Enables `RunSchema.parse(unknownJson)` with compile-time inference

### Phase 3 — `@genblaze/manifest` (on demand)
- Pure-TS canonical JSON + SHA-256 matching
  `libs/core/genblaze_core/canonical/json.py`
- Enables client-side `canonical_hash` verification

## Risks

- **Canonical JSON determinism (AGENTS.md invariant).** Not a risk for
  phase 1a (types only). Becomes load-bearing in phase 3 — any
  Python↔TS divergence in key sort, float normalization, unicode NFC,
  or datetime serialization silently breaks cross-language verify().
  Phase 3 must ship with a shared golden-vector test suite.
- **Node dependency in a Python repo.** Contained to `make ts-types`.
  `npx --yes` with pinned version avoids `node_modules/` churn.
- **Hand-written schemas could drift from Pydantic over time.**
  Mitigated by the conformance test running under `make test`; any PR
  that changes a model without updating the schema fails CI.

## Success Criteria

- `make ts-types` produces a clean `.d.ts` that `tsc --noEmit` accepts
- Conformance test passes; re-running `make ts-types` is idempotent
- Consumer can `import type { Run, Step, Asset, Manifest, EmbedPolicy }`
  from the file and have shapes that match Pydantic serialization
- `CHANGELOG.md` entry under `[Unreleased]`
