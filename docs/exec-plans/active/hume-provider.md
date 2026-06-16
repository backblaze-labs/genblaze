# Exec Plan: Add Hume AI (Octave TTS) Provider

**Status:** active
**Author:** kmahaney@backblaze.com
**Created:** 2026-06-15
**Scope:** New connector package `genblaze-hume` exposing `HumeTTSProvider` (audio / text-to-speech).

---

## 1. Summary

Add [Hume AI](https://www.hume.ai/) as a generative-media provider. The relevant
product for genblaze is **Octave TTS** — a synchronous text-to-speech API. This
maps to a single **audio** connector built on `SyncProvider`, modeled closely on
the existing `genblaze-lmnt` and `genblaze-elevenlabs` connectors.

EVI (Empathic Voice Interface, speech-to-speech) and Expression Measurement are
**out of scope** — neither fits the `Pipeline`/`Step`/manifest generation model.

### Hume API facts (verified against official docs, 2026-06-15)

| Aspect | Value |
|---|---|
| Base URL | `https://api.hume.ai`, endpoint `POST /v0/tts` |
| Auth header | `X-Hume-Api-Key` (SDK reads `HUME_API_KEY`) |
| Python SDK | `pip install hume`; sync client `from hume import HumeClient` (async `AsyncHumeClient` also exists) |
| Synthesize call | `client.tts.synthesize_json(...)` → non-streaming full result |
| Request shape | `utterances=[Utterance(text=, voice=, description=, speed=, trailing_silence=)]`, top-level `format`, `num_generations`, `version` ("1"/"2"), `temperature`, `split_utterances`, `instant_mode` |
| Voice | `VoiceRef` by id or name (provider `HUME_AI` / `CUSTOM_VOICE`); optional — omitting it generates a novel voice |
| Format | discriminated by `type`: `{"type":"mp3"}` / `{"type":"wav"}` / `{"type":"pcm"}` |
| Response | `generations[]` each with `audio` (**base64**), `duration` (s), `encoding.{format, sample_rate}` (default 48000 Hz), `file_size`, `generation_id`, `snippets[]` with optional word/phoneme `timestamps` |
| Models | No per-slug catalog. Octave model selected via `version` "1" or "2" (Octave 2 = preview, multi-language, requires a voice) |

**Sources:**
- [TTS overview](https://dev.hume.ai/docs/text-to-speech-tts/overview)
- [synthesize-json reference](https://dev.hume.ai/reference/text-to-speech-tts/synthesize-json)
- [Python quickstart](https://dev.hume.ai/docs/text-to-speech-tts/quickstart/python)
- [hume-python-sdk](https://github.com/HumeAI/hume-python-sdk)

### Key design consequences

- **Base class:** `SyncProvider` — Hume's `synthesize_json` is synchronous (use sync `HumeClient`, no `_run_async` needed). Implement `generate(step) → Step`.
- **Base64, not URLs:** like ElevenLabs/LMNT, decode `generation.audio` and write to `output_dir` (or a tempfile) and set a `file://` URL — guide §9.
- **No model catalog → `DiscoverySupport.NONE`:** Hume has no `/models` listing; only `version` 1/2. Family-matched `octave-*` slugs return `OK_PROVISIONAL`. Document the rationale in the class docstring (guide §11.2). This mirrors `genblaze-lmnt`.
- **`model` slug → `version`:** genblaze `step.model` carries the slug (e.g. `octave-2`); translate to the API's `version` field inside `generate()` (`octave-2` → `"2"`, `octave-1` → `"1"`).

---

## 2. Closest existing reference

`libs/connectors/lmnt/` — audio TTS, `SyncProvider`, `DiscoverySupport.NONE`,
`per_input_chars` pricing, base64/bytes → `output_dir`/tempfile → `file://`.
Borrow audio-metadata + optional word-timing handling from
`libs/connectors/elevenlabs/genblaze_elevenlabs/provider.py`.

---

## 3. Files to create

```
libs/connectors/hume/
├── genblaze_hume/
│   ├── __init__.py          # export HumeTTSProvider + __version__
│   ├── _version.py          # __version__ = "0.1.0"
│   ├── py.typed             # REQUIRED — typed marker shipped in wheel
│   ├── _errors.py           # map_hume_error(exc) -> ProviderErrorCode
│   └── provider.py          # HumeTTSProvider(SyncProvider)
├── tests/
│   ├── __init__.py
│   ├── test_hume_provider.py        # ProviderComplianceTests subclass + error mapping
│   └── test_catalog_decoupling.py   # mirror lmnt: no hardcoded pricing, family match
├── README.md
└── pyproject.toml
```

> Prefer running `/scaffold-provider hume audio sync` first to generate this
> skeleton from current conventions, then apply the Hume-specific wiring below.

---

## 4. Implementation steps

### 4.1 `pyproject.toml`
- `name = "genblaze-hume"`, `version = "0.1.0"`, `requires-python = ">=3.11"`, MIME/keyword classifiers matching `genblaze-elevenlabs`.
- Dependencies: `genblaze-core>=0.3.0,<0.4` (confirm current core minor against a sibling at edit time) and `hume>=0.x` (pin to the installed major).
- Entry point — **required for discovery**:
  ```toml
  [project.entry-points."genblaze.providers"]
  hume-tts = "genblaze_hume:HumeTTSProvider"
  ```
- `[tool.hatch.build.targets.wheel] packages = ["genblaze_hume"]`, `[tool.pytest.ini_options] testpaths = ["tests"]`.

### 4.2 `provider.py` — `HumeTTSProvider(SyncProvider)`
- `name = "hume-tts"`, `discovery_support = DiscoverySupport.NONE` (docstring explains: no upstream catalog endpoint; only Octave `version` 1/2).
- **Model family** (guide §11.1): single `ModelFamily(name="hume-octave", pattern=re.compile(r"^octave-"), spec_template=ModelSpec(model_id="*", modality=Modality.AUDIO), example_slugs=("octave-1","octave-2"))`. `spec_template.pricing` **must be `None`**. `create_registry()` returns `ModelRegistry(provider_families=(...,), fallback=ModelSpec(model_id="*", modality=Modality.AUDIO))`.
- `__init__(self, api_key=None, output_dir=None, *, models=None, retry_policy=None, probe_cache_ttl=None, probe_cache_max_entries=None)` → **always** `super().__init__(models=..., retry_policy=..., probe_cache_ttl=..., probe_cache_max_entries=...)`. Store `api_key`, `output_dir`, lazy `_client`.
- `_get_client()`: lazy import `from hume import HumeClient`; raise `ProviderError("hume package not installed. Run: pip install hume")` on `ImportError`; construct with `api_key` (falls back to `HUME_API_KEY` env).
- `get_capabilities()`: `supported_modalities=[Modality.AUDIO]`, `supported_inputs=["text"]`, `models=self._models.known()`, `output_formats=["audio/mpeg","audio/wav","audio/pcm"]`. `accepts_chain_input=False` (text-only).
- `normalize_params()` (idempotent, guarded with `if "x" in p and "native" not in p`):
  - `voice_id` → keep as `voice_id` internally; map to `VoiceRef`/utterance voice in `generate()`.
  - `output_format` → internal format key, mapped to Hume `{"type": mp3|wav|pcm}` in `generate()`.
  - Pass through `speed`, `temperature`, `trailing_silence`, `description`, `num_generations`.
  - No `duration` mapping (Hume has no output-duration control; do **not** invent one).
- `generate(step, config=None)`:
  1. `payload = self.prepare_payload(step)`.
  2. Resolve `version` from `step.model` (`octave-2`→`"2"`, else `"1"`).
  3. Build `Utterance(text=step.prompt, voice=<VoiceRef if voice_id>, description=payload.get("description"), speed=..., trailing_silence=...)`.
  4. Call `client.tts.synthesize_json(utterances=[...], format={"type": fmt}, num_generations=1, version=version, ...)`.
  5. Take `generations[0]`: `base64.b64decode(gen.audio)` → write bytes to `output_dir/{step.step_id}.{ext}` (mkdir parents) or `tempfile.mkstemp`; `file://` + `urllib.parse.quote(resolved_path)`.
  6. `validate_asset_url(file_url)` then build `Asset(url=file_url, media_type=<mime>)`; set `asset.size_bytes = len(bytes)`, `asset.duration = gen.duration`, `asset.metadata["audio_type"] = "speech"`.
  7. `asset.audio = AudioMetadata(channels=1, codec=<from format>, sample_rate=gen.encoding.sample_rate)`.
  8. **(Optional)** if `payload.get("with_timestamps")`: request `include_timestamp_types=["word"]` (Octave 2) and map `snippets[].timestamps` → `WordTiming` like ElevenLabs `_parse_*_alignment`. Mark as a follow-up if it complicates the first cut.
  9. `step.assets.append(asset)`; `self._apply_registry_pricing(step)`; `return step`.
  10. Error envelope: `except ProviderError: raise` / `except Exception as exc: raise ProviderError(f"Hume TTS failed: {exc}", error_code=map_hume_error(exc), retry_after=retry_after_from_response(exc)) from exc`.
- **`list_voices(self, *, model=None, language=None)`** (guide §10): Hume exposes a voice library + custom voices (`client.tts.voices.list(...)`). Return `Voice` objects, filtering by `language` (BCP-47 prefix). If wiring the live call is non-trivial, ship a small curated static catalog first (gmicloud/Riva pattern) and note the live-API upgrade as follow-up. Non-blocking for compliance.

### 4.3 `_errors.py` — `map_hume_error(exc) -> ProviderErrorCode`
Delegate to the shared `classify_api_error`, with Hume-specific checks first if the
SDK raises typed errors carrying `status_code` (e.g. `hume.core.api_error.ApiError`):
```python
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import classify_api_error

def map_hume_error(exc):
    status = getattr(exc, "status_code", None)
    if status == 429: return ProviderErrorCode.RATE_LIMIT
    if status in (401, 403): return ProviderErrorCode.AUTH_FAILURE
    if status in (400, 422): return ProviderErrorCode.INVALID_INPUT
    if status in (500, 502, 503): return ProviderErrorCode.SERVER_ERROR
    return classify_api_error(exc)
```

### 4.4 `__init__.py` / `_version.py` / `py.typed`
- `__init__.py`: `from genblaze_hume.provider import HumeTTSProvider`; re-export `__version__`; `__all__ = ["HumeTTSProvider"]`.
- `py.typed`: empty marker file (must be packaged).

### 4.5 Tests
- `test_hume_provider.py`: subclass `ProviderComplianceTests` (`make_provider()` returns `HumeTTSProvider(api_key="test")` with `_client` set to a fake returning a base64 generation). Covers the 15 compliance checks (lifecycle, asset URL validation, media types, audio metadata, `normalize_params` idempotency, cost soft-check). Add Hume-specific tests: `map_hume_error` status-code mapping, `model`→`version` translation, base64→file write + `file://` URL.
- `test_catalog_decoupling.py`: mirror lmnt — assert the registry ships **no** hardcoded pricing and that `octave-*` slugs match the family.

---

## 5. Integration / wiring (easy to miss → CI won't run otherwise)

- **`Makefile`** — add `libs/connectors/hume` to all three: `install`, `install-dev`, and `test` targets (guide §18 checklist).
- **`libs/meta/pyproject.toml`** — add umbrella extras: `hume = ["genblaze-hume>=0.3.0,<0.4"]`, and include it in the `audio` and `all` bundles.
- **`docs/reference/pricing-recipes.md`** — add a `## Hume` section. Octave TTS bills per character → `per_input_chars(rate, per=1000)`. Pull the **exact** USD rate and tier breakdown from Hume's pricing page at implementation time (do not guess); include the upstream pricing URL.

---

## 6. Documentation (same-PR requirement — AGENTS.md)

- **`README.md`**:
  - Install section: add `pip install genblaze-hume` line.
  - Providers matrix: add a **Hume** row (Audio = Octave TTS; Video/Image/Chat = —). Respect the "Update when adding a provider" comment above the table.
  - "Configure API keys" table: add `Hume | HUME_API_KEY | platform.hume.ai`.
- **`ARCHITECTURE.md`**: add `genblaze-hume` to the provider-adapters list, the External Services list (Hume API — Octave TTS), and any modality notes.
- **`AGENTS.md`**: bump package/provider counts ("14 packages"/11 providers → 15 packages/12 providers) and add `genblaze-hume` to the connector enumeration.
- **`libs/connectors/hume/README.md`**: usage snippet (env var, `HumeTTSProvider(output_dir=...)`, `Pipeline(...).step(..., model="octave-2", modality=Modality.AUDIO)`).
- **No spec/schema change:** no new manifest or event fields are introduced, so `libs/spec` and `make ts-types` are **not** touched. Note this explicitly in the PR so reviewers don't expect regenerated TS types.

---

## 7. Invariants to respect (AGENTS.md + guide)

- Provider implements the required lifecycle — `SyncProvider.generate()` (no `submit/poll/fetch_output` since it's synchronous).
- `super().__init__(...)` always called (sets up retry/preflight/registry/poll cache).
- `validate_asset_url()` on the output `file://` URL; **no API tokens** stored in `step.provider_payload`.
- `normalize_params` idempotent.
- `spec_template.pricing is None`; pricing user-registered only.
- Pydantic v2 only; Python 3.11+; UUID ids (handled by core).
- Canonical-hash determinism untouched (no model/serialization changes).
- `make test`, `make lint`, `make typecheck` all green from repo root before PR.

---

## 8. Verification gate

```bash
pip install -e "libs/connectors/hume[dev]"   # dev install
/test-package hume                             # fast single-package run
make lint && make typecheck
make test                                      # full-suite gate before PR
```

Manual smoke (optional, needs `HUME_API_KEY`): one-step `Pipeline` with
`model="octave-2"`, `modality=Modality.AUDIO`, assert asset written, manifest
verifies.

---

## 9. Open questions / decisions deferred to implementation

1. **Voice catalog** — live `client.tts.voices.list()` vs. curated static catalog for the first cut. Default: static catalog now, live upgrade as follow-up.
2. **Word/phoneme timestamps** — include in v1 or defer? Default: defer (Octave-2-only, adds response-shape branching).
3. **Exact pricing rate** — confirm from Hume's current pricing page before writing the recipe; leave `cost_usd=None` until registered (SDK ships no prices).
4. **`hume` SDK version pin** — confirm the installed major and whether the sync `HumeClient` (not just `AsyncHumeClient`) is exported in that version.
