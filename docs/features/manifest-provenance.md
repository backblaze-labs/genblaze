<!-- last_verified: 2026-06-20 -->
# Feature: Manifest Provenance

## Purpose
Produce hash-verified, canonical JSON manifests that capture full provenance of generative media pipeline runs.

## Used By
- API: `Manifest`, `Manifest.from_run()`, `canonical_json()`
- CLI: `extract`, `verify` commands

## Core Functions
- `Manifest.from_run(run)` — Construct manifest from run and compute hash
- `Manifest.verify()` — Validate canonical_hash matches content and every output asset declares a valid lowercase `sha256`
- `Manifest.verify_hash()` — Validate only that canonical_hash matches the canonical payload
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
- `verify_hash()` re-serializes and compares the canonical hash
- `verify()` calls `verify_hash()` and returns `False` when any output asset lacks a valid lowercase `sha256`

## Edge Cases
- Float precision differences → normalization ensures consistency
- Unicode variants → NFC normalization before hashing
- Empty run (no steps) → valid manifest with empty steps list

## Hash payload vs canonical JSON

`Manifest.to_canonical_json()` and `_hash_payload()` (in `models/manifest.py`) produce
**different** byte sequences. The hash is NOT `sha256(to_canonical_json())`.

`to_canonical_json()` includes operational fields useful for diagnostics:
timestamps (`started_at`, `completed_at`), `status`, `error`, `error_code`,
`retries`, `cost_usd`, `provider_payload`, `step_id`, `run_id`, `asset_id`,
`asset.url`, `transfer_failures`, `signature`, `encryption_scheme`, `manifest_uri`.

`_hash_payload()` strips them before hashing because they are non-deterministic
(timestamps, random IDs) or transport-only (URL, signature). The exclusion sets
are version-keyed:

- `_RUN_HASH_EXCLUDE` — run_id, status, created_at, started_at, completed_at, idempotency_key, parent_run_id
- `_STEP_HASH_EXCLUDE` — step_id, run_id, status, error, error_code, retries, cost_usd, started_at, completed_at, provider_payload, step_index
- `_ASSET_HASH_EXCLUDE` — asset_id, url. Schema 1.6 Python read support keeps an explicit `asset_integrity=url_only_unverified` marker plus a canonicalized `unverified_asset_url` for assets without `sha256`. The URL form strips known presign credential/expiry parameters and fragments while retaining resource-identifying query parameters. Schema versions 1.4 and 1.5 preserve the previous URL-stripping rules for backwards verification, and the SDK plus published language-neutral spec continue to write/declare schema 1.5 during rollout.
- Schema versions ≤ 1.3 used the legacy exclusion set (random IDs were included in the hash)
- Unsupported schema versions are rejected with an upgrade-required parse error instead of inheriting the latest hash policy.

Third-party verifiers in other languages must apply the same strip rules before
recomputing SHA-256. The Python implementation in `_hash_payload()` is the
authoritative reference.

### Self-verification flow
1. Read the embedded / sidecar manifest JSON (full canonical form).
2. Parse with `parse_manifest(json.loads(text))` so schema migrations and manifest invariants are enforced.
3. Call `manifest.verify_hash()` to check only canonical payload integrity, or `manifest.verify()` to also reject URL-only output assets and malformed sha256 declarations.
4. If you will fetch asset URLs, hash those fetched bytes separately and compare them with `asset.sha256`; manifest verification does not perform network reads.

### Trust modes
The hash provides **integrity**, not **authentication**. See
[trust-modes.md](trust-modes.md) for what the manifest does and does not prove.

## Verification
- Test files: `libs/core/tests/unit/test_canonical.py`, `libs/core/tests/unit/test_models.py`, `libs/core/tests/unit/test_unicode.py`
- Required cases: hash determinism, round-trip verify, float normalization, unicode NFC
- Quick verify: `cd libs/core && pytest tests/unit/test_canonical.py tests/unit/test_models.py tests/unit/test_unicode.py -v`
- Full verify: `make test`
- Pass criteria: canonical hash is deterministic across serialize/deserialize cycles
