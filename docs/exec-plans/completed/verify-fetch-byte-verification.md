<!-- last_verified: 2026-07-21 -->
# `genblaze verify --fetch`: opt-in byte-level asset verification

## Problem

`genblaze verify` validates the manifest's canonical hash and requires every
output asset to declare a well-formed `sha256`, but never dereferences
`asset.url` to compare live bytes against the declared digest. The gap is
self-documented in five places:

- The command itself prints "Asset bytes were not fetched or compared."
- `README.md` (line 21): "callers that fetch `asset.url` should re-hash
  those bytes separately."
- `cli/README.md` (line 40): "does not fetch remote asset URLs; consumers
  … must hash those bytes separately."
- `docs/features/cli.md` (line 47): same consumer-responsibility language.
- `docs/features/trust-modes.md` (lines 90–93): "do not fetch `asset.url`
  and re-hash remote bytes."

trust-modes.md path 1 (line 116) recommends "Verify against the upstream
artifact: keep the original asset … and recompute sha256 from those bytes"
as the blessed verification path for embedded media, but no shipped tool
performs it. The consequence: `verify` can report OK while the stored asset
has been corrupted, truncated, or replaced; Mode 1's stated use case
("storage-corruption detection, internal audit trail") is not fully served
at the consumer layer.

## Fix

Add an opt-in `--fetch` flag to `cli/genblaze_cli/commands/verify.py`.
`--fetch` gives consumers a native, integrated way to do the byte-check
the docs already tell them to do manually; a real command they can run,
not a recipe they have to assemble themselves. Default behavior is
byte-for-byte unchanged; no core model or schema changes.

### Implementation (completed)

1. **`--fetch` flag**: after existing manifest-hash and sha256-coverage
   checks pass, stream each output asset's bytes and compare the computed
   SHA-256 to `asset.sha256`. `--fetch` and `--hash-only` are mutually
   exclusive.

2. **SSRF-hardened streaming**: stream the bytes via the SSRF-validated
   helper in `libs/core/genblaze_core/storage/transfer.py`
   (`_http_get_stream`; per-hop redirect validation, DNS pinning, existing
   max-bytes cap), hashing incrementally in chunks. Never buffer a full
   asset in memory. Decision to surface in the PR: import the private helper
   directly (CLI already depends on core) vs. promoting a thin public
   wrapper in `transfer.py`; neither touches models or schemas. Uses the
   transfer layer's existing `_DEFAULT_DOWNLOAD_TIMEOUT` and
   `_DEFAULT_MAX_DOWNLOAD_BYTES`.

3. **Chunked hashing**: 256 KB chunks (`_FETCH_CHUNK`), matching the
   transfer layer's streaming granularity. A whole asset is never held in
   memory. The download size cap (`_DEFAULT_MAX_DOWNLOAD_BYTES`) is
   checked after each chunk read, consistent with the transfer layer's
   own read-then-check pattern; overshoot is bounded by one chunk.

4. **`size_bytes` cross-check**: when `asset.size_bytes` is declared, the
   fetched byte count is compared after the digest matches.

5. **`file://` root restrictions**: hash the local file only when the
   resolved path is under the allowed roots (mirror `resolve_input_path`
   validation); otherwise fail the asset with a reason. `--allowed-root
   <dir>` (repeatable) extends the allowlist for pipelines run with
   `output_dir=` that write assets outside the temp-dir default.
   `--allowed-root` requires `--fetch`. URL-only assets without `sha256`
   are already rejected by the existing checks before `--fetch` runs.

6. **Per-asset failure isolation**: one unreachable, oversize, or
   mismatched asset does not abort verification of the rest. All failures
   surface in a single pass.

7. **Presigned URL redaction**: redact presigned URL query strings from all CLI output
   and error messages (reuse the redaction pattern from `_ffmpeg_utils.py`
   / #75; a presigned query string is a bearer credential).
   `_redact_url` and `_redact_text` also catch exceptions propagated from
   the transfer layer, where the URL can be echoed in error text.

8. **Default message update**: the no-flag OK message now ends with
   "add --fetch to verify the media itself" instead of dead-ending at
   "bytes were not fetched or compared."

9. **Vacuous-OK guard**: zero output assets with `--fetch` prints
   "no output assets to fetch" instead of a misleading "fetched and matched."

### Files touched

- `cli/genblaze_cli/commands/verify.py`: `_redact_url`, `_redact_text`,
  `_hash_url_bytes`, `_fetch_and_compare`, updated `verify` command with
  `--fetch`, `--hash-only`, `--allowed-root` options.
- `cli/tests/test_verify_fetch.py`: 14 tests: matching bytes, tampered
  bytes, size mismatch, multi-asset failure isolation, hash-only mutual
  exclusion, HTTPS streaming via pinned transfer, presigned query redaction,
  file:// outside allowed roots, default message points to --fetch, SSRF
  guard (no mocking; real security path), allowed-root admission,
  allowed-root requires --fetch, vacuous-OK for zero assets, HTTP scheme
  rejection.

### Docs (in the same PR)

- `README.md`: append "or run `genblaze verify --fetch`" to the re-hash
  sentence.
- `cli/README.md`: same pattern; add `--fetch` to the usage block.
- `docs/features/cli.md`: update edge-case bullet and add `--fetch` to
  verification section.
- `docs/features/trust-modes.md`: update lines 90–93 with opt-in clause;
  add `--fetch` pointer to path 1 (line 116).
- `docs/features/media-embedding.md`: add `--fetch` pointer to the
  "hash the upstream artifact" sentence (line 67).
- `docs/features/manifest-provenance.md`: add `--fetch` pointer to the
  self-verification step 4 (line 76).
- `CHANGELOG.md`: entry under `[Unreleased]` / genblaze-cli.

### Known limitations

- `--fetch` verifies the bytes returned at fetch time. It does not prove
  those bytes were not served from an intermediary cache or CDN. If origin
  freshness matters for the threat model, that should be called out
  explicitly; for immutable object storage this is usually acceptable.

### Out of scope

- No changes to `Manifest`, core models, or JSON Schema; deep verification
  stays CLI-side; no schema/ts-types churn.
- No strip-then-hash of embedded files; the verification target is the
  pristine pre-embed artifact at `asset.url`, per trust-modes.md path 1.
- No `http://` support. HTTPS-only, same policy as the transfer layer.

### Future scope

- Support a deliberate "new version" path for assets whose bytes are meant
  to change. The original manifest should stay immutable as the record of
  what was first produced; any accepted modification should create a new
  manifest linked back to the original rather than overwriting it in place.
  This separates verification of what exists now from intentional versioning
  of changed content later.

## Relationship to #77 / #100

PR #100 (issue #77, 3500+ lines) fixed model-level verification: URL-only
output assets now fail `Manifest.verify()`, and `ObjectStorageSink` populates
`sha256` at upload time. That work explicitly kept byte fetching out of the
default verification path; the "consumers must hash fetched bytes
themselves" language in the docs was written as part of #100.

This PR does not relitigate that decision. `--fetch` is opt-in, builds the
consumer-side tool those docs point to, and requires no core changes. If
maintainers prefer this as a docs recipe or example script instead of a
CLI flag, the PR can be reshaped accordingly.

## Risk

Low. Default `verify` behavior is unchanged (no `--fetch` = identical
output, tested). The fetch path reuses core's existing SSRF-hardened
`_http_get_stream` rather than introducing a new HTTP client. Private-API
import (`_http_get_stream`, `_DEFAULT_DOWNLOAD_TIMEOUT`,
`_DEFAULT_MAX_DOWNLOAD_BYTES`) is disclosed in the PR description.

## Verification

- `cd cli && pytest tests/test_verify_fetch.py -v`
- `mypy cli/genblaze_cli/ --ignore-missing-imports`
- `make lint`
- `make test`
