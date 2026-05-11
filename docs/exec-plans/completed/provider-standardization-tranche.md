<!-- last_verified: 2026-04-24 -->
# Provider standardization tranche

**Status:** in-progress · **Owner:** core · **Target release:** `genblaze-core 0.3.0`
· **Shape:** A (additive contracts) + B (Pipeline `raise_on_failure` flip in 0.4.0)
· **Feedback ref:** items #1-#17 from the 10-agent GMI build report; ties to
P0-03, P1-01, P2-03, P2-09, P2-11, P2-12 in `feedback.md`.

## Problem

The 10-agent build report exposed pain that isn't GMI-specific — it's the
absence of a small set of provider contracts that every connector should
implement. Today each connector independently re-derives:

- whether & how to verify credentials before the first long-running call,
- how to publish a voice catalog for TTS models,
- how to probe whether a registered model id is alive,
- how to compose per-model param allowlists,
- how to estimate cost without an actual generation,
- how to enumerate models for discovery.

Where shared mechanics exist (`ModelSpec`, `ModelRegistry`,
`prepare_payload`), the GMI connector opts out by sharing one
`_COMMON_ALLOWLIST` across every model. Result: a class of "looks fine,
output is wrong" bugs and a credibility gap when shipped defaults are dead.

The fix is a base-class contract layer with a parametric conformance test
suite. Every connector inherits the new abstractions; failing
contract tests block release.

## Architecture

Three principles:

1. **Define contracts in `libs/core/genblaze_core/providers/`** — every new
   helper is a `BaseProvider` method (default no-op) so apps code against
   the abstract interface, never per-connector specifics.
2. **Enforce contracts in `libs/core/tests/conformance/`** — parametric
   tests over every entry-point-discovered provider; failures block release.
3. **Editorial / curated data lives with the connector** — voice catalogs,
   recommended-use tags, quality tiers ride alongside the model registry
   that owns them. Core is contract-only.

### Files added

```
libs/core/genblaze_core/
├── models/
│   └── voice.py                      # Voice pydantic model
├── providers/
│   ├── params.py                     # ParamSurface composable allowlist builder
│   └── probe.py                      # ProbeResult enum + helpers
└── tests/conformance/
    ├── __init__.py
    └── test_provider_contract.py     # parametric over discover_providers()

libs/core/genblaze_core/exceptions.py  # +PipelineError
tools/
├── probe_models.py                   # CI: probe every default id per provider
└── gen_model_matrix.py               # CI: regenerate README model matrix
```

### Files extended

```
libs/core/genblaze_core/providers/base.py            # preflight_auth, probe_model,
                                                     # estimate_cost, list_voices,
                                                     # list_models
libs/core/genblaze_core/providers/model_registry.py  # __iter__, items, __contains__,
                                                     # __len__, WARN-level drop log
libs/core/genblaze_core/pipeline/pipeline.py         # raise_on_failure,
                                                     # batch_run(items=),
                                                     # estimated_cost()
libs/core/genblaze_core/__init__.py                  # new lazy exports
libs/core/genblaze_core/providers/__init__.py        # new re-exports
```

### Open design decisions (locked)

1. **Voice contract on `BaseProvider`, not a new `AudioProvider` ABC.** All
   other capability hooks live on `BaseProvider`; adding an audio-specific
   subclass would break single-inheritance for connectors that mix
   modalities (GMI audio handles both TTS and music). Default returns `[]`.
2. **Voice catalog refresh: quarterly manual** for static catalogs (GMI,
   OpenAI TTS, NVIDIA Riva). Live-API providers (ElevenLabs, LMNT) call
   upstream with 5-minute caching.
3. **`probe_model()` opt-in + polite by default.** Default returns
   `ProbeResult.SKIPPED`. CI runs weekly, not per-PR. Connectors that can't
   do a cheap probe stay opted-out.
4. **`fail_fast` keeps its name; the contract changes.** Two-release
   deprecation cycle. In 0.3.0, `raise_on_failure=None` (sentinel) emits a
   `DeprecationWarning` describing the 0.4.0 default flip. In 0.4.0,
   `raise_on_failure` defaults to `True` and `Pipeline.run` raises
   `PipelineError(result=...)` when any step failed.

## Phased delivery

Each phase is one PR. Conformance tests added in Phase 0 are required-CI
from that point on, so every later PR inherits the gate.

### Phase 0 — Contracts (additive, ships in `genblaze-core 0.3.0`)

- [x] `libs/core/genblaze_core/models/voice.py` — `Voice` pydantic model.
- [x] `libs/core/genblaze_core/providers/params.py` — `ParamSurface` builder
      with modality-default surfaces.
- [x] `libs/core/genblaze_core/providers/probe.py` — `ProbeResult` enum +
      shared helpers.
- [x] `libs/core/genblaze_core/providers/base.py` — extend `BaseProvider`
      with `preflight_auth`, `probe_model`, `estimate_cost`, `list_voices`,
      `list_models`. Wire `preflight_auth` once-per-instance into the
      submit lifecycle.
- [x] `libs/core/genblaze_core/providers/model_registry.py` — add
      `__iter__`, `items`, `__contains__`, `__len__`. Bump param-drop log
      from INFO to WARN.
- [x] `libs/core/genblaze_core/__init__.py` + `providers/__init__.py` —
      lazy exports for `Voice`, `ParamSurface`, `ProbeResult`.
- [x] `libs/core/tests/conformance/test_provider_contract.py` — parametric
      tests over `discover_providers()`.

### Phase 1 — Pipeline behavior (semi-breaking, ships in `genblaze-core 0.3.0`)

- [x] Add `PipelineError(GenblazeError)` carrying `.result`,
      `.failed_step_index`, `.failed_step_error`.
- [x] Add `raise_on_failure: bool | None = None` to `Pipeline.run` /
      `arun`. `None` (default) emits `DeprecationWarning` and behaves as
      today. `True` raises. `False` returns the failed `PipelineResult`.
- [x] `Pipeline.batch_run(items=[...])` overload — each dict merges into
      step 0 params with `prompt` override support.
- [x] `Pipeline.estimated_cost()` — sums per-step `BaseProvider.estimate_cost()`
      results; returns `None` if any step is non-estimable.

### Phase 2 — GMI registry hygiene (ships in `genblaze-gmicloud 0.2.4`)

Tracked separately in `gmi-registry-reconciliation.md`. This tranche owns
the contract Phase 2 depends on; reconciliation tracks the connector-side
work.

### Phase 3 — GMI hook implementations (ships in `genblaze-gmicloud 0.2.4`)

- [x] `GMICloudBase.preflight_auth()` — `GET /` with 5s timeout.
- [x] `GMICloudBase.probe_model()` — POST `/requests` with empty payload,
      distinguish 404 (NOT_FOUND) from 400 (OK).
- [x] `GMICloudAudioProvider.list_voices()` — read from new
      `models/voices.py` curated catalog.

### Phase 4 — CI / tooling (ships in repo infra)

- [x] `tools/probe_models.py` — walks every provider via
      `discover_providers()`, calls `probe_model()` for every default id,
      writes JSON report. Fails if any default returns `NOT_FOUND`.
- [x] `tools/gen_model_matrix.py` — emits markdown model matrix between
      `<!-- MODEL_MATRIX_START -->` markers in README.
- [ ] GitHub Action: `model-probe.yml` (weekly + on `models/**` PR diff).
- [ ] GitHub Action: `model-matrix-staleness.yml` (fails if README markers
      drift).

### Phase 5 — Roll the contract across other connectors (rolling)

Each connector PR adds `preflight_auth()`, `probe_model()`, and (audio
connectors) `list_voices()`. Order by user volume:

- [ ] `genblaze-openai` — `GET /v1/models` for both probe and preflight; static voice list for TTS.
- [ ] `genblaze-google` — `genai.list_models(page_size=1)` for preflight; catalog probe.
- [ ] `genblaze-elevenlabs` — `GET /v1/user/subscription` preflight; live `GET /v1/voices` for `list_voices` with cache.
- [ ] `genblaze-replicate` — `GET /v1/account` preflight; SDK `models.get(slug)` for probe.
- [ ] `genblaze-runway` — `GET /v1/organization` preflight.
- [ ] `genblaze-luma` — `GET /dream-machine/v1/credits` preflight.
- [ ] `genblaze-decart` — `GET /me` preflight.
- [ ] `genblaze-stability-audio` — `GET /v1/user/account` preflight.
- [ ] `genblaze-lmnt` — `GET /voices?limit=1` preflight + `list_voices` (cached).
- [ ] `genblaze-nvidia` — `GET /v1/models` preflight + `list_voices` for Riva.

### Phase 6 — Storage helper (ships in `genblaze-core 0.3.1`)

- [ ] `genblaze_core.storage.stage_input(path, sink)` — uploads a local
      file via the active sink and returns an `Asset` with the durable URL.
      Closes the i2v / reference-image flow for real user uploads.
- [ ] `docs/features/local-file-inputs.md` — recipe.

### Phase 7 — Docs (ships continuously)

- [ ] README per-modality quickstarts: image, image-to-image, TTS, music,
      video, vertical-9:16, batch fan-out.
- [ ] `docs/reference/model-matrix.md` (auto-generated, embedded into README).
- [ ] `docs/features/character-consistency.md` — seed + reference-image +
      prompt-prefix recipes for multi-asset coherence.
- [ ] `docs/features/prompt-semantics.md` — what `prompt=` means per
      modality.
- [ ] Populate `ProviderCapabilities.resolutions` per-model spec; consumed
      by `gen_model_matrix.py`.

## Acceptance criteria (tranche-wide)

- [ ] `make test` green at every phase boundary.
- [ ] New conformance tests cover every entry-point-registered provider
      and pass.
- [ ] CHANGELOG entries:
  - `### Added` for new contracts (Voice, ParamSurface, ProbeResult,
    `BaseProvider` hooks, `Pipeline.estimated_cost`, `batch_run(items=)`).
  - `### Deprecated` for `raise_on_failure=None` default with 0.4.0 flip
    callout.
- [ ] Two-release migration guide for `raise_on_failure` in
      `docs/migrations/0.3-to-0.4.md` (added in this PR for forward visibility).

## Migration / deprecation notes

- **Phase 0** is fully additive. Every new method has a default
  no-op/pass-through. No connector changes required to keep existing tests
  green; conformance tests assert the defaults, not custom impls.
- **Phase 1** introduces a `DeprecationWarning` for callers that don't
  explicitly pass `raise_on_failure=`. The release note documents the
  0.4.0 default flip; one-line migration:
  `pipeline.run(raise_on_failure=False)` to keep current behavior.
- **Phases 2-3** are connector-internal; users see them as bug fixes plus
  new methods. No migration required.
- **Phase 4** is CI-only. Failing probes block the release; passing
  probes update `docs/reference/model-probe-status.json` automatically.

## Out of scope (intentionally)

- `AudioProvider` ABC subclass tree (collapsed into a single
  `BaseProvider.list_voices` method).
- `Modality.EMBEDDING` and vector primitives (`feedback.md` P2-13 — own
  exec plan).
- StepType extension for analysis workflows (`feedback.md` P0-04, P0-05,
  P0-06 — separate analysis-shaped tranche).
- Multi-step per-item params in `batch_run(items=)` (defer; single-step is
  95% of use cases).
