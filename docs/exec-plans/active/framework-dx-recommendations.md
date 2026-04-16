<!-- last_verified: 2026-04-16 -->
# Framework DX & Production Readiness

Tracks open recommendations for developer experience and production hardening.

## Open Items

| # | Recommendation | Effort | Priority |
|---|---------------|--------|----------|
| 1 | Chain mode examples (examples/ has no chain, input_from, or compositor examples) | 1 day | **P0** |
| 2 | Webhook HMAC signing (`X-Genblaze-Signature` header for payload authentication) | 1 day | **P1** |
| 3 | Resume error handling (wrap poll/fetch in same try/except as invoke) | 1 day | **P1** |
| 4 | Resume test expansion (currently 2 tests; needs async, progress, error cases) | 1 day | **P1** |
| 5 | Webhook retry jitter (match `_jittered_backoff()` pattern from providers/base.py) | 0.5 day | **P1** |
| 6 | C2PA/JWS manifest signing (authenticity, not just integrity) | 3-4 days | **P1** |
| 7 | Dynamic cost config (runtime override for hardcoded pricing dicts) | 2 days | **P2** |
| 8 | Runnable ABC simplification (deprecate unused pipe composition) | 1 day | **P2** |

## Resolved Since Last Review

- **Moderation system** — Pre/post step content filtering with audit trail. 16 tests.
- **PromptTemplate** — Parameterized prompts with batch rendering. 19 tests.
- **Pipeline templates** — Serializable pipeline definitions with provider resolution. 17 tests.
- **FFmpegTransform** — 5 local media transforms. Null metadata guards added. 17 tests.
- **Webhooks** — Event notification with retry, event filtering, SSRF protection. 12 tests.
- **Job resume** — `BaseProvider.resume()` / `aresume()` with checkpoint callback.
- **PipelineResult** — `failed_steps()` and `succeeded_steps()` convenience methods.

## Recommended Execution Order

1. Resume hardening (error handling + status check + tests) — prevents data loss
2. Webhook HMAC signing — security before deployment
3. Chain mode examples — developer onboarding
4. Remaining items by priority
