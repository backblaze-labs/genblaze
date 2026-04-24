<!-- last_verified: 2026-04-24 -->
# GMI hardening + SDK-wide content-policy classification

## Goal

Address the high-leverage items from the external agent review of
`genblaze-gmicloud` without growing scope into the SDK's core abstractions.
Two concerns are interleaved in one execution:

- **PR-1a scope** — gmi connector correctness and DX (multi-image bug, base
  URL override, HTTP client injection, README fixes).
- **PR-1b scope** — SDK-wide additions that GMI needs but every provider
  benefits from (`CONTENT_POLICY` error code + spec regen, capability
  matrix in root README, dropped-params log visibility).

Shipping them together because PR-1b unblocks GMI's safety-rejection
classification, and the capability matrix is the single best answer to
"what does each connector do?"

## Non-goals (explicitly deferred)

- Pipeline-integrated `ChatProvider` — the standalone `chat()` already
  covers the LLM access need. Revisit only on concrete demand for
  run-level LLM provenance.
- `GMICloudSTTProvider` / `GMICloudVisionProvider` — real media-adjacent
  capabilities, but non-trivial design (text-as-Asset convention for
  transcripts / structured JSON). Separate follow-on plan.
- Shared `GMICloudClient` factory — becomes documentation after
  `http_client=` injection lands. Not a separate PR.
- Rename `gmicloud` → `gmi` — ecosystem-breaking rename for taxonomic
  cleanup. PyPI alias (`genblaze-gmi`) at release time instead.
- Rename `GMICloudAudioProvider` → `GMICloudTTSProvider` — breaking class
  rename. Clarify via docstring.
- Class-level `resolve()` alias for `resolve_canonical()` — one-mention
  DX complaint doesn't justify permanent API-surface bloat.
- Embeddings connector — outside the media-generation thesis.
- Token streaming / new `StreamEvent` variants — prior decision.

## PR-1a — gmi connector (zero core changes)

### 1. Multi-image output (correctness bug)

**Problem:** `extract_media_url()` in `_base.py` returns only the first URL
from the `media_urls` envelope; `GMICloudImageProvider.fetch_output`
consumes it, producing a single `Asset` even when the user requests N
images. Silent overcharge.

**Fix:** Add sibling `extract_media_urls() -> list[str]` that returns all
URLs in the envelope (with legacy flat `*_url` fallback demoted to a
one-item list). Refactor `extract_media_url` to delegate
(`return urls[0] if urls else None`) so video / audio paths stay intact.
Image `fetch_output` iterates and appends one `Asset` per URL.

### 2. HTTP client injection + `base_url` override

**Problem:** `_BASE_URL` is module-constant in `_base.py:21`. Enterprise,
staging, and VPC deployments need an override. No mechanism to share a
single `httpx.Client` across the three modality provider instances.

**Fix:** Extend `GMICloudBase.__init__` with:

- `base_url: str | None` — ctor override; fallback to `GMI_BASE_URL` env,
  then module default.
- `http_client: httpx.Client | None` — external client injection (tests,
  proxies, shared across multi-modality pipelines).

Track `_owns_client: bool` so `close()` only tears down internally-created
clients. Mirrors the `own_client` pattern already in `chat.py` for
symmetry with the standalone entry points. When `http_client=` is passed,
`api_key`, `base_url`, and `http_timeout` are ignored (client is
pre-configured) — document in the docstring.

### 3. Audio-provider input semantics (documentation)

**Claim in external review:** `supported_inputs=["text","audio"]` is
misleading — users read "audio" as STT support.

**Reassessment:** The existing vocabulary is architecturally correct —
`supported_inputs` is a modality list. The disambiguation comes from
the `(supported_inputs, supported_modalities)` pair: `audio → audio`
means voice cloning; `audio → text` would mean STT. Don't invent
role-tokens like `"reference_audio"` — that pollutes the vocabulary and
breaks cross-provider introspection.

**Fix:** Keep the list unchanged. Add a docstring clarifier that audio
input is reference voice for cloning and STT is not supported here.

### 4. README corrections

- Remove the "Two auth modes — API key or SDK email/password" claim (only
  API key is implemented). Untruthful docs erode trust faster than they
  close issues.
- Add a **Naming reference** callout so users see the five surfaces
  (PyPI, import, class prefix, env var, entry-point slug) at a glance.
- Surface `chat()` / `achat()` prominently so the LLM access path is
  discoverable.
- Show the canonical `run.steps[0]` idiom — always check `step.status` /
  `step.error` before reading `step.assets[0]`.

## PR-1b — SDK-wide additions

### 5. `ProviderErrorCode.CONTENT_POLICY`

**Problem:** Safety rejections surface as `UNKNOWN` or `INVALID_INPUT`,
conflating policy refusals with transient validation errors. Retry logic
and user-facing messaging can't distinguish them.

**Fix:**

- Add `CONTENT_POLICY = "content_policy"` to
  `genblaze_core.models.enums.ProviderErrorCode`.
- Extend `libs/spec/schemas/manifest/v1/step.schema.json` enum.
- Regenerate `libs/spec/ts/genblaze.d.ts` via `make ts-types`.
- Extend `classify_api_error()` in
  `libs/core/genblaze_core/providers/base.py` to detect the content-policy
  keywords (`content_policy`, `content policy`, `safety`, `safety_filter`,
  `content filter`, `policy violation`). All three provider-specific
  mappers inherit this for free.
- Not added to `RETRYABLE_ERROR_CODES` — content refusals are deterministic.

### 6. Dropped-params visibility

**Problem:** `ModelRegistry` silently drops non-allowlisted params at
`DEBUG` level. Production logs rarely surface DEBUG; users don't see that
their `width=2048` was thrown away.

**Fix:** Bump the existing `logger.debug(...)` line to `logger.info(...)`
at `model_registry.py:262`. One-line change. No vocabulary or API
expansion — just visibility.

### 7. Provider × modality capability matrix

**Problem:** No single-page answer to "which connector does what?". Every
external investigation started by grepping three packages.

**Fix:** Add a compact matrix to the root `README.md` showing, per
connector, which modalities are generated and whether standalone `chat()`
is available. Manually maintained with an in-file comment noting the
update policy.

## Files touched / created

| Path | Kind |
|---|---|
| `docs/exec-plans/active/gmi-hardening.md` | new (this) |
| `libs/connectors/gmicloud/genblaze_gmicloud/_base.py` | edit (base_url, http_client, extract_media_urls) |
| `libs/connectors/gmicloud/genblaze_gmicloud/image.py` | edit (iterate media_urls) |
| `libs/connectors/gmicloud/genblaze_gmicloud/audio.py` | edit (docstring clarifier) |
| `libs/connectors/gmicloud/tests/test_gmicloud_image_provider.py` | edit (multi-image test) |
| `libs/connectors/gmicloud/tests/test_gmicloud_provider.py` | edit (base_url + http_client tests) |
| `libs/connectors/gmicloud/README.md` | edit |
| `libs/core/genblaze_core/models/enums.py` | edit (add CONTENT_POLICY) |
| `libs/core/genblaze_core/providers/base.py` | edit (classify content-policy keywords) |
| `libs/core/genblaze_core/providers/model_registry.py` | edit (DEBUG → INFO) |
| `libs/core/tests/unit/test_model_registry.py` or new test | edit/new (CONTENT_POLICY classification) |
| `libs/spec/schemas/manifest/v1/step.schema.json` | edit (enum value) |
| `libs/spec/ts/genblaze.d.ts` | regenerate |
| `README.md` | edit (capability matrix) |
| `CHANGELOG.md` | edit |

## Verification

- `make test` — full gate, all 13 packages
- `make lint`
- `make ts-types-check` — TS drift guard

## Principles applied

- **Media-generation-first moat intact.** No text modality leakage into
  `Step` / `Asset` / `Pipeline`. The one new enum value (`CONTENT_POLICY`)
  benefits every provider, not just text.
- **No API-surface bloat.** No method aliases, no parallel vocabularies
  (rejected `"reference_audio"` role-token), no new public classes beyond
  what's strictly additive.
- **Pattern consistency.** `http_client=` + `_owns_client` mirrors the
  already-shipped `chat.py` pattern. The `extract_media_urls` plural
  mirrors how `models/asset.py` already handles lists.
- **Truth in documentation.** Remove the false auth claim outright
  instead of trying to implement SDK email/password.
- **Discoverability over documentation.** The capability matrix earns the
  single biggest cognitive win for 30 lines of markdown.
