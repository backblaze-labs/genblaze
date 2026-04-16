<!-- last_verified: 2026-03-06 -->
# Feature: Manifest Provenance

## Purpose
Produce hash-verified, canonical JSON manifests that capture full provenance of generative media pipeline runs.

## Used By
- API: `Manifest`, `Manifest.from_run()`, `canonical_json()`
- CLI: `extract`, `verify` commands

## Core Functions
- `Manifest.from_run(run)` — Construct manifest from run and compute hash
- `Manifest.verify()` — Validate canonical_hash matches content
- `canonical_json()` — Deterministic serialization (sorted keys, normalized floats, NFC unicode)
- `Manifest.to_embed_json()` — Policy-filtered JSON for embedding

## Canonical Files
- Manifest model: `libs/core/genblaze_core/models/manifest.py`
- Canonical JSON: `libs/core/genblaze_core/canonical/json.py`
- Normalization: `libs/core/genblaze_core/canonical/_normalize.py`

## Inputs
- `Run` with populated `Steps` and `Assets`
- Optional `EmbedPolicy` for filtered output

## Outputs
- `Manifest` with `canonical_hash` (SHA-256 of canonical JSON)
- `schema_version`, `manifest_uri`, `signature` fields

## Flow
- `Manifest.from_run(run)` creates manifest and computes hash
- `compute_hash()` serializes to canonical JSON (deterministic key sort + float normalization + NFC)
- SHA-256 hash computed over canonical bytes
- Hash stored as `canonical_hash`
- `verify()` re-serializes and compares hash

## Edge Cases
- Float precision differences → normalization ensures consistency
- Unicode variants → NFC normalization before hashing
- Empty run (no steps) → valid manifest with empty steps list

## Verification
- Test files: `libs/core/tests/unit/test_canonical.py`, `libs/core/tests/unit/test_models.py`, `libs/core/tests/unit/test_unicode.py`
- Required cases: hash determinism, round-trip verify, float normalization, unicode NFC
- Quick verify: `cd libs/core && pytest tests/unit/test_canonical.py tests/unit/test_models.py tests/unit/test_unicode.py -v`
- Full verify: `make test`
- Pass criteria: canonical hash is deterministic across serialize/deserialize cycles
