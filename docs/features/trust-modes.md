<!-- last_verified: 2026-07-21 -->
# Feature: Trust Modes

## Purpose
Define what the genblaze provenance manifest does and does not prove, so downstream consumers can pick the right verification posture for their threat model.

## Used By
- API: `Manifest.verify()`, `Manifest.canonical_hash`, `Manifest.signature` (reserved)
- CLI: `genblaze verify <file>`
- Storage: any consumer reading manifests from B2 / S3 / sidecar / embedded media

## Three trust modes

Genblaze supports a layered trust model. Today only Mode 1 ships in core; Modes 2 and 3 are roadmap items with reserved schema fields already in place.

### Mode 1 — Integrity (default, ships today)

**What it proves:**
- The manifest content has not changed since it was written (canonical_hash recomputes).
- The manifest commits to each output asset's `asset.sha256` when that field is populated. A caller can prove fetched/stored bytes have not changed by hashing those bytes and comparing them to the committed digest — see caveat in [media-embedding.md](media-embedding.md).
- The pipeline run is reproducible: same inputs always produce the same canonical_hash.

**What it does NOT prove:**
- Byte integrity for URL-only output assets. Security-facing verification returns `False` until every output asset has `sha256` populated, typically by using `ObjectStorageSink`.
- That a specific party produced the manifest. Anyone with the SDK can build a self-consistent manifest from arbitrary inputs.
- Resistance to a determined re-embedder. A tamperer can modify the asset, recompute the manifest, re-embed, and produce a manifest that verifies against itself.

**When to rely on it:**
- "Did MY pipeline produce this asset?" — internal audit trail, replay validation, storage-corruption detection.
- "Are these inputs reproducible?" — CI drift detection, pipeline regression.
- Any context where the manifest reaches the verifier through a trusted channel (your own B2 bucket, your own database).

**API surface:**
```python
from genblaze_core import Manifest

# Build + hash
manifest = Manifest.from_run(run)

# Verify payload integrity and declared output sha256 coverage.
assert manifest.verify()

# Hash-only migration/read path.
assert manifest.verify_hash()

# CLI
# $ genblaze verify video.mp4
```

### Mode 2 — Authenticated integrity (roadmap)

**What it would prove:** Mode 1 + only the holder of a specific signing key could have produced the manifest.

**Mechanism (planned):** Pluggable `Signer` / `Verifier` interface; ship Ed25519 default. Bring-your-own-key, no PKI. The `signature` and `encryption_scheme` fields on `Manifest` are reserved (excluded from the canonical hash) for forward compatibility — adding signing in a future schema version is non-breaking.

**When you'd want it:** Multi-tenant SaaS attribution, brand publishing, internal compliance.

**Status:** Not yet implemented. Open an issue if your use case needs it.

### Mode 3 — Standards-verifiable (roadmap, opt-in)

**What it would prove:** Mode 2 + cross-tool verifiable by any consumer that speaks [C2PA](https://c2pa.org). Adobe, Microsoft, BBC, Leica, browser badges.

**Mechanism (planned):** Optional `genblaze-c2pa` adapter package that translates the genblaze manifest into a C2PA claim and signs with a customer-provided certificate. Genblaze manifest stays in B2 (full provenance, internal use); C2PA claim ships embedded in the asset for external verification. The two layers are complementary, not exclusive.

**When you'd want it:** Public distribution, journalism, asset publishing into ecosystems that display C2PA badges.

**Status:** Not yet implemented. Will be a separate optional install (`pip install genblaze[c2pa]`) so the core SDK stays pure-Python and dependency-light.

## Threat model summary

| Adversary | Mode 1 (today) | Mode 2 (signed) | Mode 3 (C2PA) |
|---|---|---|---|
| Storage bit-flip | Detected | Detected | Detected |
| Accidental edit | Detected | Detected | Detected |
| Tamperer with no SDK access | Detected | Detected | Detected |
| Tamperer with SDK access, no signing key | Detected as content change BUT can re-embed a self-consistent forged manifest | Detected (no key) | Detected (no key) |
| Tamperer with signing key | Not applicable | Compromised — rotate key | Compromised — rotate cert |
| Cross-org consumer with no shared trust | Cannot verify authorship | Verifies if has public key | Verifies via C2PA trust list |

## Asset binding caveat

`Manifest.verify()` changed in 0.3.4 from hash-only verification to a stricter
declared asset-binding check. A successful output asset without `asset.sha256`
does not verify, even when the manifest hash itself matches. This applies to
legacy schema versions too, so an attacker cannot set `schema_version="1.5"` to
bypass output sha256 coverage. Use `verify_hash()` for historical hash-only
CI gates or audits where URL-only outputs are expected.

`Manifest.verify()` and a bare `genblaze verify` do not fetch `asset.url` and
re-hash remote bytes. They verify the canonical manifest hash and require every
output asset to declare a syntactically valid lowercase sha256 digest.
`genblaze verify --fetch` performs that byte-level check as an opt-in CLI mode;
it streams each output asset through the SSRF-hardened transfer path and
compares fetched bytes to `asset.sha256` (path 1 below). It does not change
what `Manifest.verify()` or a bare `genblaze verify` accepts. Programmatic
consumers that dereference asset URLs should do the same independently before
trusting those bytes.

Use `Manifest.verify_hash()` when a caller only needs to check that
`canonical_hash` matches the manifest payload. This distinction matters for
storage reads and replay flows that should report URL-only outputs as
byte-unverified without treating them as hash tampering.

Schema 1.6 URL-only hash markers are Python read-supported, but the SDK still
writes schema 1.5 by default and the published JSON Schema/TypeScript spec stay
capped at 1.5 during the expand-contract rollout. Operators should upgrade all
readers before enabling 1.6 manifest emission. If 1.6 manifests are written and
a rollback is required, keep a reader with 1.6 hash-marker support available to
re-save or inspect those manifests; older 1.5 readers cannot verify the 1.6
URL-only hash payload.

Unknown future schema versions are rejected with an upgrade-required manifest
error. That is deliberate: every schema version defines canonical hash behavior,
so a reader that does not know the version cannot safely report provenance
verification.

`asset.sha256` in the manifest is computed against the asset bytes at the moment the manifest is built — i.e., **before** embedding. After `SmartEmbedder.embed()` modifies the file to insert the manifest, the on-disk file's sha256 will not match `asset.sha256`. Two paths to verify the asset:

1. **Verify against the upstream artifact** — keep the original asset (e.g., in B2 storage) and recompute sha256 from those bytes. `genblaze verify --fetch` performs this check from the CLI. Recommended for any sink that already uploads the asset.
2. **Strip-then-hash** — extract the manifest, remove the embed region per format, re-hash the remaining bytes. Format-specific; not currently shipped as a helper. C2PA's hard-binding algorithm in Mode 3 solves this for free.

## Choosing a mode

- Building an internal pipeline whose output stays in your own bucket → **Mode 1 is sufficient.**
- Publishing assets to customers and need to prove "this came from us" → **wait for Mode 2** or layer your own signing on the embedded JSON today.
- Publishing into an ecosystem that displays provenance badges → **wait for Mode 3** or use [c2pa-python](https://github.com/contentauth/c2pa-python) directly today.

## Verification
- Mode 1 test files: `libs/core/tests/unit/test_canonical.py`, `test_canonical_hash_stability.py`, `tests/integration/test_pipeline_embed_roundtrip.py`
- Required cases: hash determinism, embed→extract→verify roundtrip per format, asset.sha256 binding, URL-only output assets do not verify
- Quick verify: `cd libs/core && pytest tests/unit/test_canonical.py tests/integration/test_pipeline_embed_roundtrip.py -v`
- Full verify: `make test`
- Pass criteria: hashed-asset roundtrip tests report `manifest.verify() == True`; URL-only output manifests report `False`
