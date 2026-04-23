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
| 8 | Runnable ABC simplification (deprecate unused pipe composition) | 1 day | **P2** |

## Resolved Since Last Review

- **Model registry** — `ModelSpec` / `ModelRegistry` / `PricingStrategy` unify per-model config across all 12 provider connectors. Users can register new models, override pricing, and customize parameter handling at runtime via `Provider(models=reg)` or `Provider.models_default().fork()`. Resolves item #7 (Dynamic cost config). See `docs/features/model-registry.md`.
- **Moderation system** — Pre/post step content filtering with audit trail. 16 tests.
- **PromptTemplate** — Parameterized prompts with batch rendering. 19 tests.
- **Pipeline templates** — Serializable pipeline definitions with provider resolution. 17 tests.
- **FFmpegTransform** — 5 local media transforms. Null metadata guards added. 17 tests.
- **Webhooks** — Event notification with retry, event filtering, SSRF protection. 12 tests.
- **Job resume** — `BaseProvider.resume()` / `aresume()` with checkpoint callback.
- **PipelineResult** — `failed_steps()` and `succeeded_steps()` convenience methods.
- **Streaming** — `Pipeline.stream()`/`astream()` yield `StreamEvent` iterators; `preview_url` on `ProgressEvent`. See `docs/features/streaming.md`.
- **Tracer abstraction** — Pluggable `Tracer` ABC with NoOp/Logging/OTel/Composite backends; `structured_log=True` still works. See `docs/features/observability.md`.
- **LangSmith connector** — `genblaze-langsmith` package with `LangSmithTracer`.
- **Agent loop** — `AgentLoop` + `Evaluator` for generate→evaluate→retry with manifest lineage. See `docs/features/agents.md`.

## Recommended Execution Order

1. Resume hardening (error handling + status check + tests) — prevents data loss
2. Webhook HMAC signing — security before deployment
3. Chain mode examples — developer onboarding
4. Remaining items by priority
