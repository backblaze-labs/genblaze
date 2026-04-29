<!-- created: 2026-04-29 -->
# Manifest signing and redaction

**Status:** active · **Owner:** crypto/auth subagent · **Target releases:** `genblaze-core` 0.3.1 (Phase 1), 0.3.3 (Phase 2 after master-plan Wave 5) · **Shape:** A (additive) + F (redaction sweep) · **Feedback refs:** framework-dx items #2 (webhook HMAC), #6 (manifest signing); tech-debt P1; P2-17 (webhook dev-mode); EmbedPolicy.encrypted P3

## Goal

Cryptographic authenticity for two surfaces — webhook payloads (HMAC) and manifest content (JWS detached signature) — without touching `Manifest` field schema until master-plan Wave 5 settles the canonical-hash inclusion registry. Sweep secret-leakage risk across tracers, error messages, and webhook payloads.

**Done when:** webhook consumers verify `X-Genblaze-Signature` against a shared secret and reject replays; callers `signer.sign(manifest)` and any third-party Python/Node/Go process verifies the resulting `.sig.json` sidecar without a network call; `_SECRET_PATTERNS` redaction is wired through `LangSmithTracer.on_run_start` / `on_chain_start` payloads; the unimplemented `EmbedPolicy.prompt_visibility="encrypted"` value is removed with a one-minor deprecation window.

## Subagent brief

### Engineering posture

You are an expert open-source SDK engineer with crypto/auth depth. Adopt established standards (RFC 7515 JWS, RFC 2104 HMAC, NIST guidance on replay windows). Never roll custom crypto; use the `cryptography` library. Never log signatures or secrets. Cross-language verifiability is the goal: Python signs, anyone verifies with a public key + standard library.

### Required reading (in order)

1. `AGENTS.md`, `ARCHITECTURE.md`, `CLAUDE.md`
2. `docs/exec-plans/feedback.md` — search "P2-17", "framework-dx"
3. `docs/exec-plans/active/framework-dx-recommendations.md`
4. `docs/exec-plans/tech-debt-tracker.md` — C2PA / signing rows (P1)
5. `libs/core/genblaze_core/webhooks/notifier.py` — webhook lifecycle, current SSRF guards, retry pattern
6. `libs/core/genblaze_core/observability/tracer.py` — Tracer ABC pattern
7. `libs/connectors/langsmith/genblaze_langsmith/tracer.py` — `on_run_start` / `on_chain_start` (the leak surface)
8. `libs/core/genblaze_core/_utils.py` — existing `_SECRET_PATTERNS`, `_sanitize_error`, `_reject_credentials_in_params`
9. `libs/core/genblaze_core/models/policy.py` — `EmbedPolicy.prompt_visibility`
10. `libs/core/genblaze_core/models/manifest.py` — `Manifest`, `canonical_hash`, the `manifest_uri` exclusion pattern (precedent for non-hashed fields)
11. `docs/exec-plans/active/p0-p1-feedback-execution.md` Wave 5 — dual-hash plan (Phase 2 of THIS plan must not collide)

### Success bar (review gate)

- **Bugs**: signature verification rejects tampered payload, expired window, mismatched key_id, swapped algorithm. Replay window enforced. Constant-time comparison via `hmac.compare_digest`.
- **Duplication**: extend `_SECRET_PATTERNS`; do not create a second pattern set. Extend the existing tracer redaction path; do not fork.
- **Performance**: HMAC + JWS are local operations. No network calls in signing path. Verifier loads keys once and reuses. ED25519 default (~50k ops/sec); RSA optional.
- **Scalability**: signing throughput ≥10k ops/sec on a single core for both HMAC and ED25519 — verified by a benchmark test.
- **Pattern-fit**: `Signer` ABC mirrors `Tracer` ABC. Crypto via `cryptography` only. No bespoke primitives.

## Phase 1 — Webhook HMAC + redaction sweep (Wk 3-4)

### A1 — Webhook HMAC signing

| File | Change |
|------|--------|
| `libs/core/genblaze_core/webhooks/signing.py` | **NEW.** `WebhookSigner(secret, *, algorithm="sha256")`; `sign(payload: bytes, *, timestamp: int) -> str` returns `t=<unix>,v1=<hex>`; `verify(header, payload, *, max_age_sec=300) -> bool` constant-time, replay-window enforced. |
| `libs/core/genblaze_core/webhooks/notifier.py` | `WebhookNotifier(signer=...)`; sets `X-Genblaze-Signature` header. Backwards-compatible — `signer=None` skips signing. |
| `libs/core/genblaze_core/webhooks/dev_mode.py` | **NEW.** `WebhookConfig(dev_mode=True)` permits `localhost`/`example.test`/non-HTTPS with WARN log (closes feedback P2-17). `CapturingWebhookTransport` for in-process tests. |
| `libs/core/tests/unit/test_webhook_signing.py` | **NEW.** Round-trip; tampered-payload reject; expired-window reject; replay-attack reject; constant-time verify (timing smoke test). |
| `docs/features/webhooks.md` | "Verifying signatures" section with Python / Node / Go pseudocode. |

### A3 — Secrets redaction sweep + drop EmbedPolicy.encrypted

| File | Change |
|------|--------|
| `libs/core/genblaze_core/_utils.py` | Extend `_SECRET_PATTERNS`: Bearer tokens, AWS access-key-id (`AKIA[0-9A-Z]{16}`), B2 key-id pattern, GCP service-account JSON shape. Add `redact_dict(d, patterns=...)` recursive helper. |
| `libs/connectors/langsmith/genblaze_langsmith/tracer.py` | Wrap `on_run_start` / `on_chain_start` payload values with `redact_dict()` before push. |
| `libs/core/genblaze_core/models/policy.py` | Deprecate `EmbedPolicy.prompt_visibility="encrypted"` via `@deprecated` helper from master-plan Wave 0.3. Removed in 0.4.0. |
| `libs/core/tests/unit/test_redaction.py` | **NEW.** Patterns covered, recursive dict walk, tracer payload sanitized, deprecation warning emitted. |

## Phase 2 — Manifest signing v1 (Wk 9-10, AFTER master-plan Wave 5 lands)

### A2 — Signer ABC + sidecar JWS only

**Why post-Wave 5:** Wave 5 adds `sha256_embedded` to `Asset` and establishes the canonical-hash inclusion registry for optional fields. Shipping `Manifest.signature` *field* before Wave 5 either invalidates in-flight signed manifests or produces a hash-inclusion rule inconsistent with Wave 5. Sidecar-only A2 dodges the collision: no `Manifest` field changes, no `Pipeline(signer=...)` API surface.

| File | Change |
|------|--------|
| `libs/core/genblaze_core/signing/base.py` | **NEW.** `Signer` ABC: `sign(manifest: Manifest) -> SignedSidecar`; `verify(sidecar: SignedSidecar, manifest: Manifest) -> VerifyResult`. `SignedSidecar(payload_hash, algorithm, key_id, signature, signed_at)`. |
| `libs/core/genblaze_core/signing/jws.py` | **NEW.** `JWSDetachedSigner`. ED25519 default; RSA optional. PEM key loading. Writes/reads `.sig.json` sidecar alongside the manifest. |
| `libs/core/tests/unit/test_signer_jws.py` | **NEW.** Sign + verify round-trip; tampered-manifest reject; mismatched key_id reject; benchmark asserting ≥10k sign+verify ops/sec; cross-language golden vector matching Node `jose` library output. |
| `docs/features/signing.md` | **NEW.** When to sign, key-rotation pattern, sidecar vs (future) inline trade-off, cross-language verification recipe. |

## Cross-plan dependencies

- **Phase 2 blocks on** master-plan Wave 5 landing first.
- **No dependency on** Plans 1, 3, 4. Phase 1 is independent of everything.
- **Required by** future C2PA work — `Signer` ABC is the seam C2PA assertions plug into.

## Acceptance gates

- [ ] `make test && make lint && make typecheck` green per phase
- [ ] Webhook signature verification documented for Python, Node, Go
- [ ] JWS sign/verify round-trip golden vector matches Node `jose` output (committed cross-language proof)
- [ ] Signing benchmark ≥10k ops/sec for both HMAC and ED25519
- [ ] LangSmith tracer payload sanitization test covers `api_key`, Bearer, AWS, B2, GCP patterns
- [ ] `EmbedPolicy.prompt_visibility="encrypted"` emits `DeprecationWarning`; documented for 0.4.0 removal
- [ ] CHANGELOG: `### Added` (signing), `### Security` (redaction sweep + webhook HMAC), `### Deprecated` (EmbedPolicy.encrypted)

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Cross-language JWS verify drift | Golden-vector test against Node `jose` output committed to repo |
| Webhook secret leakage in logs | Constant-time compare, never log raw secret, redaction sweep covers env-var-shaped patterns |
| Subagent introduces custom crypto | Review gate forbids any non-`cryptography` primitive |
| Secret patterns produce false positives that mask legitimate data | Redaction is opt-in per tracer; structured fields (`api_key=`, `Authorization:`) only — not free-text scanning |

## Out of scope

- C2PA assertions (own design doc and plan)
- Inline manifest signing (post-Wave 5 follow-up)
- Key-management infrastructure (Vault, KMS, etc.) — caller owns
- Public/private manifest split (tech-debt P2; own plan)
