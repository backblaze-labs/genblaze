# Add AssemblyAI as a genblaze provider (speech-to-text)

> Status: active · Owner: Eduardo Pavez · Created 2026-06-23

## Context

AssemblyAI is a **speech-to-text / audio-intelligence** API — it *consumes* an audio
URL and *produces* a text transcript (+ word-level timing, speaker labels, optional
audio-intelligence). This is the inverse of every existing genblaze connector, which
generates media. We add it anyway because:

- It fits genblaze's primitives with **zero core changes**: `Modality.TEXT` already
  exists, `AudioMetadata.word_timings` (`WordTiming{word,start,end,confidence}`) is
  purpose-built for transcript timing, and `NvidiaChatProvider`
  (`libs/connectors/nvidia/genblaze_nvidia/chat_provider.py`) is a working precedent
  for a Pipeline provider that emits a **TEXT `Asset`** (`url="text:{sha256}"`,
  `media_type="text/plain"`, payload in `metadata["text"]`, sha256 over text bytes).
- It makes a transcribe step composable into pipelines (e.g. generate audio → transcribe,
  or caption a generated video) with full provenance: the transcript gets a
  manifest + canonical hash and lands in B2 like any other asset.

**Outcome:** a new `genblaze-assemblyai` connector package, installable as
`pip install genblaze-assemblyai`, discoverable via the `genblaze.providers` entry point,
that transcribes an audio URL into a hash-verified TEXT asset with word timings.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Base class | **`BaseProvider`** (submit/poll/fetch_output) | AssemblyAI's REST API is genuinely async (POST `/v2/transcript` → poll GET status). Gets adaptive polling, progress events, `resume()` crash-recovery, and poll caching for free — the right fit for long jobs. |
| v1 scope | **Transcription + word timings** | Core STT: audio URL → transcript TEXT asset, speaker labels, `word_timings`. Audio-intelligence flags pass through to the API and land in `metadata`, not first-class. |
| Output | TEXT `Asset` per the NvidiaChatProvider precedent | No new asset shape needed. |
| Input source | Resolve audio URL from (priority) `step.inputs[0].url` → `step.params["audio_url"]` → `step.prompt`; `validate_chain_input_url()` the chosen URL | Supports both standalone and chained pipeline use; satisfies the SSRF invariant. |
| Discovery | `DiscoverySupport.NONE` + one `ModelFamily` for known `speech_model` slugs + permissive `TEXT` fallback | AssemblyAI exposes no live `/models` catalog; the slug set is small/stable. |
| Pricing | User-registered, **per minute of *input* audio** via a custom strategy reading `audio_duration` from the response; ship **no** hardcoded price | Per-0.3.0 invariant: connectors ship zero prices. AssemblyAI bills per input-audio duration (new recipe shape — no existing helper). |
| Env var | `ASSEMBLYAI_API_KEY` | AssemblyAI convention. |
| SDK | `assemblyai` (PyPI; current 0.64.x) | Use `Transcriber().submit()` (non-blocking) + `aai.Transcript.get_by_id(id)` for polling. |

## Out of scope for v1 (follow-ups)

- **Real-time/streaming** transcription (websocket) — doesn't fit the Pipeline step lifecycle.
- **LeMUR** (LLM-over-transcript) — would mirror the standalone `chat()` shape, separate effort.
- **SRT/VTT subtitle outputs** and **first-class audio-intelligence** surfacing — both were
  offered and deferred; clean follow-ups once the core connector lands.

## Files to create — `libs/connectors/assemblyai/`

```
libs/connectors/assemblyai/
├── genblaze_assemblyai/
│   ├── __init__.py          # from .provider import AssemblyAIProvider; __all__
│   ├── provider.py          # AssemblyAIProvider(BaseProvider)
│   ├── _errors.py           # map_assemblyai_error(exc) -> ProviderErrorCode
│   └── py.typed             # empty PEP 561 marker
├── tests/
│   ├── __init__.py
│   └── test_assemblyai.py   # TestAssemblyAICompliance + provider-specific tests
├── pyproject.toml
└── README.md
```

### `provider.py` — shape (mirror `replicate/provider.py` for lifecycle, `nvidia/chat_provider.py` for the TEXT asset)

```python
class AssemblyAIProvider(BaseProvider):
    name = "assemblyai"
    discovery_support = DiscoverySupport.NONE

    @classmethod
    def create_registry(cls) -> ModelRegistry: ...   # ModelFamily(pattern for best|nano|universal*|slam-*) + TEXT fallback

    def __init__(self, api_key=None, *, poll_interval=3.0, models=None,
                 retry_policy=None, probe_cache_ttl=None, probe_cache_max_entries=None):
        super().__init__(models=models, retry_policy=retry_policy,
                         probe_cache_ttl=probe_cache_ttl, probe_cache_max_entries=probe_cache_max_entries)
        self._api_key = api_key; self._client = None

    def _get_client(self): ...        # lazy `import assemblyai`; ImportError -> ProviderError with pip hint

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.TEXT], supported_inputs=["audio"],
            accepts_chain_input=True, models=self._models.known(), output_formats=["text/plain"])

    def normalize_params(self, params, modality=None): ...   # language -> language_code (idempotent guards); pass-through rest

    def submit(self, step, config=None):     # resolve+validate audio_url; build aai.TranscriptionConfig(speech_model=step.model, **params)
        return self._get_client()... .submit(audio_url, config=cfg).id   # non-blocking; return transcript id

    def poll(self, prediction_id, config=None):   # t = aai.Transcript.get_by_id(id)
        if t.status in (completed, error):
            self._cache_poll_result(prediction_id, t); return True
        return False

    def fetch_output(self, prediction_id, step):
        t = self._get_cached_poll_result(prediction_id) or aai.Transcript.get_by_id(prediction_id)
        if t.status == error: raise ProviderError(t.error, error_code=map_assemblyai_error(t.error))
        text = t.text or ""; digest = sha256(text)
        asset = Asset(url=f"text:{digest}", media_type="text/plain", sha256=digest,
                      size_bytes=len(text.encode()),
                      audio=AudioMetadata(word_timings=[...]) if t.words else None,
                      metadata={"text": text, "language": ..., "confidence": ..., "utterances": ...})
        step.assets = [asset]
        step.provider_payload["audio_duration"] = t.audio_duration   # seconds; pricing reads this
        self._apply_registry_pricing(step)
        return step
```

### Key implementation gotchas (easy to get wrong)

1. **Milliseconds → seconds.** AssemblyAI returns word `start`/`end` in **ms**;
   `WordTiming` expects **seconds**. Divide by 1000.
2. **Verify SDK surface at the chosen floor.** Confirm `Transcriber().submit()` (non-blocking)
   and `aai.Transcript.get_by_id(id)` exist in the pinned `assemblyai` version. If not,
   fall back to raw REST via `httpx` (POST `/v2/transcript`, GET `/v2/transcript/{id}`,
   `Authorization: <API_KEY>` header, no `Bearer`).
3. **`step.model` is the `speech_model`** ("best" | "nano" | "universal" | "slam-1" | …).
   Map string → `aai.SpeechModel` enum where the SDK requires it; pass through otherwise.
4. **`expects_cost = False`** in the compliance subclass — we ship no pricing, so
   `step.cost_usd` is `None` unless the user registers a strategy (same as Hume).
5. **No core / spec change.** We add no fields to core Pydantic models, so **no
   `make ts-types` regen and no `test_spec_conformance` impact**. Keep it that way.

### `_errors.py` — `map_assemblyai_error(exc)` (status-code-first, like `hume/_errors.py`)

```python
def map_assemblyai_error(exc):
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status == 429: return RATE_LIMIT
        if status in (401, 403): return AUTH_FAILURE
        if status in (400, 422): return INVALID_INPUT
        if status >= 500: return SERVER_ERROR
    return classify_api_error(exc)   # shared string fallback; also used for transcript.error strings
```

### `pyproject.toml` (copy `libs/connectors/hume/pyproject.toml`, which is current 0.3.0)

- `name = "genblaze-assemblyai"`, `version = "0.3.0"` (match current release wave — confirm vs a sibling)
- `dependencies = ["genblaze-core>=0.3.0,<0.4", "assemblyai>=0.40,<1"]`
- `[project.entry-points."genblaze.providers"]` → `assemblyai = "genblaze_assemblyai:AssemblyAIProvider"`
- classifiers, `[tool.hatch.build.targets.wheel] packages = ["genblaze_assemblyai"]`,
  `[tool.pytest.ini_options] testpaths = ["tests"]`, `[tool.deptry]` block — all per the hume template.

### `tests/test_assemblyai.py`

- `class TestAssemblyAICompliance(ProviderComplianceTests)` with `expects_cost = False`;
  `_patch_sdk` fixture patching `sys.modules["assemblyai"]` (Hume pattern); `make_provider()`
  injects a fake whose `Transcriber.submit()` → fake id and `Transcript.get_by_id()` → a
  **completed** fake transcript with `.text`, `.words`, `.audio_duration`, `.status`.
- Provider-specific tests: `map_assemblyai_error` by status code; ms→s word-timing conversion;
  audio-URL resolution precedence (`inputs` > `params["audio_url"]` > `prompt`);
  `status == "error"` raises `ProviderError`; `normalize_params` idempotency.

## Files to modify

| File | Change |
|---|---|
| `Makefile` | Add 3 lines: `pip install -e libs/connectors/assemblyai` (`install`), `pip install -e "libs/connectors/assemblyai[dev]"` (`install-dev`), `cd libs/connectors/assemblyai && pytest -v` (`test`). **Without the `test` line, CI never runs the tests.** |
| `libs/meta/pyproject.toml` | Add an `assemblyai = ["genblaze-assemblyai"]` optional extra and include it in the `all` extra (confirm the meta package's extras layout first). |
| `README.md` | Add to the Install list and the "Configure API keys" table (`ASSEMBLYAI_API_KEY` → platform.assemblyai.com). The provider matrix is generation-only (Video/Image/Audio/Chat) — AssemblyAI fits none; add a short **Speech-to-Text / Transcription** note or a matrix row with a "transcription, TEXT output" footnote (keep minimal). |
| `ARCHITECTURE.md` | Add `genblaze-assemblyai` to the provider-adapters list and the External Services list. |
| `AGENTS.md` | Update the package count (15 → 16) and the connector enumeration in "Repo Purpose". |
| `docs/reference/pricing-recipes.md` | Add an AssemblyAI section: per-minute-of-input-audio strategy reading `step.provider_payload["audio_duration"]`, plus the upstream pricing URL. (New recipe shape — no existing helper.) |
| `CHANGELOG` | Add an entry under the current wave (per RELEASING/CONTRIBUTING). |

## Verification

1. `cd libs/connectors/assemblyai && pytest -v` — compliance harness (16 checks) + provider-specific tests pass.
2. From repo root: **`make test`**, **`make lint`**, **`make typecheck`** all green (AGENTS.md invariant).
3. Entry-point discovery: `python -c "from genblaze_core.providers import discover_providers; assert 'assemblyai' in {p.name for p in discover_providers()}"`.
4. **Live smoke (optional, needs `ASSEMBLYAI_API_KEY`):** a tiny Pipeline step transcribing a
   public audio URL; assert `result.run.steps[0].assets[0].metadata["text"]` non-empty,
   `word_timings` populated, and `result.manifest.verify()` is `True`. Consider adding it as
   `examples/transcribe.py`.

## Execution order

1. Create the package files under `libs/connectors/assemblyai/`.
2. Update `Makefile` / `libs/meta` / docs (`README.md`, `ARCHITECTURE.md`, `AGENTS.md`,
   `docs/reference/pricing-recipes.md`, `CHANGELOG`) in the same PR as the code (AGENTS.md invariant).
3. Run the verification gates above (`make test`, `make lint`, `make typecheck`).
4. Open the PR; move this plan to `docs/exec-plans/completed/` once merged.

> Working in a background session: implementation happens in a git worktree, then merges
> back to `main` with the worktree removed, so the end state matches an in-place edit.
