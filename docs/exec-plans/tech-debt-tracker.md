<!-- last_verified: 2026-04-16 -->
# Tech Debt Tracker

Track known tech debt. Update when debt is discovered or resolved.

| Description | Impact | Proposed Resolution | Priority |
|-------------|--------|---------------------|----------|
| C2PA/signing not implemented | `signature` field is schema-only placeholder; no signing or tamper-proof provenance | Design doc for key management + C2PA assertion generation | P1 |
| Manifest encryption not implemented | `encryption_scheme` field is schema-only placeholder; no encrypted embedding | Design doc for encryption strategy (key exchange, at-rest encryption) | P2 |
| Public/private manifest split not implemented | No mechanism to store full manifest server-side while embedding only pointer | Implement manifest storage API + pointer mode integration | P2 |
| Hardcoded pricing dicts in providers | Pricing data drifts silently; no runtime override | Extract to YAML data file with `last_verified` dates + runtime override API | P2 |
| Runnable ABC overhead | `Runnable[In, Out]` implies pipe composability that doesn't exist | Simplify to standalone ABCs for Pipeline/BaseProvider; deprecate Runnable | P2 |
| `EmbedPolicy.prompt_visibility` accepts `encrypted` but downstream logic has no semantics for it | Surprising — EmbedPolicy only acts on `public`/`private`/`redacted` in `to_embed_json()` | Narrow field to `Literal["public","private","redacted"]` or document `encrypted` as equivalent to `public` | P3 |
