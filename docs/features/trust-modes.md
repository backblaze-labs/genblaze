<!-- last_verified: 2026-04-27 -->
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
- The asset bytes have not changed since the manifest was written (asset.sha256 is included in the canonical hash payload — see caveat in [media-embedding.md](media-embedding.md)).
- The pipeline run is reproducible: same inputs always produce the same canonical_hash.

**What it does NOT prove:**
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

# Verify
assert manifest.verify()  # hash recomputes from canonical payload

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

`asset.sha256` in the manifest is computed against the asset bytes at the moment the manifest is built — i.e., **before** embedding. After `SmartEmbedder.embed()` modifies the file to insert the manifest, the on-disk file's sha256 will not match `asset.sha256`. Two paths to verify the asset:

1. **Verify against the upstream artifact** — keep the original asset (e.g., in B2 storage) and recompute sha256 from those bytes. Recommended for any sink that already uploads the asset.
2. **Strip-then-hash** — extract the manifest, remove the embed region per format, re-hash the remaining bytes. Format-specific; not currently shipped as a helper. C2PA's hard-binding algorithm in Mode 3 solves this for free.

## Choosing a mode

- Building an internal pipeline whose output stays in your own bucket → **Mode 1 is sufficient.**
- Publishing assets to customers and need to prove "this came from us" → **wait for Mode 2** or layer your own signing on the embedded JSON today.
- Publishing into an ecosystem that displays provenance badges → **wait for Mode 3** or use [c2pa-python](https://github.com/contentauth/c2pa-python) directly today.

## Verification
- Mode 1 test files: `libs/core/tests/unit/test_canonical.py`, `test_canonical_hash_stability.py`, `tests/integration/test_pipeline_embed_roundtrip.py`
- Required cases: hash determinism, embed→extract→verify roundtrip per format, asset.sha256 binding
- Quick verify: `cd libs/core && pytest tests/unit/test_canonical.py tests/integration/test_pipeline_embed_roundtrip.py -v`
- Full verify: `make test`
- Pass criteria: every roundtrip test reports `manifest.verify() == True`
