<!-- last_verified: 2026-04-23 -->
# genblaze-spec

Language-neutral contract for genblaze manifests.

Ships:

- **`schemas/manifest/v1/`** — Draft 2020-12 JSON Schemas for the
  `Run` / `Step` / `Asset` / `Manifest` / `EmbedPolicy` wire format.
  These are the authoritative wire contract — stricter than what
  Pydantic's `model_json_schema()` auto-generates (closed objects,
  uuid/uri formats, sha256 patterns, required fields that reflect
  serialization behavior rather than input validity).
- **`ts/genblaze.d.ts`** — TypeScript type declarations generated from
  the schemas. Drop this into a frontend/Node project and import the
  types directly.

## Why

Consumers that parse or render manifests from TypeScript were
hand-writing interfaces against the Python Pydantic models and drifting
— inventing fields that don't exist (`b2_key`, `videoRun`) and omitting
fields that do (`asset_id`, `metadata`, `width`/`height`/`video`/`audio`).
The schemas + generated types eliminate that drift by making the
contract a single source of truth that both languages consume.

## Consuming the TypeScript types (phase 1a)

Until `@genblaze/spec` is published to npm (phase 1b), consume via git:

```bash
# option 1: vendor the file
curl -o src/types/genblaze.d.ts \
  https://raw.githubusercontent.com/backblaze-labs/genblaze/main/libs/spec/ts/genblaze.d.ts

# option 2: git submodule (keeps updates easy)
git submodule add https://github.com/backblaze-labs/genblaze vendor/genblaze
```

Then:

```ts
import type { Manifest, Run, Step, Asset, EmbedPolicy } from "./types/genblaze";

function render(m: Manifest) {
  for (const step of m.run.steps) {
    // step.step_id, step.status, step.assets — all typed
  }
}
```

Types follow the serialized JSON shape exactly: `run.run_id` (not `.id`),
`step.step_id` (not `.id`), `asset.url` (no separate `b2_key` —
`url` is the durable handle).

## Regenerating

```bash
make ts-types
```

This runs `libs/spec/scripts/generate-types.sh`, which invokes a pinned
`json-schema-to-typescript` via `npx` and writes `ts/genblaze.d.ts`.
The script is deterministic — rerunning with no schema changes produces
a byte-identical file.

## Drift prevention

Two guardrails stop schemas, Pydantic models, and generated types from
drifting apart:

1. **`libs/core/tests/unit/test_spec_conformance.py`** — asserts
   bidirectional field-set equality, matching enum values, closed
   `additionalProperties`, and that every field carries a description.
   Runs under `make test`.
2. **CI `ts-types` job** — regenerates the `.d.ts` and fails if the
   committed file would change. Any schema edit that doesn't also
   update `ts/genblaze.d.ts` is rejected at PR review.

## Versioning

Schema versioning tracks `Manifest.schema_version` (currently `1.5`).
Once published to npm (phase 1b), `@genblaze/spec` versions will move
lockstep with `genblaze-core` — a `genblaze-core@0.3.0` release
publishes `@genblaze/spec@0.3.0`, even if only Python changed. This
keeps "which types match my SDK?" answerable with a single version
number.

## Roadmap

- **Phase 1a (current)** — committed `.d.ts`, drift guards, consumed via git
- **Phase 1b** — publish `@genblaze/spec` to npm with provenance and
  dual ESM/CJS; ship raw schemas alongside types for runtime validation
  via `ajv`
- **Phase 2 (on demand)** — `@genblaze/spec/zod` subpath for ergonomic
  runtime validation with inferred types
- **Phase 3 (on demand)** — `@genblaze/manifest` with pure-TS canonical
  JSON + SHA-256, enabling client-side `canonical_hash` verification
  without a backend round-trip

See `docs/exec-plans/active/ts-type-codegen.md` for full rationale.
