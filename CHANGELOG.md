# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### genblaze-core

- **Fixed** concurrent `stream()`/`astream()` calls on the same `Pipeline` or
  `AgentLoop` instance no longer cross-deliver events (#79, #84). The active
  emitter was a single mutable instance attribute, so a second concurrent
  call's install silently clobbered the first's, mixing one stream's events
  into the other's queue. Both classes now install the emitter on an
  `EmitterSlot` (`contextvars.ContextVar`-backed), which is isolated per
  thread/task with no additional locking required.
- **Fixed** `stream()`/`astream()` no longer leak an abandoned worker's
  remaining events onto an undrained queue after an early break (#74). The
  worker used to keep running in the background for the rest of the
  pipeline's duration, enqueuing every subsequent event with nobody to drain
  it. The emitter now closes as soon as the early break is detected, so
  further `put()` calls become no-ops instead of buffering unboundedly.
- **Fixed** `stream()`/`astream()` no longer emit a successful
  `pipeline.completed` terminal event for a run that aborts before reaching
  normal finalization — e.g. `pipeline_timeout` expiring before any step
  starts (#85). `all([])` treated an empty completed-steps list as
  "succeeded"; aborted runs are now always finalized as `FAILED` and emit
  `pipeline.failed`, with the abort's exception message attached.
- **Fixed** concurrent `arun(fail_fast=True)` cancellation now preserves the
  original `step_id` for cancelled and exception-raising steps, so a
  `step.failed` event correlates with its own earlier `step.started` instead
  of minting a brand-new id (#86).
- **Fixed** `step_cache_key` no longer sorts `step.inputs` before hashing (#71).
  Providers that consume inputs positionally (multi-image edit/compose,
  multimodal chat) produce different output when input order changes, but the
  sorted key made a reordered request incorrectly reuse an earlier run's
  cached asset. The key now preserves input order, matching the
  order-preserving manifest canonical hash. Existing cache entries for
  multi-input steps whose inputs weren't already in sorted order miss once
  and recompute (one-time repopulation, no data loss).
- **Fixed** Windows `file://` URL handling across all call sites (#132).
  On Windows, `urlparse("file:///C:/...").path` returns `/C:/...`, and
  `Path("/C:/...").resolve()` produces a drive-relative path that always
  fails the `is_relative_to(temp_root)` allowlist check. All affected sites
  now use `urllib.request.url2pathname`, which strips the leading `/` before
  a drive letter so `Path.resolve()` gets a properly anchored path.
  On Unix `url2pathname` is an alias for `unquote` — no behaviour change.
  Fixed in: `storage/transfer.py` (`ObjectStorageSink`), `providers/_ffmpeg_utils.py`
  (compositor/transform), `providers/base.py` (`validate_chain_input_url`,
  which also replaces `startswith("/")` with cross-platform `Path.is_absolute()`).
- **Fixed** 0.3.4 → 0.3.5: `MockProvider`, `MockVideoProvider`, and
  `MockAudioProvider` no longer require `pytest` at import. They moved to a new
  pytest-free `genblaze_core.mocks` module (still re-exported from
  `genblaze_core.testing` for backward compatibility), so
  `from genblaze_core import MockVideoProvider` works in a runtime-only install.
- **Fixed** `Pipeline.step(..., params={...})` no longer nests the dict under a
  literal `"params"` key in `Step.params` (#133). The catch-all kwargs collector
  was itself named `params`, so a caller passing `params={"image": ..., "length":
  ...}` (the natural spelling, mirroring the `Step.params` field) got
  `step.params == {"params": {...}}` instead of the flattened dict, with no error
  or warning. `.step()` now accepts an explicit `params={}` dict alongside
  top-level kwargs; both populate `Step.params`, and a top-level kwarg wins on
  key collision.
- **Fixed** `Step(...)` now rejects unrecognized constructor kwargs
  (`model_config = ConfigDict(extra="forbid")`) instead of silently discarding
  them — the same class of bug via direct model construction rather than
  `Pipeline.step()`. Provider-specific keys belong in `params={...}` (#133).
- **Fixed** `examples/quickstart_local.py` printed `Verified: False` on a
  clean `pip install genblaze-core` (#125). The 0.3.4 hardening of
  `Manifest.verify()` (requires every output asset to declare a `sha256`)
  wasn't reflected in the example's synthetic output asset. The example now
  passes a `sha256` for its placeholder demo bytes, matching what a real
  provider/`ObjectStorageSink` would populate.
- **Fixed** `Pipeline.ingest` sorted assets by the random `asset_id` before
  hashing, so identical fresh asset batches (new `Asset()` instances with new
  random ids, same content) could produce different `canonical_hash` values
  even though `asset_id` is excluded from the hash payload (#76). Ingest now
  sorts the finished steps by `asset_provenance_key` — the same content
  fields that feed the hash (`sha256`, `media_type`, `size_bytes`,
  dimensions, ...) — *after* `sink.put_asset` has populated each asset's
  hash, since sorting beforehand ties on the shared placeholder content
  every fresh batch starts with and falls back to caller input order. The
  determinism contract now holds for fresh asset batches and sink-populated
  hashes, not just permuted reuses of already-hashed objects.

### genblaze-openai

- **Fixed** same Windows `file://` drive-letter bug in `dalle.py`
  (`_resolve_local_file` — DALL-E image-edit local inputs) (#132).
- **Fixed** `SoraProvider` image-to-video chaining (#126). `submit()` forwarded
  the routed image slot verbatim as `image=`, but `Videos.create()` has no such
  kwarg — the openai SDK 2.x start frame goes in `input_reference`, which must
  be an uploaded file, not a URL string. Chain inputs also arrive as local
  `file://` temp paths (sink upload happens later), so the URL was unusable as-is
  even with the right parameter name. `submit()` now materializes the routed
  image (local `file://` resolve, or SSRF-pinned `https://` download — reusing
  `DalleProvider`'s existing file-input helpers) into an open file handle before
  upload. Also stringifies `seconds` (`4`/`8`/`12`), which the SDK types as
  `Literal["4", "8", "12"]`, not `int`.
- **Fixed** `SoraProvider.fetch_output()` download against openai SDK 2.x (#127).
  It called `videos.content()`, which the SDK renamed to `download_content()`;
  every completed generation failed at the download step with `'Videos' object
  has no attribute 'content'` — after the generation cost was already incurred.
  The returned binary response still supports `write_to_file()`, so the method
  rename is the only change needed.

### genblaze-google

- **Fixed** `VeoProvider` broken in Vertex AI auth mode (`project`/`location`) (#136).
  `poll()`/`fetch_output()` passed the bare operation-name string returned by
  `submit()` straight to `client.operations.get()`, which reads `.name` off its
  argument and raised `AttributeError: 'str' object has no attribute 'name'` on
  every poll — now wrapped in `types.GenerateVideosOperation` first. `fetch_output()`
  also called `client.files.download()` unconditionally, which raises `ValueError`
  on Vertex (the Files API is Gemini-Developer-API-only); Vertex returns video
  bytes inline instead, so `fetch_output()` now saves those bytes to a local file
  (new `output_dir` constructor param, indexed per-video for `number_of_videos > 1`)
  and exposes a `file://` asset, matching the existing `ImagenProvider`/
  `DecartVideoProvider` convention. The Gemini Developer API path is unchanged.

### genblaze-replicate

- **Fixed** `ReplicateProvider.submit()` 404s for community models (#109).
  `predictions.create(model=<slug>)` only works for Replicate's *official*
  models — community slugs (e.g. `sczhou/codeformer`, `tencentarc/gfpgan`)
  404 on that path. `submit()` now picks the endpoint per model: community
  models resolve to a published version hash and run via
  `predictions.create(version=<hash>)`, while official/versionless models
  (e.g. `black-forest-labs/flux-schnell`) keep running via the `model=`
  path. Resolution accepts an inline `owner/name:hash` pin, otherwise reads
  `client.models.get(slug).latest_version` and caches the result per-slug —
  hash or "official, no version" — seeded from `validate_model()`'s existing
  probe, so a normal `Pipeline.run()` costs no extra round-trip.

### genblaze-cli

- **Fixed** 0.3.2 → 0.3.3: `extract` now supports the `-o/--output` option
  to write the manifest JSON to a file, matching the documented usage.
- **Changed** `--version` now reports `genblaze-cli` rather than `genblaze`,
  so the CLI's version is no longer mistaken for the umbrella package version.

### genblaze (umbrella)

- **Changed** 0.4.1 → 0.4.2: raises its `genblaze-core` floor to 0.3.5
  so `pip install genblaze` resolves the mock-import fix above.

### Security

- `genblaze-core`: `PromptTemplate` now rejects attribute and item traversal
  such as `{user.api_key}` and `{settings[voice]}`. Pass explicit top-level
  values instead, for example `{api_key}` or `{voice}`. Top-level format specs
  and conversions such as `{price:.2f}` and `{name!r}` remain supported (#88).

### Changed

- `genblaze-gmicloud`: video `duration` now requires whole-second integer
  values from 1 to 60 seconds; fractional, zero/negative, and oversized
  inputs fail invalid-input validation instead of being silently truncated
  or forwarded (#90).

## [0.4.0] - 2026-06-25

Security hardening (SSRF, URL-only asset verification), two new providers
(Hume Octave TTS, AssemblyAI STT), and a batch of connector patch-republishes
to update the `genblaze-core>=0.3.4` floor.

### Released package versions

- `genblaze` (umbrella) 0.4.0 → **0.4.1** — patch republish to refresh extras
  and connector floors. PyPI cannot overwrite an existing version; all changed
  pins ship only when a new wheel is published.
- `genblaze-core` 0.3.2 → **0.3.4** (URL-only asset verification hardening,
  SSRF DNS pinning + redirect guard, fan-in failure propagation, sink lifecycle
  fix, async preflight offload, error sanitiser, tenant-scoped step cache)
- `genblaze-s3` 0.3.2 → **0.3.4** (bumps `genblaze-core` floor to 0.3.4;
  region-probe close on `S3StorageBackend`, backend `close()` shuts boto3
  client to release thread pool connections on `run()` teardown)
- `genblaze-cli` 0.3.0 → **0.3.2** (bumps `genblaze-core` floor to 0.3.4;
  exposes `verify_hash()` and output-asset sha256 diagnostics)
- `genblaze-replicate` 0.3.0 → **0.3.2** (bumps `genblaze-core` floor to 0.3.4)
- `genblaze-gmicloud` 0.3.1 → **0.3.2** (bumps `genblaze-core` floor to 0.3.4)
- `genblaze-hume` — **new at 0.3.1** (Hume Octave TTS provider)
- `genblaze-assemblyai` — **new at 0.3.0** (AssemblyAI speech-to-text provider)
- `genblaze-openai` 0.3.0 → **0.3.1** (DALL-E URL outputs materialised locally;
  pinned-DNS download; bumps `genblaze-core` floor to 0.3.4)
- `genblaze-google` 0.3.0 → **0.3.1** (bumps `genblaze-core` floor to 0.3.4)
- `genblaze-decart` 0.3.0 → **0.3.1** (bumps `genblaze-core` floor to 0.3.4)
- `genblaze-elevenlabs` 0.3.0 → **0.3.1** (bumps `genblaze-core` floor to 0.3.4)
- `genblaze-langsmith` 0.3.0 → **0.3.1** (bumps `genblaze-core` floor to 0.3.4)
- `genblaze-lmnt` 0.3.0 → **0.3.1** (bumps `genblaze-core` floor to 0.3.4)
- `genblaze-luma` 0.3.0 → **0.3.1** (bumps `genblaze-core` floor to 0.3.4)
- `genblaze-nvidia` 0.3.0 → **0.3.1** (bumps `genblaze-core` floor to 0.3.4)
- `genblaze-runway` 0.3.0 → **0.3.1** (bumps `genblaze-core` floor to 0.3.4)
- `genblaze-stability-audio` 0.3.0 → **0.3.1** (bumps `genblaze-core` floor to 0.3.4)

### Security

- `genblaze-core` 0.3.2 → 0.3.4: `Manifest.verify()` now rejects output
  assets that lack `sha256` for every supported schema version, preventing a
  schema downgrade from bypassing declared output sha256 coverage. This is an
  intentional security exception to the normal patch-release compatibility
  policy; use `verify_hash()` for legacy hash-only checks against historical
  URL-only media (#77).
- `genblaze-core`: `Asset.sha256` values remain loadable even when malformed,
  so historical and cross-producer manifests can still be inspected with
  `verify=False` or `allow_unverified_assets=True`. `Manifest.verify()` and
  `genblaze verify` treat missing, uppercase, or otherwise malformed `sha256`
  as unverified output coverage. They do not fetch remote asset URLs; consumers
  must independently hash fetched bytes before trusting those bytes (#77).
- `genblaze-core`: schema 1.6 URL-only hash markers are Python read-supported.
  The canonical marker URL strips known credential, expiry, and response
  override query parameters for AWS SigV4/SigV2, GCS, B2, CloudFront, Azure SAS,
  and GCS V2 signed URLs (including bare `authorization` tokens) while retaining
  resource-identifying query parameters. Default manifest emission, storage
  writes, media embedding, and the published JSON Schema/TypeScript spec stay on
  schema 1.5 for an expand-contract rollout (#77).
- `genblaze-openai`: `dall-e-2` / `dall-e-3` URL responses are now downloaded
  immediately to a local `file://` asset with a populated `sha256` and
  `size_bytes`, instead of being stored as the raw (credential-bearing,
  ~1-hour) Azure SAS URL. The signed URL is used only for that fetch and never
  reaches the manifest, step cache, or any sink. This fixes the `.run(sink=...)`
  transfer path (which previously received a credential-stripped, unfetchable
  URL) and lets DALL-E outputs verify without a storage sink (#77).
- `genblaze-cli` 0.3.0 → 0.3.2: raises its `genblaze-core` floor to the first
  core version that exposes `verify_hash()` and output-asset sha256 diagnostics
  (#77).
- `genblaze` umbrella package: raises its `genblaze-core` floor to 0.3.4 so
  umbrella installs receive the verification hardening (#77).
- Provider and storage connector packages now require `genblaze-core>=0.3.4,<0.4`
  and carry patch version bumps so adapter-only installs receive republished
  wheels with the URL-only asset verification fix (#77).
- `ObjectStorageSink.read_manifest_for_asset()` now requires `tenant_id`, stores
  tenant-scoped asset index entries, validates `asset_id` as a UUID, falls back
  to legacy flat index entries during migration, rejects manifest pointer
  substitution unless the recovered manifest references the requested asset,
  and applies the same staged verification behavior as `read_manifest()` (#77).
- `ObjectStorageSink.write_run()` now fails the write when an asset transfer
  fails, instead of uploading a success-path manifest that later fails strict
  verification. Successful transfers from a partial failure are reused on
  retry and the in-memory manifest hash is recomputed before raising (#77).

### Changed

- `ObjectStorageSink.read_manifest(verify=True)` verifies canonical hashes by
  default and logs output assets whose `sha256` is missing or malformed.
  Hard-failing those unverified outputs is staged behind
  `strict_manifest_reads=True` or `GENBLAZE_STRICT_MANIFEST_READS=true` so
  operators can backfill historical URL-only manifests before enabling strict
  read failures on hot paths (#77).
- `genblaze-core` `[audio]` extra: mutagen capped at `<1.49`. Mutagen 1.48
  changed m4a timescale handling in a way that broke the AAC handler test
  fixture; the cap prevents a future mutagen release from reintroducing
  the same break before it can be vetted (#108).

### Fixed

- `tools/check_pin_parity.py`: extend the pre-publish drift guard to
  compare `[project.optional-dependencies]` as well as base
  `[project.dependencies]`. Previously the gate reported
  `0 drift` for the `genblaze` umbrella even when connector pins in
  the `all`, `video`, `image`, or `audio` extras diverged from the
  published wheel, because `Requires-Dist` extras entries were
  silently filtered out. The gate now groups PyPI extras by name and
  diffs each extra independently, naming the extra in the drift
  report (e.g. `[all]`). Adds `tools/tests/test_check_pin_parity.py`
  and wires `pytest tools/tests/` into `make test`. Updates
  RELEASING.md to accurately describe the gate's scope (#23).

### Added

- `genblaze-hume`: new provider adapter for Hume AI **Octave TTS** (audio /
  text-to-speech). Synchronous `SyncProvider` that decodes the API's base64
  audio to a local `file://` asset; `step.model` (`octave-1` / `octave-2`)
  maps to the Octave `version` field. Ships a pattern-keyed `octave-*` model
  family with a permissive fallback, `DiscoverySupport.NONE`, and no hardcoded
  pricing (register per-character rates via the recipe in
  `docs/reference/pricing-recipes.md`). Available as `pip install genblaze-hume`
  or the `genblaze[hume]` / `genblaze[audio]` extras.
- `genblaze-assemblyai`: new provider adapter for **AssemblyAI** speech-to-text
  / transcription — the first connector that *consumes* audio and *produces*
  text. Async `BaseProvider` (submit / poll / fetch_output) that resolves an
  audio URL (`step.inputs[0]` → `params["audio_url"]` → `prompt`, SSRF-validated
  via `validate_chain_input_url`) and emits a hash-verified **TEXT asset**
  (`text:{sha256}`, `media_type="text/plain"`, transcript in `metadata["text"]`)
  with word-level timings on `AudioMetadata.word_timings` (converted ms → s).
  `step.model` is sent on the SDK's plural `speech_models` field (the live API
  has deprecated the singular `speech_model` field and the legacy best/nano
  aliases). Ships a pattern-keyed `assemblyai-speech` family (`universal-3-pro`
  / `universal-2`) with a permissive TEXT fallback,
  `DiscoverySupport.NONE`, and no hardcoded pricing (register
  per-minute-of-input-audio rates via the recipe in
  `docs/reference/pricing-recipes.md`). Available as
  `pip install genblaze-assemblyai` or the `genblaze[assemblyai]` extra.

### Security

- `genblaze-core`: DNS pinning closes the rebinding / TOCTOU window on all
  outbound HTTP paths. `resolve_ssrf` resolves the hostname once, validates
  every returned IP, and returns the pinned address. Callers connect to that
  IP directly — the HTTP client never performs a second independent resolution.
  TLS SNI and cert verification continue to use the original hostname. Affects
  `storage/transfer.py` (urllib3 `HTTPSConnectionPool` per hop), and
  `webhooks/notifier.py` and `genblaze_openai/dalle.py` (direct
  `http.client.HTTPSConnection` with pre-connected pinned socket) (#9).
- `genblaze-core`: SSRF guard now validates every HTTP redirect hop in the
  asset transfer path (`storage/transfer.py`). Previously, `check_ssrf` ran
  only on the initial URL; a CDN redirect to a private/loopback/IMDS address
  bypassed the guard entirely. Redirects are now followed manually with a
  bounded loop (max 5 hops); each `Location` is re-resolved and re-pinned
  before following, and downgrade to non-HTTPS is rejected (#9).
- `genblaze-core`: Webhook delivery (`webhooks/notifier.py`) switched from
  `urllib.request` to `http.client.HTTPSConnection` directly. `http.client`
  has no redirect handler, so a 3xx response is treated as a delivery failure
  rather than following the `Location` header, preventing a server-side
  redirect to an internal host from bypassing the SSRF guard (#9).
- `genblaze-openai`: `_download_https_to_temp` now uses `http.client`
  directly with a pinned DNS connection, closing both the redirect-bypass and
  DNS rebinding vectors for edit-input downloads (#9).
- `genblaze-core`: IPv4-mapped IPv6 addresses (`::ffff:169.254.x.x`,
  `::ffff:10.x.x.x`, etc.) are now normalized to their IPv4 form before the
  SSRF blocklist check. Previously, a DNS response returning an IPv4-mapped
  address bypassed all IPv4 `BLOCKED_NETWORKS` entries (#9).
- `genblaze-core`: HTTP `Location` headers in redirect chains are now resolved
  with `urljoin` before re-validation, so RFC-legal relative redirects work
  and relative redirects to private targets are still rejected (#9).
- **Egress proxy note:** DNS pinning requires a direct TCP connection to the
  validated IP; `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` env vars are ignored
  by design on all pinned outbound paths (transfer, webhook, dalle). Deployments
  that require an egress proxy should allowlist the target hosts at the proxy
  instead of relying on env-var forwarding.

### Fixed

- `genblaze-core`: `Pipeline.arun()` no longer blocks the event loop during
  model preflight. The network-bound phase (`_validate_models`, which uses a
  `ThreadPoolExecutor` for provider discovery fetches) is now offloaded via
  `asyncio.to_thread`, allowing concurrent coroutines to keep running during
  the preflight window. Cheap capability checks (modality, chain-input) still
  run synchronously. `Pipeline.run()` behavior is unchanged (#56).
- `genblaze-core`: `Pipeline.run()`/`arun()` now release a sink's run-scoped
  resources in their `finally` block (and on early preflight/validation
  failure), shutting down the `ObjectStorageSink` eager-upload
  `ThreadPoolExecutor` and releasing the backend connection pool —
  `S3StorageBackend.close()` now closes the boto3 client. Previously non-daemon
  worker threads stayed alive after `run()` returned and in-flight eager-upload
  futures leaked on error paths that bypassed `write_run`. Sinks declare
  lifecycle ownership via `BaseSink._close_with_run` (default `True`):
  run-scoped sinks (`ObjectStorageSink`) are closed by the pipeline and are
  single-use, while fire-and-forget `WebhookSink` opts out (`False`) so it is
  never closed — keeping webhook delivery non-blocking and the sink reusable
  across runs. `batch_run()`/`abatch_run()` close their shared sink once after
  the whole batch rather than after the first item. `BaseSink` gains
  `__enter__`/`__exit__` for callers that manage the lifecycle outside a
  `run()`. **Behavior change:** a run-scoped sink passed to `run()`/`arun()` is
  closed afterward — construct a fresh one per run rather than reusing it (#57).
- **`genblaze-openai`**, **`genblaze-google`**: remove hardcoded USD per-token
  rate tables (`_RATES`) from the standalone `chat()` helpers and return
  `cost_usd=None` unconditionally, consistent with the 0.3.0 contract that
  connector modules ship zero static rate tables (Pipeline-Step providers
  register rates via `PricingContext`/`ModelSpec`). The standalone `chat()`
  helpers have no model registry, so callers that relied on
  `ChatResponse.cost_usd` being non-`None` for known models must now compute
  cost from `tokens_in`/`tokens_out` with their own rates (see
  `docs/reference/pricing-recipes.md`). Adds
  `test_pricing_phaseout.py` to `genblaze-core` — a lint-style CI guard that
  fails if any future connector reintroduces a `_RATES`/`_PRICING` constant (#13).
- `genblaze-core`: post-submit step-level retries now resume the existing
  upstream prediction instead of submitting a new one, including transient
  checkpoint failures after `submit()` returns by replaying idempotent
  `on_submit(step_id, prediction_id)` callbacks before retry resume (#70).
- `genblaze-core`: fan-in consumers using `input_from` now fail before provider
  invocation when a referenced producer step failed, produced no assets, or
  points at an out-of-range prior step, preventing a downstream step from 
  reporting success with empty declared inputs (#69). Affected pipelines that
  previously appeared green can now report `FAILED`/`INVALID_INPUT`, which may
  increase status-based alerts and change manifest hashes once during rollout.
  Route or re-baseline those alerts using
  `metadata.failure_reason="input_resolution"`; these pre-failed steps also
  carry `metadata.provider_invoked=false` for telemetry filtering.
- `genblaze-core`: failed `Step.error` values now use the shared sanitizer for
  OpenAI/Anthropic/Google/Replicate keys, AWS access key IDs and secret access
  keys, Backblaze B2 application keys, JWTs, bearer/token headers, API-key
  assignments, and basic-auth URL credentials, then truncate to 500 characters
  before manifest, log, or stream-event emission. This redaction hardening ships
  with #69 because fan-in pre-fail telemetry can otherwise re-surface untrusted
  upstream provider errors on dependent steps.
- `genblaze-core`: `StepCache` now partitions the step cache key by `tenant_id`
  when a tenant is set (via `Pipeline(tenant_id=...)`, or passed to
  `StepCache.get`/`put`), so a cache shared across tenants no longer serves one
  tenant's cached output asset to another for an otherwise-identical step.
  Single-tenant keys are unchanged. A `tenant_id` in `RunnableConfig` is now
  rejected at runtime (it was never honored), so a dynamic caller passing it via
  `config()` / `invoke(config=...)` fails loudly instead of silently bypassing
  cache isolation (#68).
- **`genblaze-replicate`** (0.3.0 → **0.3.1**): declare `httpx>=0.24` as a
  direct dependency. `ReplicateProvider._get_client()` imports `httpx`
  directly to build the client timeout, but the package declared only
  `replicate`, which pins `httpx>=0.21` — below the `0.22` floor where the
  `httpx.Timeout(connect=...)` kwarg landed. Pin aligned with the
  `httpx>=0.24` already used by the nvidia, gmicloud, and stability-audio
  connectors. Version bumped so a corrected wheel can publish past the
  `skip-existing` pin-parity gate (#37).
- **`genblaze-core`** (0.3.2 → **0.3.3**): declare `urllib3>=1.26,<3` as a
  direct dependency. `storage/transfer.py` imports `urllib3` unguarded at module load
  (the shared `PoolManager` behind `AssetTransfer`) and `storage/__init__`
  imports it eagerly, so `import genblaze_core.storage` hard-required urllib3 on
  a clean install while core declared only `pydantic` + `pillow`. It previously
  arrived only transitively via a connector's boto3 stack — the same
  clean-install crash class as the replicate/httpx fix above.
- **`genblaze-s3`** (0.3.2 → **0.3.3**): declare `botocore>=1.31` (imported
  directly in `backend.py`) and `aiobotocore>=2.7` in the `async` extra
  (imported directly in `async_backend.py`). Both always shipped transitively
  via boto3/aioboto3, which pin their exact versions — so these declarations add
  honesty for the dependency gate without changing what resolves.

### Added

- `genblaze-core`: `http` (`httpx`), `otel` (`opentelemetry-api`), and
  `testing` (`pytest`) extras. These advertise previously-undeclared
  guarded/soft imports. In particular `genblaze_core.testing` (the public
  `MockProvider` / `ProviderComplianceTests` harness) imports `pytest` at module
  load and ships in the wheel, so it now installs via
  `pip install "genblaze-core[testing]"`.

### Changed

- **Repo tooling**: new `make deptry` dependency-hygiene gate
  (backed by per-package `[tool.deptry]` config) fails on undeclared imports
  (DEP001), shipped imports of dev-only deps (DEP004), and misclassified
  transitive deps — the clean-install crash class above. Wired into `make lint`,
  `make pre-release`, and a new `deptry` CI job that also runs `pip check`
  against the editable workspace. `libs/meta` is excluded (umbrella metapackage:
  its deps are install-time bundles, not imports).

## [0.3.3] - 2026-05-26

Release-pipeline hardening + refresh of two PyPI wheels with stale
`genblaze-core` pins. Closes the class of drift bug where
`skip-existing` silently no-ops a package whose source pin was widened
but whose `version` field was never bumped. This trap shipped twice
before: `genblaze-s3` in the 0.3.0 wave (fixed in 0.3.1), and now
`genblaze-langsmith` + `genblaze-cli` discovered during 0.3.2
post-publish verification.

### Released package versions

- `genblaze-langsmith` 0.2.1 → **0.3.0** — patch republish to refresh
  the PyPI wheel. The 0.2.1 wheel pinned `genblaze-core<0.3`, so
  `pip install "genblaze[all]"` could not resolve any release in the
  0.3.x line. Source had the corrected `<0.4` pin since the 0.3.0
  prep window but `skip-existing` skipped publish on every wave.
- `genblaze-cli` 0.2.0 → **0.3.0** — patch republish for the same
  reason. The 0.2.0 wheel on PyPI pinned `genblaze-core<0.3`; source
  had `<0.4`. Surfaced by the new pin-parity gate, not by any
  install-resolution failure (cli is not in `genblaze[all]`).
- All other packages — unchanged.

### Changed

- **Release pipeline**: new pre-publish parity gate
  (`make pypi-pin-parity`, backed by `tools/check_pin_parity.py`)
  fails the release if any package's `[project.dependencies]` diverges
  from the same-version wheel already on PyPI. Bundled into
  `make pre-release` and added as a `pin-parity` job in
  `.github/workflows/release.yml` that gates every publish job. The
  trap class is now closed — a future wave cannot ship a divergent
  wheel without an explicit version bump.
- **Release pipeline**: `install-verify` now resolves
  `genblaze[all]==<version>` instead of bare `genblaze==<version>`.
  The workflow's own post-publish gate now exercises every connector's
  pin against public PyPI, matching what `make post-release` does from
  a maintainer's laptop and what real users see on `pip install
  "genblaze[all]"`.

## [0.3.2] - 2026-05-26

Storage ergonomics & GMI catalog hygiene. Closes the 2026-05-23 user feedback
batch (7 items). Additive-only — no existing import paths, kwargs, or
behaviors break. See [MIGRATING-0.3.2.md](MIGRATING-0.3.2.md) for the
migration guide.

### Released package versions

- `genblaze-core` 0.3.0 → **0.3.2** (canonical_slug field on `ModelFamily`;
  `URLPolicy` / `URLPolicyError` relocated from `genblaze_s3.url_policy`;
  `ObjectStorageSink(asset_url_policy=...)` kwarg)
- `genblaze-s3` 0.3.1 → **0.3.2** (`presigned_get_url` / `presigned_put_url`
  companions; `for_backblaze()` 403-region probe)
- `genblaze-gmicloud` 0.3.0 → **0.3.1** (canonical_slug applied to audio /
  Veo / new Kling V2.1 families per GMI's 2026 catalog)
- All other packages — unchanged.

### Added

- `genblaze-core`: `ObjectStorageSink(asset_url_policy=URLPolicy.AUTO | PUBLIC | PRESIGNED)`
  kwarg selects what flavor of URL gets written into `asset.url` on
  transfer. Default `AUTO` preserves today's durable-URL behavior; `PUBLIC`
  enforces that `backend.public_url_base` is configured (raises
  `URLPolicyError` at construction if not); `PRESIGNED` is **rejected
  at sink construction** — manifests must not carry SigV4 URLs (they
  decay before the manifest does, breaking provenance). For read-time
  presigned URLs use `backend.presigned_get_url(key)` directly.
  `AUTO` + missing `public_url_base` emits a one-time WARN per process
  per `(bucket, policy)` tuple pointing the caller at `public_url_base`
  or `presigned_get_url`. Backend-agnostic: backends that don't expose
  `public_url_base` skip the WARN cleanly. Addresses the 2026-05-23
  feedback batch item 5.

- `genblaze-s3`: `S3StorageBackend.presigned_get_url(key, ...)` and
  `presigned_put_url(key, ...)` — raw-`str` companions to `presigned_get` /
  `presigned_put`. Use at the boundary where the URL leaves the process
  (HTTP client call, API response). Equivalent to
  `backend.presigned_get(key).url` but discoverable from autocomplete
  alongside the wrapped form. Addresses the 2026-05-23 feedback batch
  item 1 — naïve `requests.get(backend.presigned_get("k"))` previously
  hit the redacted `__str__` and 403'd on a SigV4 mismatch. The wrapped
  `presigned_get` / `presigned_put` remain the safe default; the `_url`
  companions are the explicit raw-string escape hatch.

### Changed

- `genblaze-core`: new `ModelFamily.canonical_slug: Callable[[str], str] | None`
  field. When set, ``family.resolve(input)`` substitutes
  ``canonical_slug(input)`` into the spec's ``model_id`` — the wire form
  the upstream HTTP client receives. ``ModelRegistry.validate()``
  normalizes via the same callable before the discovery-cache check, so
  validation and submit agree on slug identity. ``ModelRegistry.known()``
  returns canonical forms for family-matched ``example_slugs``. When the
  rewrite actually changes the input, a one-time INFO per
  ``(family, input)`` fires so callers know to migrate their call sites
  (instance-level dedup, fork-safe via ``fork()``).

- `genblaze-gmicloud` 0.3.0 → **0.3.1**: applied the new `canonical_slug`
  mechanism to bridge GMICloud's per-slug casing.
  - **Audio families** (TTS / Voice-Clone / Music) — patterns now
    case-insensitive; `canonical_slug=str.lower` rewrites to GMI's
    published lowercase wire form (per the 2026-03-10 "Most Popular
    AI Models" blog: `elevenlabs-tts-v3`, `minimax-tts-speech-2.6-turbo`,
    `inworld-tts-1.5-mini`, `minimax-music-2.5`,
    `minimax-audio-voice-clone-speech-2.6-hd`). Pre-0.3.1 PascalCase
    callers continue to work; their input gets rewritten on the wire
    with a one-time INFO nudge.
  - **Veo family** — pattern made case-insensitive; `canonical_slug`
    rewrites `veo3` → `Veo3` (PascalCase per every GMI blog from
    2025-12-08 onward).
  - **New Kling V2.1 family** — covers `Kling-Text2Video-V2.1-Master` and
    `Kling-Image2Video-V2.1-Master` (PascalCase wire form per the
    2026-04-14 "Real-Time Video Generation Platforms" blog at $0.28/req).
    Accepts pre-0.3.1 lowercase callers via `canonical_slug`. Previously
    these slugs fell through to the permissive fallback and lost their
    family-specific param surface.
  - Cleaned up `_UNSTABLE_SLUGS` — pre-0.3.1 it carried lowercase
    variants of slugs whose canonical wire form is PascalCase
    (`kling-text2video-v2.1-master`, `minimax-hailuo-2.3-fast`). With
    the canonical-slug rewrite, those lowercase forms now resolve to
    the right wire ids and aren't unstable. Only `vidu-q1` remains
    (replaced by `vidu-q3-pro-i2v` per the 2026-03-04 GMI blog).
  - `models/voices.py`, audio README quickstart, audio example, and
    audio family `example_slugs` all updated to the lowercase canonical
    forms.

- `genblaze-core` 0.3.0 → **0.3.2**: relocated `URLPolicy` /
  `URLPolicyError` from `genblaze_s3.url_policy` to
  `genblaze_core.storage.url_policy`. Required so `ObjectStorageSink`
  (which lives in core) can reference the enum without inverting the
  `genblaze-s3 → genblaze-core` dependency direction. Back-compat
  preserved: `from genblaze_s3.url_policy import URLPolicy` still
  resolves (the s3 module is now a thin re-export). New convenience:
  `from genblaze_core import URLPolicy`. `genblaze-s3`'s and
  `genblaze-gmicloud`'s minimum `genblaze-core` pin tightens to
  `>=0.3.2,<0.4`.

### Improved

- `genblaze-s3`: `S3StorageBackend.for_backblaze()` preflight now surfaces
  a specific region in the error message when the bucket lives in a B2
  region that returns 403 instead of 301 (e.g. `us-east-005`). On a 403
  from a B2 endpoint, the preflight probes the other published B2 regions
  in parallel and classifies:
  - Exactly one other region returns 200 → *"Bucket 'X' lives in
    `us-east-005` — pass `region='us-east-005'` to `for_backblaze()`."*
  - Every probed region returns 404 → *"Bucket 'X' does not exist in any
    known B2 region. Verify the bucket name."*
  - Mixed signals → today's generic message, now with the endpoint URL
    we tried.

  Probe clients share credentials with the primary client, carry a 3-second
  connect+read timeout with retries disabled, and are explicitly closed via
  `contextlib.closing` so the error path doesn't leak sockets in long-running
  daemons. Probe path is gated on B2 endpoints (`backblazeb2.com`) — AWS S3
  and R2 backends are unaffected. Addresses the 2026-05-23 feedback batch
  item 6.

### Fixed

- **GMICloud README + examples: corrected Kling slug casing + Veo casing.**
  Verified against GMICloud's own blog posts (no third-party sources used).
  The newest authoritative source is the [2026-04-14 Real-Time Video
  Generation Platforms blog](https://www.gmicloud.ai/en/blog/real-time-video-generation-platforms);
  cross-checked against the [2026-03-10 Most Popular AI Models blog](https://www.gmicloud.ai/en/blog/the-most-popular-ai-models-available-today)
  and the [2026-03-04 Compare Generative Media blog](https://www.gmicloud.ai/en/blog/compare-generative-media-ai-platforms-video-and-image).

  Changes:
  - `kling-text2video-v1.6-pro` → `Kling-Text2Video-V2.1-Master` (the V1.6-Pro
    text2video slug doesn't appear in any GMICloud blog; the V2.1-Master form
    is explicit in the 2026-04-14 blog at $0.28/req).
  - `kling-image2video-v2.1-master` → `Kling-Image2Video-V2.1-Master`
    (PascalCase per 2026-03-04 blog; the V2.1 designation is confirmed by
    the 2026-04-14 blog's matching Kling-Text2Video-V2.1-Master casing).
  - `veo3` → `Veo3` in the connector README provider table (PascalCase per
    every GMICloud blog from 2025-12-08 onward).

  Touched: `libs/connectors/gmicloud/README.md` (provider table, video
  quickstart), root `README.md` (chain example), `examples/gmicloud_video_pipeline.py`.

- **Rewrote the stale "Slug casing" note in `libs/connectors/gmicloud/README.md`.**
  The previous note claimed all GMI slugs are lowercase and that PascalCase
  ids rewrite via `ModelSpec.deprecated_aliases` — both false. Replaced with
  an accurate per-slug-casing description (GMICloud's published catalog
  uses lowercase for Sora/Pixverse/Seedance/Seedream/Reve/Wan/Bria/Gemini-Image
  and PascalCase for Kling V2.1 + Veo3, while newer Kling V2.5/V3 series use
  lowercase — verify on `console.gmicloud.ai` per slug).

- **Repo dev install**: stripped the stub `[project]` block from the root
  `pyproject.toml`. The stub declared `name="genblaze" version="0.1.0"`
  with empty dependencies, which made `pip install -e .` from the repo
  root install an extras-less 0.1.0 placeholder that shadowed the real
  `genblaze` umbrella from `libs/meta`. Users following the README saw
  *"no matching distribution for genblaze-gmicloud"* on `pip install -e
  ".[gmicloud]"` even though the umbrella on PyPI works correctly. After
  the strip, `pip install -e .` from the root fails cleanly (hatchling:
  *"Missing 'project' metadata table"*); use `make install-dev` for
  editable monorepo dev installs. Addresses the 2026-05-23 feedback batch
  item 4.

### Internal

- **Pre-release catalog verification checklist**: new
  [`docs/dev-workflows.md` §"Pre-release catalog verification"](docs/dev-workflows.md#pre-release-catalog-verification)
  with a per-provider table of upstream catalog/docs links. Maintainers
  click through before tagging a release to confirm that every slug in
  `family.example_slugs` and the README quickstarts still resolves on
  the provider's published catalog. Replaces an earlier scheduled CI
  probe design — provider catalogs rotate ~quarterly, so weekly polling
  was overkill and carried real audit-log + potential-paid-job cost.
  The existing `tools/probe_*.py` scripts remain as an optional
  programmatic sanity-check.

## [0.3.1] - 2026-05-18

### Released package versions

- `genblaze-s3` 0.3.0 → **0.3.1** — patch republish. The 0.3.0 wheel on PyPI was built before the `genblaze-core<0.4` widening and shipped with the stale `<0.3` constraint, which made `pip install "genblaze"` unresolvable after the 0.3.0 wave. Source already had the correct constraint; this bump forces PyPI to take the corrected wheel.
- All other packages — unchanged.

## [0.3.0] - 2026-05-11

### Released package versions

- `genblaze-core` 0.2.8 → **0.3.0** (minor — breaking: `ModelRegistry(defaults=...)` removed)
- `genblaze` (umbrella) 0.3.2 → **0.4.0**
- `genblaze-decart` 0.2.2 → **0.3.0**
- `genblaze-elevenlabs` 0.2.2 → **0.3.0**
- `genblaze-gmicloud` 0.2.6 → **0.3.0**
- `genblaze-google` 0.2.4 → **0.3.0**
- `genblaze-lmnt` 0.2.2 → **0.3.0**
- `genblaze-luma` 0.2.3 → **0.3.0**
- `genblaze-nvidia` 0.2.1 → **0.3.0**
- `genblaze-openai` 0.2.4 → **0.3.0**
- `genblaze-replicate` 0.2.2 → **0.3.0**
- `genblaze-runway` 0.2.3 → **0.3.0**
- `genblaze-stability-audio` 0.2.2 → **0.3.0**
- `genblaze-langsmith` — unchanged (no code edits this release)
- `genblaze-s3` — unchanged
- `genblaze-cli` — unchanged

### Hardening — production readiness (`model-registry-decoupling-hardening`)

Closes the BLOCKER + HIGH-severity findings from the post-rollout deep
review. See
[`docs/exec-plans/active/model-registry-decoupling-hardening.md`](docs/exec-plans/active/model-registry-decoupling-hardening.md)
for the design trail.

**Security:**
- `validate_chain_input_url` rewritten with multi-layer hardening:
  RFC 8089 netloc check; `urllib.parse.unquote()` before traversal
  detection (catches `..%2F` percent-encoded bypass); canonicalization
  via `Path.resolve()` (collapses `..`, follows symlinks, resolves
  macOS `/private/etc` aliases); denylist on the canonical path
  (`/proc`, `/dev`, `/sys`, `/etc`, `/private/etc`, `/private/var/run`,
  `/run/secrets`, `/var/run/secrets`); opt-in `file_root_allowlist`
  parameter for strict containment. Test corpus parametrized at 35
  cases including symlink-resolves-outside-root, percent-encoded,
  and double-encoded variants.

**Concurrency:**
- `BaseProvider._cached_probe` cleanup moved into `try/finally` so a
  `BaseException` from `_invoke_family_probe` no longer orphans the
  in-flight `threading.Event` (would have permanently blocked all
  subsequent waiters for that slug). Pinned by 7 regression tests
  including a 20× repeat run via `pytest-repeat`.
- `BaseProvider._poll_cache` reads/writes/cleanup now guarded by
  `threading.Lock`. Read-then-pop is atomic — concurrent `ainvoke()`
  callers (which dispatch via `asyncio.to_thread`) can no longer race.
  5 regression tests including an 8-thread × 100-cycle stress run.
- `FamilyProbe` contract docstring updated to require bounded duration
  via the underlying transport (`httpx.Client(timeout=...)` etc.) —
  the framework deliberately does not wrap probes in a separate
  `concurrent.futures` timeout (would add a thread layer without
  cancelling the in-flight HTTP request).

**Registry correctness:**
- `ModelRegistry.register_pricing(slug, strategy)` now falls through
  to `match_family()` when the slug isn't yet user-registered. The
  family-resolved spec (with all its param contracts) becomes the
  base; pricing layered on top. Previously, a bare `ModelSpec` with
  only `pricing` was minted, silently dropping the family's param
  aliases / schemas / allowlist / extras.
- `ModelRegistry.register_family()` now enforces a separate
  `MAX_USER_FAMILIES = 32` cap on the user layer. The connector cap
  (`MAX_PROVIDER_FAMILIES = 32`) only counts provider families — a
  connector at its provider cap does not block users from registering
  their own families. Total scan cost stays under 64 patterns.
- `ModelRegistry.fork()` now carries `_warned_deprecated` from parent
  to clone. Multi-tenant deployments that fork-per-request no longer
  spam deprecation warnings on every fork for an already-warned alias.

**Connector correctness:**
- `genblaze-google` Veo split into two families:
  `GOOGLE_VEO_LEGACY_FAMILY` (`^veo-2[.-]`, no audio) and
  `GOOGLE_VEO_FAMILY` (`^veo-` catch-all, `extras["has_audio"]=True`).
  `VeoProvider.fetch_output` reads `extras.get("has_audio")` instead
  of `step.model.startswith("veo-3")` — future `veo-N` slugs (N≥3)
  inherit synchronized-audio metadata automatically without a
  provider release.

**API uniformity:**
- LMNT, Replicate, and all NVIDIA providers (chat / audio / video /
  image) now accept `probe_cache_ttl` and `probe_cache_max_entries`
  ctor kwargs (no-ops on NATIVE / NONE providers; accepted for API
  uniformity).
- New `ProviderComplianceTests.test_accepts_probe_cache_kwargs`
  conformance test calls each provider's constructor with the kwargs
  and asserts no `TypeError` — catches `**kwargs`-forwarders that an
  inspect-only check would miss.

**Test coverage:**
- New `tests/test_catalog_decoupling.py` for LMNT and Replicate (the
  two empty-registry NATIVE/NONE proof-points).

**Documentation accuracy:**
- Migration guide and `model-registry.md` outcome tables now match
  `validation.py` exactly: four `ValidationOutcome` values
  (`OK_AUTHORITATIVE`, `OK_PROVISIONAL`, `UNKNOWN_PERMISSIVE`,
  `NOT_FOUND`); five `ValidationSource` values (`USER`, `FAMILY`,
  `DISCOVERY`, `PROBE`, `FALLBACK`). Phantom `KNOWN_UNSTABLE` enum
  removed; the unstable-slug case correctly described as
  `OK_PROVISIONAL` with `detail="known_unstable; ..."`.
- `Pipeline.preflight()` documented as a fluent setter (returns
  `Pipeline`); validation runs automatically inside `run()`. Use
  `provider.validate_model(slug)` for direct slug checks.
- `discover_models()` for NONE providers documented as returning
  `DiscoveryResult.unsupported(...)` (not empty results).
- `new-provider.md` `pyproject.toml` template constraint bumped to
  `>=0.3.0,<0.4`.
- LMNT added to the migration guide's NONE-provider list (matches
  CHANGELOG and `provider-system.md`).
- Deprecation horizon section added to migration guide:
  `BaseProvider.probe_model()` → removed in 0.4.0;
  `ModelRegistry(defaults=...)` already removed in 0.3.0.

### Added — model registry decoupling (PRs #1–#4)

- **`genblaze-core 0.3.0` (in progress)**: introduces a new pattern-based
  model-catalog architecture. The SDK no longer ships hardcoded slug lists.
  Connectors declare:
  - `discovery_support: DiscoverySupport` — `NATIVE`, `PARTIAL`, or `NONE`.
  - `provider_families: tuple[ModelFamily, ...]` — pattern-keyed param-shape
    rules, replacing per-slug `defaults={...}` dicts.
  - Optional `family.probe` callable for `PARTIAL` providers (NIM
    generative endpoints use the empty-payload POST trick).
  See `docs/exec-plans/active/model-registry-decoupling.md` for the
  architecture, red-team trail, and rollout sequence.

- New public types in `genblaze_core.providers`:
  `ModelFamily`, `FamilyMatch`, `FamilyProbe`, `LiveProbeResult`,
  `DiscoverySupport`, `DiscoveryResult`, `DiscoveryStatus`,
  `ValidationResult`, `ValidationOutcome`, `ValidationSource`,
  `MAX_PROVIDER_FAMILIES`, `DEFAULT_TTL_SECONDS`.

- New public methods on `BaseProvider`:
  - `discover_models()` — snapshot the upstream catalog (NATIVE only).
  - `validate_model(slug, *, refresh=False)` — graded slug-validity outcome.
  - `_invoke_family_probe(probe, slug)` — connector-side hook for PARTIAL
    providers to wire their `httpx.Client` into the family probe.

- New `Pipeline(preflight=True)` knob (default ON, soft-launch posture).
  `Pipeline.run()` validates every step's model in parallel via
  `ThreadPoolExecutor` before any wire calls. `NOT_FOUND` raises
  `ProviderError(MODEL_ERROR)`; `OK_PROVISIONAL` and `UNKNOWN_PERMISSIVE`
  emit one-time WARN logs. Opt out with `Pipeline(preflight=False)`.

- New connector adoption (this release):
  - **lmnt** — `DiscoverySupport.NONE` proof-point.
  - **replicate** — `DiscoverySupport.NATIVE`. `discover_models()`
    returns the first page of `/v1/models`; `validate_model()` does
    per-slug `client.models.get()` lookups (cached per-process, 1-hour TTL).
  - **nvidia** — chat = `NATIVE` via `/v1/models`; audio/image/video =
    `PARTIAL` with the empty-payload `genai` probe.
  - **gmicloud** — audio/image/video = `PARTIAL` with the empty-payload
    `/requests` probe. The 2026-04 reconciliation's `suspected_dead`
    slugs (`veo3-fast`, `kling-text2video-v2.1-master`,
    `minimax-hailuo-2.3-fast`, `vidu-q1`, all 5 audio defaults) are
    preserved as `ModelFamily.unstable_examples` (RT-10) — the probe is
    the authoritative answer at runtime; users see a "known unstable"
    hint at preflight. PascalCase `deprecated_aliases` are removed
    (soft-launch clean break); pass canonical lowercase slugs.
  - **runway** — `DiscoverySupport.NONE` with a single
    `runway-gen-video` family covering `^gen\w+_turbo$` (Gen-3a,
    Gen-4, future variants). The runwayml SDK doesn't expose raw HTTP
    for the empty-payload probe pattern, so submit-time errors are the
    authoritative liveness signal. Pricing tables removed; recipe
    published at `docs/reference/pricing-recipes.md`.
  - **decart** — `DiscoverySupport.NONE` for both video and image
    providers. Two families: `decart-lucy-video` (pattern absorbs
    `^lucy-.*(?:2v|motion|restyle)`) and `decart-lucy-image`
    (`^lucy-.*2i$`). Cross-modality isolation guarded by tests so video
    slugs don't leak into the image registry and vice versa. Pricing
    removed; recipe published.
  - **elevenlabs** — TTS = `DiscoverySupport.NATIVE` via
    `client.models.get_all()` with a single `^eleven_` family pattern;
    SFX = `DiscoverySupport.NONE` (single-model surface, models endpoint
    may not enumerate SFX). TTS retired-slug verdicts surface as
    `NOT_FOUND` at preflight before any wire calls. Per-1K-char and
    duration-bucket pricing tables removed; recipes published.
  - **openai** — TTS, DALL-E (gpt-image + dall-e), and Sora all declare
    `DiscoverySupport.NATIVE` via `client.models.list()`. Each
    provider's fetcher filters the global OpenAI catalog by its family
    pattern, so chat / embeddings / Whisper slugs don't pollute
    cross-modality caches. Two image families
    (`openai-gpt-image` ^gpt-image-, `openai-dalle` ^dall-e-) keep the
    bespoke `_validate_params` logic in the provider while the families
    handle slug routing. Per-tier TTS pricing and per-(quality,size)
    image pricing tables removed. Sora pricing remains None — the
    correct formula is `(model, size, seconds)` per-second; a flat
    table misreports 10x+. Standalone chat module out of scope (not a
    Pipeline-Step provider).
  - **google** — Veo (`^veo-`) and Imagen (`^imagen-`) now ship as
    pattern-keyed families with `DiscoverySupport.PARTIAL`. Liveness
    is confirmed via `client.models.get(model=slug)` — the canonical
    "is this model available to my project?" lookup — so dead /
    unauthorized slugs surface as `NOT_FOUND` at preflight without
    enqueuing a generation. Per-second Veo and per-image Imagen
    pricing tables removed; recipes published. Standalone Gemini
    `chat()` retains its per-token table (out of scope — not a
    Pipeline-Step provider).
  - **luma** — single `^ray-` family covers `ray-2`, `ray-flash-2`,
    and future `ray-N` / `ray-*-N` variants. Reclassified from
    `PARTIAL` to `DiscoverySupport.NONE` during implementation: the
    lumaai SDK exposes no per-slug probe that doesn't enqueue a
    billable generation, and the upstream catalog is small and
    stable (mirrors the Runway / Decart precedent). Luma was
    already pricing-None — the recipe documents the canonical
    per-second-by-model strategy for users who want cost
    attribution.
  - **stability-audio** — single `^stable-audio-` family covers
    `stable-audio-2.5` and future `stable-audio-N` variants.
    `DiscoverySupport.NONE`: Stability ships no Python SDK or
    `/v1/models` endpoint for audio, the upstream catalog is
    effectively a single line, and submit-time errors plus the
    family pattern are sufficient. The hardcoded $0.01/sec rate
    table removed; recipe published preserving the duration-fallback
    behavior (probe → params).

### Removed — `ModelRegistry(defaults=...)` transitional shim

- The ``defaults=`` constructor parameter is gone. PR #1 added it as a
  code-path migration shim alongside ``provider_families=`` so the
  per-connector PRs (#2-#12) could land incrementally without breaking
  the world. Every connector now uses ``provider_families=`` (or
  ``fallback=`` alone for empty-catalog providers like LMNT and
  Replicate). User code that previously built registries via
  ``ModelRegistry(defaults={"slug": spec})`` should migrate to the
  post-construction surface:

  ```python
  reg = ModelRegistry()
  reg.register(spec)         # single spec
  reg.extend([s1, s2, ...])  # bulk
  ```

  Resolution semantics are unchanged — the legacy ``_defaults`` dict
  and the user ``_user`` dict were always checked at the same
  precedence tier (``_user.get(slug) or _defaults.get(slug)``), so
  migrating to ``register()`` / ``extend()`` is a pure constructor-
  vs-method rephrasing. The dead-code branch in ``validate()`` that
  minted ``OK_AUTHORITATIVE`` with a ``"legacy defaults shim"``
  detail string is also gone.

- New conformance test
  (``libs/core/tests/unit/test_no_defaults_kwarg.py``) gates the
  connector tree against re-introducing the kwarg.

- ``FamilyProbe`` switched from a kwarg-pinned ``Protocol`` to a
  ``Callable[..., LiveProbeResult]`` type alias. The old shape locked
  every probe's keyword argument to ``http: httpx.Client``, which
  excluded SDK-based probes (e.g. ``client.models.get`` for
  ``genblaze-google``). The new alias is honest about transport
  variability — connectors choose their probe's keyword shape; the
  contract is "first positional is slug, return is
  ``LiveProbeResult``." Fixes the typecheck-connectors CI failure
  introduced when PR #10 landed before the protocol was loosened.

### Fixed — F-2026-05-04-01 (NVIDIA `nvidia/riva-tts` 404)

- The retired `nvidia/riva-tts` slug is no longer pinned in the SDK. Users
  who still pass it now get a deterministic `ProviderError(MODEL_ERROR)`
  at preflight (via the empty-payload probe) instead of a mid-pipeline 404.
  `nvidia/magpie-tts-multilingual` is the surfaced "Did you mean…?" hint.
- New end-to-end repro test
  (`libs/connectors/nvidia/tests/test_catalog_decoupling.py::test_riva_tts_surfaces_at_preflight_not_mid_pipeline`)
  pins this regression class going forward.

### Changed — pricing phase-out

- The SDK no longer ships hardcoded prices. `_PRICE_PER_CHAR`, `_COST_PER_SEC`,
  `_compute_time_cost`, and the per-slug pricing on connector fallback
  specs have been removed. `compute_cost()` and `Pipeline.estimated_cost()`
  return `None` for any model unless the user has registered pricing via
  `provider.models.register_pricing(slug, strategy)`.
- New `docs/reference/pricing-recipes.md` cookbook holds the last-known
  prices per connector as one-shot copy-paste recipes. **Not maintained**:
  verify with the upstream provider before relying on the values.
- `compute_cost()` and `register_pricing()` themselves are unchanged; the
  pricing-strategy primitives (`per_unit`, `by_param`,
  `bucketed_by_duration`, `per_input_chars`, `per_response_metric`,
  `by_model_and_param`, etc.) remain in `genblaze_core.providers.pricing`.

### Deprecated

- `BaseProvider.probe_model()` is deprecated and now delegates to
  `validate_model(refresh=True)` with a coerced `ProbeResult` for legacy
  `tools/probe_models.py` consumers. Slated for removal in
  `genblaze-core 0.4.0`. Use `validate_model()` directly going forward.

### Documentation

- New migration guide: [`docs/guides/migrating-to-0.3.md`](docs/guides/migrating-to-0.3.md)
  — TL;DR for callers of 0.2.x, before/after for the 4 most common
  registry patterns, validation-outcome handling, and the decision
  tree for unexpected `NOT_FOUND` outcomes.
- `docs/features/model-registry.md` reorganized so `ModelFamily` leads
  the conceptual picture (pattern-keyed routing) with `ModelSpec` as
  the per-slug override path. Adds `DiscoverySupport` / `FamilyProbe` /
  `ValidationResult` reference sections and a `validate_model(refresh=True)`
  example.
- `docs/features/provider-system.md` Cost Tracking section rewritten
  to point at [`docs/reference/pricing-recipes.md`](docs/reference/pricing-recipes.md)
  instead of listing per-connector strategies that no longer ship.
  New `DiscoverySupport` overview describing the three-tier classification.
- `docs/guides/new-provider.md` updated for the families-first model:
  new "Declare your model families and DiscoverySupport" section with
  a decision tree for picking `NATIVE` / `PARTIAL` / `NONE` and
  worked examples for each tier's required hooks
  (`_fetch_models`, `_invoke_family_probe`).
- `README.md` and `ARCHITECTURE.md` updated to surface the new
  catalog architecture and cross-link the migration guide.
- `docs/reference/pricing-recipes.md` is now the single source of truth
  for per-provider rate sheets (LMNT, Replicate, GMICloud, Runway,
  Decart, ElevenLabs, OpenAI, Google, Luma, Stability-Audio). Each
  section is dated and linked to the upstream pricing URL.

## [0.2.9] - 2026-04-30

### Released package versions
- Code-change bumps: `genblaze-core` 0.2.8, `genblaze-s3` **0.3.0** (minor —
  near-total backend rewrite with async, encryption, presigned URLs).
- Metadata + version-source bumps: `genblaze` (umbrella) 0.3.2 — switches
  `__version__` to `importlib.metadata`, adds keywords, widens
  `genblaze-s3` pin to `<0.4` so it can resolve the new minor.
- `@genblaze/spec` (npm) 0.4.0 — `step.schema.json` contract change
  (already bumped locally; ready to publish).
- Untouched (no republish): `genblaze-stability-audio` 0.2.2 (only
  internal version-source refactor, no behavior change), and every other
  connector + cli at their current versions.

### Added
- `genblaze-core`: net-new public modules — `_optional.py` (84 lines,
  optional-import framework), `pipeline/ingest.py` (223 lines, ingest
  orchestration for standalone asset writes), `storage/config.py`,
  `storage/errors.py`, `storage/types.py`, `storage/key_builder.py`,
  `storage/_tracer.py`. `storage/base.py` and `storage/sink.py` grew
  substantially (+292 / +158 lines); `transfer.py` extended.
- `genblaze-s3`: full async backend (`async_backend.py`, 476 lines),
  end-to-end encryption (`encryption.py`, 211 lines), presigned URL
  support (`presigned.py`, 117 lines), preflight-classification
  (`_preflight_classify.py`), URL-policy enforcement (`url_policy.py`),
  user-agent module (`_user_agent.py`). `backend.py` grew by 1018 lines
  to support these. Comprehensive new test coverage across phase 2A/2B/2C
  regression suites.
- Tooling: `tools/check_pypi_metadata.py` (180 lines) for catching
  metadata drift before publish.

### Fixed
- **Build-time / runtime version drift** (Plan 5 Phase 1A/1B):
  `pyproject.toml`'s literal `version = "X.Y.Z"` is now the single source
  of truth. Hatchling propagates it into the wheel METADATA;
  `_version.py` reads it back via `importlib.metadata.version(...)`. So
  `genblaze_core.__version__`, `pip show genblaze-core`, and the
  `b2ai-genblaze/{version}` user-agent header always agree with the
  installed wheel. No more manual edits to `_version.py` per release.
  Applied to `genblaze-core`, `genblaze-s3`, `genblaze-stability-audio`,
  and `genblaze` (umbrella). Removed all `[tool.hatch.version]` blocks
  and `dynamic = ["version"]` declarations.
- **All 13 connectors**: ``__version__`` now reads from
  ``importlib.metadata`` (``genblaze-{slug}``) rather than being a
  hardcoded string — same drift class as the core/umbrella fix below,
  rolled across ``genblaze-s3``, ``genblaze-openai``,
  ``genblaze-google``, ``genblaze-replicate``, ``genblaze-runway``,
  ``genblaze-luma``, ``genblaze-decart``, ``genblaze-elevenlabs``,
  ``genblaze-lmnt``, ``genblaze-stability-audio``, ``genblaze-nvidia``,
  ``genblaze-gmicloud``, and ``genblaze-langsmith``. Pinned by
  ``TestConnectorVersionCoherence`` in
  ``test_version_coherence.py`` — adding a 14th connector that
  hardcodes its ``__version__`` will fail CI.
- `genblaze-core`: ``__version__`` now reads from ``importlib.metadata``
  rather than a hardcoded string. Closes the version-drift footgun
  (storage tranche bug #9): pre-fix the constant was edited per
  release and silently drifted out of sync with
  ``importlib.metadata.version("genblaze-core")`` and the
  ``b2ai-genblaze/{version}`` user-agent header. The smoke check
  on the migration confirmed the prior hardcoded ``"0.2.7"`` was
  already drifted from the actual installed wheel ``"0.2.3"`` —
  exactly the bug the plan flagged. Same fix applied to the
  umbrella ``genblaze`` package's ``__version__``. Test
  ``test_version_coherence.py`` pins the invariant going forward.
- `genblaze-core`: optional-dependency import errors now raise
  :class:`OptionalDependencyError` with the install incantation in
  the message (``pip install 'genblaze[parquet]'``) — not a bare
  ``ModuleNotFoundError``. Closes storage tranche bug #8.
  ``OptionalDependencyError`` subclasses ``ImportError`` so legacy
  ``except ImportError:`` callers continue to catch it without code
  changes; new callers can be more specific via
  ``except OptionalDependencyError:``. Wired into
  ``ParquetSink``'s pyarrow gate; future optional-extra modules
  (``aioboto3`` for AsyncS3StorageBackend, etc.) can adopt the same
  pattern.

### Added
- `genblaze-s3`: ``genblaze_s3._user_agent.build_user_agent(*,
  base=None, extra=None)`` helper. Default base is
  ``b2ai-genblaze/{genblaze_core.__version__}`` so the user-agent
  always tracks the installed core wheel — no more two-step "bump
  core, then bump the connector's hardcoded UA" dance. ``base`` lets
  forks override the prefix; ``extra`` composes with
  ``StorageConfig.user_agent_extra`` so application identifiers
  append cleanly. ``backend.py`` now wires
  ``_USER_AGENT = build_user_agent()`` and the legacy
  f-string-on-import has been removed. Pinned by
  ``test_user_agent.py``.
- **CI gate**: ``tools/check_pypi_metadata.py`` audits every published
  ``pyproject.toml`` (``libs/**`` plus ``cli/``) for the metadata
  fields PyPI search and project pages render — ``description``,
  ``readme``, ``authors``, ``license``, ``requires-python>=3.11``,
  classifier groups (``License`` / ``Programming Language :: Python ::
  3.1*`` / ``Topic`` / ``Development Status``), ``project.urls``
  (Homepage / Documentation / Repository / Issues), and ``keywords``.
  Wired as ``make pypi-metadata-check`` (``--strict`` mode exits 1 on
  any miss) so a release-prep PR that adds a new package missing
  classifiers/keywords/etc. fails loudly rather than rendering empty
  on PyPI.
- `genblaze-core`: ``genblaze_core._optional`` module with
  :class:`OptionalDependencyError` and a :func:`require(extra,
  package, symbol=None)` helper for module-top-level use in
  optional-extra-gated code.

- `genblaze-core` / `@genblaze/spec`: ingest-sink tranche **Phase 2** —
  ``Pipeline.ingest()`` factory for non-generative bulk imports. Closes
  the "podcast hosting / DAM bulk import / RSS pull / UGC upload"
  use cases the plan called out as forced to fabricate
  ``SyncProvider`` shims today.
  - ``Pipeline.ingest(assets, *, source, source_metadata=None, sink=None,
    name=None, tenant_id=None, step_type=StepType.INGEST) ->
    PipelineResult`` — classmethod factory. Each asset becomes a
    :class:`Step` with ``step_type=StepType.INGEST`` (or ``IMPORT``),
    ``provider=None``, ``model=source``, ``modality`` inferred from
    ``asset.media_type``, ``status=StepStatus.SUCCEEDED``. Step
    metadata carries ``{"source": source, **source_metadata}``. The
    factory orders assets by ``asset_id`` before building steps so
    the resulting manifest's canonical hash is **invariant under
    permuted input order** — a podcast app calling ``ingest`` with
    feed entries in any order produces a byte-identical manifest.
  - When a sink and ``tenant_id`` are supplied, ``put_asset`` is called for
    each asset with a derived ``manifest_uri`` and tenant context so
    :meth:`BaseSink.read_manifest_for_asset` can later discover the
    manifest from that tenant's asset_id. Without ``tenant_id``, assets are
    uploaded without a reverse-lookup index. Sinks that don't implement
    ``put_asset`` (e.g. ``ParquetSink``) emit a warning and skip
    the upload — manifest still produced for in-memory consumers.
  - New module ``genblaze_core.pipeline.ingest`` houses the
    orchestration; ``Pipeline.ingest`` is a thin classmethod
    wrapper so the fluent ``.step(...)`` builder surface stays
    focused on generation.

- `genblaze-core` / `@genblaze/spec`: **StepType.INGEST and
  StepType.IMPORT** — non-generative step type values. Added as the
  Plan 4 Phase 2 slice of the master plan's Wave 4 enum extension
  (``StepType.{TRANSCRIBE, CLASSIFY, ANALYZE, EXTRACT, MODERATE}``
  remain in Wave 4 scope).

### Changed
- **PyPI metadata sweep**: every published package's
  ``pyproject.toml`` now carries the full classifier set
  (``License :: OSI Approved :: MIT License``, ``Programming Language
  :: Python :: 3.{11,12,13}``, ``Topic :: Multimedia`` + ``Topic ::
  Software Development :: Libraries``, ``Development Status``,
  ``Typing :: Typed``), per-package ``keywords`` (common base —
  genblaze / ai / media / manifest / provenance / c2pa-ready / genai
  / pipeline — plus provider-specific tags), and a complete
  ``project.urls`` block. The ``cli`` package gained the
  ``Documentation`` URL it was missing. PyPI search and project
  pages now render rich previews instead of blank metadata.
- `genblaze-core` / `@genblaze/spec`: ``Step.provider`` is now
  ``str | None``. A new model validator requires ``provider`` to be
  set unless ``step_type ∈ {INGEST, IMPORT}`` — non-generative step
  types may have null provider (no upstream service to attribute);
  every other step type continues to require provider as before.
  Wire schema (``manifest/v1/step.schema.json``) reflects the
  change: ``provider`` is now nullable and removed from the
  ``required`` array; the ``step_type`` enum gains ``"ingest"`` and
  ``"import"`` values. ``@genblaze/spec`` bumped 0.3.3 → 0.4.0
  (additive minor for the enum; field-relaxation is also
  forward-compatible since older consumers expecting non-null
  provider only break for the new step types they wouldn't have
  emitted anyway).
- `genblaze-core`: three internal call sites that read
  ``step.provider`` and forward it through a non-Optional surface
  (``StepFailedEvent`` / ``StepCompletedEvent`` provider field, OTel
  span attribute, ``ParquetSink`` partition path) now coerce ``None``
  to a sentinel. The wire / observability shape is preserved for
  generative steps; ingest steps emit ``provider=""`` on the wire
  and skip the partition-path provider segment (filtered out before
  ``sorted``).

### Added
- `genblaze-core`: ingest-sink tranche **Phase 1** — standalone asset
  writes via :class:`BaseSink`. Closes the "non-generative workflow"
  gap where DAM, archival, podcast-hosting, and UGC apps had to
  fabricate `SyncProvider` shims to seed assets through the
  generation-shaped pipeline.
  - ``BaseSink.put_asset(asset, *, manifest_uri=None, tenant_id=None) -> Asset`` —
    write a single asset's bytes via the sink's storage backend
    (no Run wrapper required). Mutates the asset in place: rewrites
    ``url`` to the durable backend URL, populates ``sha256`` and
    ``size_bytes``. Source bytes resolved from the asset's existing
    URL (``file://`` allowlisted dirs, or SSRF-protected ``https://``).
    Default impl on the ABC raises ``NotImplementedError`` so
    non-storage-backed sinks (``ParquetSink`` etc.) keep working.
  - ``BaseSink.put_assets(assets, *, manifest_uri=None, tenant_id=None) ->
    list[Asset]`` — bulk variant, parallelizes via
    ``ThreadPoolExecutor`` sized at ``min(max_upload_workers,
    len(assets))``. Returned list preserves input order.
  - ``BaseSink.read_manifest_for_asset(asset_id, *, tenant_id) -> Manifest |
    None`` — reverse-lookup. When ``put_asset`` is called with
    ``manifest_uri=`` and ``tenant_id=``, the sink writes a tenant-scoped
    sidecar index entry at ``{prefix}/_index/{tenant_id}/{asset_id}.json`` so
    future callers can discover the manifest from the asset_id inside their
    authorization context. Manifests for assets put without ``manifest_uri=``
    and ``tenant_id=`` are not discoverable
    via this method (by design — opt-in). Returns ``None`` for
    unknown asset_ids and for foreign-backend manifest URIs that
    don't round-trip through ``key_from_url``.
  - ``ObjectStorageSink`` is the concrete implementation; reuses
    the existing ``AssetTransfer`` machinery (download, hash,
    KeyBuilder-routed put). Phase 2 (``Pipeline.ingest()``
    factory) blocks on the master plan's Wave 4 ``StepType.INGEST``
    enum addition.

### Fixed
- `genblaze-s3`: every ``S3StorageBackend`` and
  ``AsyncS3StorageBackend`` operation now wraps unexpected exceptions
  via ``classify_botocore_error`` so the resulting ``StorageError``
  carries populated ``error_code`` / ``status_code`` /
  ``is_retriable`` / ``operation`` / ``request_id`` fields. Pre-fix
  every operation raised a bare ``StorageError(f"...failed: {exc}")``
  with all structured fields ``None`` — defeating the plan's primary
  observability acceptance gate ("StorageError round-trips
  request_id, status_code, is_retriable, operation, error_code"). 13
  call sites updated across ``put`` / ``get`` / ``exists`` /
  ``delete`` / ``copy`` / ``head`` / ``list`` / ``get_range`` /
  ``stream`` / ``delete_many`` / ``get_url`` / ``presigned_get`` /
  ``presigned_put`` (sync) and ``aget`` / ``astream`` (native
  async). Surfaced during the cross-phase final review.
- `genblaze-core`: ``StorageBackend`` ABC async pairs accept
  ``**kwargs`` for connector-specific options. ``aput`` /
  ``acopy`` / ``aget`` / ``ahead`` / ``aget_range`` now forward
  arbitrary kwargs through ``asyncio.to_thread`` to the matching
  sync method. Pre-fix the ABC defaults silently dropped
  ``encryption=`` / ``object_lock=`` / ``progress=`` for callers
  typed against the ABC — e.g. an SSE-C HEAD via the ABC async
  surface would treat the encrypted object's 403 as not-exist
  instead of decrypting. Surfaced during the cross-phase final
  review.

### Documentation
- `docs/features/ingest-workflows.md`: NEW. Full feature doc for the
  ingest tranche covering Pipeline.ingest API, the four canonical
  use cases (podcast hosting, UGC upload, archival/DAM bulk import,
  cross-system/cross-tenant transfers, RTMP-style live ingest),
  reverse-lookup pattern via read_manifest_for_asset, manifest
  determinism gate (hash invariance under permuted input order),
  INGEST vs IMPORT semantics, composition with the generative
  Pipeline.step() builder, and cross-links to object-storage.md
  / manifest-provenance.md / moderation.md.
- `examples/ingest_podcast_episode.py`: NEW runnable. Pulls episode
  list, ingests into B2 with CONTENT_ADDRESSABLE dedup, prints
  per-asset attribution + manifest hash, demonstrates reverse
  lookup. Notes Wave 6 Whisper transcribe chain at the bottom.
- `examples/ingest_ugc_upload.py`: NEW runnable. Synthesizes a
  user-uploaded PNG, ingests via Pipeline.ingest with full
  uploader source_metadata (uploader_id / session_id / ip /
  user_agent), demonstrates reverse lookup, sketches the
  ModerationHook integration.

- `docs/features/object-storage.md`: added Phase 2 (read primitives,
  bulk deletes, progress callbacks, per-put Object Lock) and Phase 3
  (``AsyncS3StorageBackend``, native async vs threadpool-delegated
  methods, ``from_sync`` pattern) sections plus callouts for the
  ``aput``-progress-fires-on-thread-not-event-loop caveat and the
  SSE/Object Lock conflict-guard ``ValueError`` (raised before the
  network try/except, so distinct from ``StorageError``).
- `libs/connectors/s3/README.md`: matching Phase 2/3 quickstart
  sections.
- `docs/exec-plans/active/storage-backend-hardening-tranche.md`:
  inline annotation that ``presigned_post`` is intentionally
  deferred (separate ``PresignedPost`` value object needed).

### Fixed
- `genblaze-s3`: ``AsyncS3StorageBackend`` async client now passes an
  ``AioConfig`` mirroring the sync ``BotoConfig`` —
  ``request_checksum_calculation="when_required"`` (B2 CRC32-trailer
  fix), ``connect_timeout=30``, ``read_timeout=300``,
  ``max_pool_connections=20``, ``user_agent_extra="b2ai-genblaze/…"``.
  Pre-fix the async path inherited aiobotocore's defaults, which on
  boto3 >= 1.36 inject the same trailer that broke async ``aget`` /
  ``astream`` on B2 endpoints. Surfaced during the Phase 3 final
  review (BLOCKING).
- `genblaze-s3`: ``AsyncS3StorageBackend.__aexit__`` reorders cleanup
  so a failed inner ``__aexit__`` (aiohttp connector teardown
  exception) doesn't leave the backend holding a stale client ctx.
  The references are cleared upfront on a local snapshot; any
  exception from the inner exit propagates naturally without
  preventing future ``async with`` re-enters from succeeding.
  Surfaced during the Phase 3 final review (IMPORTANT).
- `genblaze-s3`: ``AsyncS3StorageBackend.from_sync`` now carries
  forward ``_region_verified``, the (possibly auto-corrected)
  ``_region`` / ``_endpoint_url``, and ``_preflight_error`` from the
  source sync backend. Pre-fix the new internal sync delegate ran
  another preflight ``HeadBucket`` even when the source was already
  verified — a redundant round-trip on every ``from_sync`` call.
  Aio kwargs are rebuilt after the copy so endpoint/region rewrites
  flow through. Surfaced during the Phase 3 final review (IMPORTANT).

### Changed
- `genblaze-s3`: ``AsyncS3StorageBackend.astream`` return annotation
  refined from ``AsyncIterator[bytes]`` to ``AsyncGenerator[bytes,
  None]`` so the type system signals "iterate, don't await" — callers
  who write ``await ab.astream(...)`` get a clearer error. ``progress``
  parameter typing tightened on ``aget`` and ``astream`` from
  ``Any`` to ``Callable[[TransferProgress], None] | None``. Surfaced
  during the Phase 3 final review.

### Added
- `genblaze-s3`: storage-backend hardening **Phase 3** native async via
  ``aioboto3`` — closes the long-deferred async-iterator gap that
  Phase 0 explicitly punted to here.
  - New ``AsyncS3StorageBackend`` class (concrete, not an ABC). Use
    as an async context manager::

        async with AsyncS3StorageBackend.from_sync(my_sync_backend) as ab:
            data = await ab.aget("k")
            async for chunk in ab.astream("big.mp4"):
                ...

    The wrapped sync backend is exposed at ``ab.sync`` for callers
    who need the historical surface (lifecycle helpers, key utils)
    without leaving the async context.
  - **Native async**: ``aget`` (single-shot or chunked-progress
    download via ``await Body.read()``) and ``astream`` (genuine
    ``AsyncIterator[bytes]`` via aioboto3's ``Body.iter_chunks``).
    The streaming path is the headline win — Phase 0's threadpool
    wrap couldn't faithfully adapt a sync iterator into an
    ``AsyncIterator`` without buffering the whole body.
  - **Threadpool-delegated** (current sub-phase): ``aput``, ``ahead``,
    ``alist``, ``aexists``, ``adelete``, ``acopy``, ``adelete_many``,
    ``adelete_prefix``, ``aget_range``, ``aget_url``,
    ``aget_durable_url``. These dispatch to the wrapped sync backend
    via ``asyncio.to_thread``. Native versions are tracked as a
    follow-up sub-phase — ``aput`` in particular needs aioboto3-native
    multipart support which is more involved.
  - **Optional dependency**: install via ``pip install
    'genblaze-s3[async]'`` (or ``aioboto3>=12,<13`` directly).
    ``import genblaze_s3`` works without the extra; only
    ``async with AsyncS3StorageBackend(...) as ab:`` requires it,
    and raises ``ImportError`` with the extras hint when missing.
  - ``AsyncS3StorageBackend.from_sync(sync_backend)`` constructs an
    async backend that shares an existing sync backend's settings
    (bucket / region / credentials) — common pattern for apps
    adding async to an established setup.

### Fixed
- `genblaze-s3`: ``_adapt_progress_to_boto3_callback`` now lock-protects
  the cumulative-byte counter. boto3 invokes the ``Callback=`` from
  multiple part workers concurrently (default ``max_concurrency=4``);
  the closure's ``nonlocal cumulative += delta`` compiled to
  LOAD/BINARY_ADD/STORE bytecodes, and the GIL releases between
  bytecodes — concurrent threads could load the same stale value and
  silently drop deltas. Cumulative byte counts under stress now match
  the sum of all reported deltas. Surfaced during the Phase 2 final
  review; my docstring's claim that boto3 serializes Callback
  invocations was wrong.
- `genblaze-s3`: ``_data_size`` now honors ``BytesIO.tell()`` so a
  partially-consumed buffer reports the correct *remaining* byte
  count for ``TransferProgress.total_bytes``. Pre-fix:
  ``data.getbuffer().nbytes`` returned the full allocated buffer
  regardless of read position, so callers who seeked or partially-read
  the BytesIO before passing it to ``put`` saw ``total_bytes``
  overstated — progress UIs hit 100% well before the upload finished.
  Surfaced during the Phase 2 final review.
- `genblaze-s3`: ``S3StorageBackend.get(progress=…)`` chunked path now
  pre-allocates a ``bytearray(ContentLength)`` when the total is known
  and writes chunks into it directly. Pre-fix: the impl appended each
  chunk to ``parts: list[bytes]`` and called ``b"".join(parts)`` at
  the end — ~2× peak memory (intermediate list + final bytes) plus
  one Python object per chunk. Defensive truncate when Content-Length
  overstates the actual body length. The fast path
  (``progress=None``) is unchanged. Surfaced during the Phase 2
  final review.
- `genblaze-s3`: ``S3StorageBackend.delete_prefix`` now surfaces
  partial-progress state on a mid-walk ``list()`` failure. Pre-fix:
  if ``list()`` raised on page N, the ``StorageError`` propagated
  uncaught and the already-deleted keys from pages 1..N-1 were
  invisible to the caller. Post-fix: the exception is captured into
  a synthetic ``DeleteError(key="", code="list_failed", message=…)``
  on the returned ``DeleteResult``; ``result.deleted`` carries the
  keys actually removed before the failure. Caller can detect and
  recover. Surfaced during the Phase 2 final review.
- `genblaze-s3`: ``S3StorageBackend.stream`` docstring documents the
  early-exit connection-lifecycle cost (``gen.close()`` mid-iteration
  discards the underlying HTTP connection rather than returning it
  to the urllib3 pool — botocore ``StreamingBody.close()`` doesn't
  drain). Negligible for typical full-stream consumers; worth
  knowing for high-fanout consumers that frequently abort streams.
  Surfaced during the Phase 2 final review.

### Added
- `genblaze-core` / `genblaze-s3`: storage-backend hardening **Phase 2C**
  progress callbacks + per-put Object Lock — closes the last two
  missing-primitive rows for synchronous Phase 2.
  - New ``TransferProgress(bytes_transferred, total_bytes, operation,
    key)`` frozen dataclass. ``total_bytes=None`` is the documented
    "unknown total" signal for stream sources where the size would
    require a draining pass to determine.
  - ``backend.put(progress=…)`` adapts boto3's ``Callback=`` (delta
    bytes per multipart chunk) to a cumulative ``TransferProgress``
    via a closure-cell accumulator. ``total_bytes`` is inferred
    automatically for ``bytes`` and ``io.BytesIO`` payloads;
    arbitrary ``BinaryIO`` streams pass ``None`` (boto3's transfer
    manager can't report the total without draining). The
    single-PUT path (caller pinned ``ChecksumSHA256``) silently
    skips progress because ``put_object`` doesn't accept a
    ``Callback`` parameter.
  - ``backend.get(progress=…)`` switches to a 1 MiB chunked-read
    loop that fires the callback with cumulative totals. Without
    ``progress=``, the historic single-call ``body.read()`` fast
    path is preserved — no allocation overhead for callers who
    don't need progress.
  - ``backend.stream(progress=…)`` fires per yielded chunk
    (``chunk_size`` defaults to 8 MiB).
  - ``backend.put(object_lock=ObjectLockConfig(...))`` applies
    per-put Object Lock retention. Useful when most uploads to a
    bucket don't need retention but a specific manifest does —
    finer granularity than the sink-wide ``manifest_lock``.
  - ``_build_extra_args`` adds an Object Lock conflict guard mirroring
    the SSE pattern: passing both ``object_lock=`` and an overlapping
    ``extra_args`` key (``ObjectLockMode``, ``ObjectLockRetainUntilDate``,
    or ``ObjectLockLegalHoldStatus``) raises ``ValueError`` rather
    than silently merging mismatched envelopes.
  - ``TransferProgress`` re-exported via ``genblaze_core.__all__``.

- `genblaze-core` / `genblaze-s3`: storage-backend hardening **Phase 2B**
  bulk-delete primitives — ``delete_many`` and ``delete_prefix``, plus
  two new value-object types (``DeleteError``, ``DeleteResult``).
  - ``backend.delete_many(keys: Sequence[str], *, dry_run=False) ->
    DeleteResult`` issues batched ``DeleteObjects`` calls (chunked at
    1000 keys, S3's hard cap). Per-key failures land in
    ``result.errors`` rather than aborting the batch — partial-success
    callers can salvage what worked. ``dry_run=True`` returns a
    preview without contacting the backend.
  - ``backend.delete_prefix(prefix: str, *, dry_run=True) ->
    DeleteResult`` walks ``list()`` pages and deletes per-page (memory
    bounded for prefixes matching millions of keys; no all-keys-in-RAM
    buffer). **Defaults to dry-run** — caller passes ``dry_run=False``
    to actually delete. The asymmetry vs. ``delete_many``
    (``dry_run=False`` default) is intentional: an explicit list of
    keys is much harder to fat-finger than a prefix that could match
    more than the caller expects. Empty / whitespace prefix raises
    ``ValueError`` rather than matching every object in the bucket.
    Loud INFO log per-page when actually deleting so accidental large
    operations leave a breadcrumb.
  - ``DeleteResult`` exposes ``.total`` and ``.all_succeeded``
    properties for the common partial-failure inspection patterns;
    ``deleted`` and ``errors`` are tuples (truly immutable + hashable
    so a result can land in a cache).
  - ``DeleteError`` mirrors S3's per-key wire shape (``key``,
    ``code``, ``message``).
  - ``adelete_many`` / ``adelete_prefix`` async pairs delegate via
    ``asyncio.to_thread``. Native async lands in Phase 3.
  - ``DeleteError`` and ``DeleteResult`` re-exported via
    ``genblaze_core.__all__``.

- `genblaze-core` / `genblaze-s3`: storage-backend hardening **Phase 2A**
  read primitives — ``head``, ``list``, ``get_range``, ``stream``, plus
  three new value-object types (``ObjectMetadata``, ``FileEntry``,
  ``ListPage``). Closes four of the missing-primitive rows in the
  storage-backend-hardening tranche.
  - ``backend.head(key, *, encryption=None) -> ObjectMetadata | None``
    returns full per-object metadata (size, last_modified, etag,
    content_type, storage_class, user metadata dict). ``None`` for
    missing keys; tolerates 404 AND 403 the same way ``exists`` does
    (scoped application keys legitimately get 403 on non-existent
    reads). ``encryption=`` accepts the same SSE-C envelope as ``get`` —
    closes the head-side asymmetry that completed bug #3.
  - ``backend.list(prefix="", *, max_keys=1000, continuation_token=None)
    -> ListPage`` walks ``ListObjectsV2`` with explicit pagination.
    ``ListPage.entries`` is a ``tuple[FileEntry, ...]`` (truly
    immutable, hashable); ``ListPage.next_token`` is ``None`` once the
    listing is exhausted. ``FileEntry`` is the cheap shape that S3's
    ``ListObjectsV2`` returns natively — no per-key HEAD round-trip
    required to populate it.
  - ``backend.get_range(key, *, offset, length, encryption=None) ->
    bytes`` downloads a byte range via the HTTP ``Range`` header.
    Validates ``offset >= 0`` and ``length >= 0``;
    ``length=0`` short-circuits without contacting the backend. Useful
    for partial-file reads of multi-GB video / long-form audio.
  - ``backend.stream(key, *, chunk_size=8MiB, encryption=None) ->
    Iterator[bytes]`` lazily yields chunks via ``StreamingBody.read``.
    Best-effort ``body.close()`` on iterator exhaustion or
    ``gen.close()`` returns the HTTP connection to the pool.
  - ``backend.ahead`` / ``backend.aget_range`` async pairs delegate
    via ``asyncio.to_thread``. ``alist`` and ``astream`` are
    deliberately omitted from the ABC — threadpool-wrapping a sync
    iterator into an ``AsyncIterator`` either buffers the full result
    (defeating streaming) or spins up a queue per call. Phase 3
    introduces native async via ``aioboto3`` for these specifically.
  - ``StorageBackend`` ABC defaults raise ``NotImplementedError`` for
    the 4 new sync methods — existing third-party subclasses that
    predate Phase 2A keep working at import time and opt in by
    overriding.
- `genblaze-core`: ``ObjectMetadata``, ``FileEntry``, ``ListPage``
  re-exported via ``genblaze_core.__all__`` (lazy, like the other
  storage value objects).

### Fixed
- `genblaze-s3`: ``S3StorageBackend`` preflight no longer permanently
  bricks the backend on a transient upstream failure. Pre-fix, any
  non-redirect ``ClientError`` from ``HeadBucket`` (including 5xx,
  throttle, network blips) was cached as ``_preflight_error`` and
  re-raised on every subsequent call until the process restarted.
  Post-fix, the new ``_preflight_classify.is_sticky_preflight_error``
  helper consults Phase 0's ``RETRYABLE_STORAGE_CODES`` to decide:
  retriable codes (``RATE_LIMIT`` / ``SERVER_ERROR`` / ``NETWORK`` /
  ``TIMEOUT``) re-raise without caching so the next call retries the
  HeadBucket; sticky codes (auth, missing bucket, signature mismatch)
  cache as before. Surfaced during the Phase 1 final review.
- `genblaze-s3`: ``S3StorageBackend.put`` now detects SSE envelope
  conflicts between ``encryption=`` and overlapping ``extra_args``
  keys and raises ``ValueError`` instead of silently encrypting the
  object with whichever side wins on a per-key basis. Mismatched
  envelopes (e.g. ``encryption=Encryption.sse_kms("A")`` plus
  ``extra_args={"SSEKMSKeyId": "B"}``) silently encrypted with the
  wrong material — S3 accepted the request, so callers wouldn't
  notice until they tried to decrypt. The ``ValueError`` is raised
  before the network try/except wrapper so caller API misuse
  propagates with its native exception type rather than being
  masked as ``StorageError``. Surfaced during the Phase 1 final
  review.
- `genblaze-core`: ``StorageBackend.aget_url`` exposes ``policy=`` and
  other backend-specific kwargs to async callers via ``**kwargs``
  forwarding. The async surface now reaches feature parity with the
  sync ``S3StorageBackend.get_url`` — ``await
  backend.aget_url(key, policy=URLPolicy.PUBLIC)`` and the conflict
  detection from Phase 1D both work. Default ``expires_in`` flipped
  from ``3600`` to ``None`` (sentinel meaning "don't pass") so
  backends that distinguish unset-vs-default see "caller didn't
  pass" rather than "caller passed 3600". Surfaced during the
  Phase 1 final review.
- `genblaze-s3`: ``URLPolicy.AUTO`` docstring corrected. Pre-fix, the
  docstring claimed AUTO raises on explicit ``expires_in`` while
  ``public_url_base`` is set; the implementation is actually
  permissive (preserves historic silent-ignore for backward compat).
  Aligned the docstring with the implementation: callers wanting
  strict raise-on-conflict semantics pass
  ``policy=URLPolicy.PUBLIC`` explicitly. Surfaced during the
  Phase 1 final review.
- `genblaze-s3`: ``S3StorageBackend.get_url(expires_in=...)`` no longer
  silently ignored when ``public_url_base`` is configured. The
  historic ``URLPolicy.AUTO`` behavior (default — public when
  ``public_url_base`` set, presigned otherwise) is preserved for
  backward compat, but explicit ``policy=URLPolicy.PUBLIC`` with an
  ``expires_in=`` argument now raises :class:`URLPolicyError` instead
  of returning a never-expiring URL. Closes bug #2 in the
  storage-backend-hardening tranche.
- `genblaze-s3`: ``S3StorageBackend.get(key)`` and
  ``S3StorageBackend.copy(src, dst)`` accept ``encryption=`` and plumb
  the customer-key / KMS-key envelope through to the boto3 call. SSE-C
  uploads now round-trip cleanly — the previous read path silently
  dropped the customer key, so encrypted objects 4xx'd on download.
  Closes bug #3.
- `genblaze-s3`: ``S3StorageBackend.get_url`` no longer issues a
  ``HeadBucket`` on the public-URL path. Public URL rendering is pure
  string concat against ``public_url_base`` and never needed the
  region verify; offline dev / CI flows that just want to compute a
  URL no longer require a reachable bucket. The presigned-URL path
  still verifies (signing endpoint must match the bucket region).
  Closes bug #7.

### Added
- `genblaze-s3`: ``S3StorageBackend.get_url(policy=URLPolicy.AUTO)``
  kwarg. Members: ``AUTO`` (default — preserves today's behavior),
  ``PUBLIC`` (force public, requires ``public_url_base``,
  conflict-with-expires_in raises :class:`URLPolicyError`), and
  ``PRESIGNED`` (force a SigV4 URL even when ``public_url_base`` is
  configured — useful for paid-feed / time-limited fetches off a
  public bucket). Backwards-compatible — every existing caller passing
  no policy or only ``expires_in`` continues to work.
- `genblaze-s3`: ``S3StorageBackend.put(encryption=...)``,
  ``.get(encryption=...)``, ``.copy(encryption=...)`` —
  :class:`Encryption` value object accepted symmetrically across the
  three operations. Replaces the historic ``extra_args={"ServerSide…":
  …}`` escape hatch with typed construction; ``extra_args`` still
  wins on conflict so callers retain raw boto3 control when they
  need it.
- `genblaze-s3`: ``S3StorageBackend.presigned_get(key, *,
  expires_in=3600) -> PresignedURL`` and
  ``S3StorageBackend.presigned_put(key, *, expires_in=3600,
  content_type=None) -> PresignedURL`` — typed presigned-URL methods
  returning the redaction-safe value object from Phase 1A. The URL
  defaults to redacted in ``repr`` / ``str`` / ``f"{...}"``; access
  the unredacted value via the ``.url`` attribute. Use these instead
  of ``get_url(policy=URLPolicy.PRESIGNED)`` when you want default
  redaction in logs. ``presigned_post`` deferred to a later phase
  (different return shape — needs a separate :class:`PresignedPost`
  value object covering both URL and POST-policy form fields).


- `genblaze-core`: ``ObjectStorageSink(prefix="runs", key_strategy=HIERARCHICAL)``
  no longer produces ``runs/runs/{tenant}/{date}/{run_id}/...`` keys.
  The strategy's hardcoded ``runs/`` segment is now collapsed against
  prefixes that already end in ``runs`` via the new ``KeyBuilder``
  primitive — same fix applies to the asset-key path through
  ``AssetTransfer``. Closes bug #5 in the storage-backend-hardening
  tranche. Caller-intentional duplicates within the prefix
  (``"archive/archive"``) or within the strategy segments are
  preserved — the dedupe is seam-only, never global.

### Added
- `genblaze-core`: ``genblaze_core.KeyBuilder`` — pure value-object for
  storage-key construction. Use ``KeyBuilder.from_prefix(s)`` to
  normalize a prefix (strips leading/trailing slashes, collapses
  consecutive separators), ``.append(*segments)`` to extend it
  (returns a new ``KeyBuilder``), and ``.build(*segments)`` for a
  terminal key string. Both ``append`` and ``build`` apply the
  seam-dedupe rule. Frozen ``@dataclass`` for parity with
  :class:`StorageConfig` and :class:`RetryPolicy`. Used internally
  by :class:`ObjectStorageSink` and :class:`AssetTransfer`; exposed
  publicly so downstream backends and custom sinks can reuse the
  same normalization rules.

### Security
- `genblaze-s3`: ``S3StorageBackend.put`` no longer returns a presigned URL.
  The previous return shape leaked the access-key-id
  (``X-Amz-Credential`` query parameter) into anything that persisted the
  value — logs, manifests, DB rows — and broke canonical-hash stability
  for content-addressable layouts because the signature rotates per call.
  ``put`` now returns the storage key (a ``str``); compose with
  :meth:`S3StorageBackend.get_durable_url` for a credential-free,
  persistable URL, or call :meth:`get_url` for an explicit presigned
  URL when one is genuinely needed (Phase 1B forthcoming
  ``presigned_get``/``presigned_put``/``presigned_post`` methods will
  return a redaction-safe :class:`PresignedURL` value object).
  No deprecation shim — every internal caller in the monorepo discards
  ``put``'s return value already (verified across ``ObjectStorageSink``
  and ``AssetTransfer``); the only callers affected are external code
  paths that were persisting the leaked URL, which was itself the
  vulnerability. Closes bug #1 in the storage-backend-hardening
  tranche.

### Changed
- `genblaze-s3`: ``S3StorageBackend.for_backblaze(auto_lifecycle=...)``
  now defaults to ``False`` (was ``True``). Construction no longer
  silently mutates bucket-wide lifecycle configuration. Callers that
  want the historic behavior pass ``auto_lifecycle=True`` explicitly,
  or call :meth:`ensure_lifecycle_defaults` after construction with
  intentional opt-in. Closes bug #4 in the storage-backend-hardening
  tranche.
- `genblaze-s3`: ``for_backblaze`` preflight failures now **raise**
  instead of warning-and-returning a half-broken backend. Placeholder
  credentials surface immediately at construction time rather than
  failing on every subsequent operation with a stale "preflight
  skipped" warning. Use ``preflight=False`` (see ``### Added``) for
  offline tests that legitimately want to defer verification.
- `genblaze-s3`: ``examples/quickstart.py`` and
  ``examples/b2_storage_pipeline.py`` updated to pass
  ``auto_lifecycle=True`` explicitly so the runnable examples preserve
  their historic behavior under the new default.

### Added
- `genblaze-s3`: ``S3StorageBackend.for_backblaze(preflight=...)`` —
  set ``preflight=False`` to skip the construction-time HeadBucket
  call. Useful for offline tests with placeholder credentials. Cannot
  be combined with ``auto_lifecycle=True`` (lifecycle requires a
  verified region) — passing both raises ``ValueError``. Closes the
  ``preflight=False`` half of bug #4.

- `genblaze-s3`: storage-backend hardening Phase 1A — additive value
  objects for the upcoming P0 bug fixes. None are wired into backend
  methods yet (Phases 1B/1D); shipping the public API first lets
  callers build on top while we land behavior changes behind deprecation
  shims. Three new symbols, one back-compatible kwarg alias.
  - `genblaze_s3.URLPolicy` (`StrEnum` — `AUTO` / `PUBLIC` /
    `PRESIGNED`) and `genblaze_s3.URLPolicyError`. Replaces the silent
    `expires_in`-vs-`public_url_base` precedence in `get_url` (bug #2).
    `AUTO` matches today's behavior; `PUBLIC` and `PRESIGNED` force the
    flavor. Wiring lands in Phase 1D — Phase 1A is the value-object
    foundation only.
  - `genblaze_s3.Encryption` — symmetric SSE config accepted by
    `put`/`get`/`copy`/`head` (bug #3). Frozen dataclass with three
    construction modes: `Encryption.sse_s3()`, `Encryption.sse_kms(key_id)`,
    `Encryption.sse_c(key_bytes)` (auto-computes key MD5). Each mode
    serializes to the right boto3 wire shape via `to_put_extra_args` /
    `to_get_extra_args` / `to_head_extra_args` / `to_copy_extra_args`.
    SSE-C customer keys are redacted in `__repr__` AND `__str__` —
    only direct attribute access reveals the raw bytes. Wiring lands
    in Phase 1D.
  - `genblaze_s3.PresignedURL` — credential-bearing URL with
    redaction-safe formatting. `__repr__` and `__str__` strip
    `X-Amz-Signature` / `X-Amz-Credential` / `X-Amz-Security-Token`
    (and legacy SigV2 equivalents) so `f"...{url}..."` log lines no
    longer leak transient credentials. The `.url` attribute returns
    the unredacted form for handing to HTTP clients — every leak
    site becomes a conscious decision rather than a default
    string-interpolation accident. Foundation for the Phase 1B
    `presigned_get`/`presigned_put`/`presigned_post` methods.
  - `genblaze_s3.S3StorageBackend` accepts `access_key_id` and
    `secret_access_key` as kwarg aliases of `aws_access_key_id` and
    `aws_secret_access_key` (bug #10). The README quickstart already
    used the short form; both names now work. Passing both names for
    the same credential raises `TypeError` — no silent precedence.

- `genblaze-core`: storage-backend hardening Phase 0 foundation modules.
  Additive only — no behavior change for end-users; downstream phases land
  the P0 bug fixes and missing primitives (range/stream/list/head/etc.) on
  top of these. Net: 6 new symbols, 7 inherited async method pairs on every
  `StorageBackend` subclass.
  - `genblaze_core.StorageConfig` — frozen `@dataclass(frozen=True, slots=True)`
    with 8 tunable knobs (`max_pool_connections`, `connect_timeout_sec`,
    `read_timeout_sec`, `multipart_threshold`, `multipart_chunk_size`,
    `retries`, `user_agent_extra`, `signing_addressing_style`). Defaults
    preserve the historic `S3StorageBackend` values; `StorageConfig()` is a
    no-op upgrade. Mirrors the `RetryPolicy` precedent from
    `genblaze_core.providers.retry`.
  - `genblaze_core.StorageErrorCode` — typed enum (`NOT_FOUND`,
    `ACCESS_DENIED`, `AUTH_FAILURE`, `REGION_REDIRECT`, `RATE_LIMIT`,
    `SERVER_ERROR`, `NETWORK`, `TIMEOUT`, `INVALID_INPUT`,
    `ENCRYPTION_REQUIRED`, `OBJECT_LOCKED`, `UNKNOWN`) and
    `RETRYABLE_STORAGE_CODES` frozenset. Mirrors `ProviderErrorCode` shape
    so retry classification and observability tooling share vocabulary
    across the provider and storage subsystems.
  - `genblaze_core.classify_botocore_error(exc, *, operation, key=None)` —
    maps a `botocore.ClientError` (or any boto exception) to a populated
    `StorageError` with `error_code`, `request_id`, `status_code`,
    `retry_after`, `is_retriable`, and `operation` set. Lazy-imports
    `botocore` so core stays minimal-install clean.
  - `genblaze_core.exceptions.StorageError.__init__` extended with optional
    keyword args `error_code`, `request_id`, `status_code`, `retry_after`,
    `is_retriable`, `operation` — full parity with `ProviderError`.
    Backwards-compatible: every existing `StorageError(message)` call site
    in the 11 connectors works unchanged.
  - `genblaze_core.storage.base.StorageBackend` — async pairs for all 6
    existing sync abstract methods (`aput`, `aget`, `aexists`, `adelete`,
    `aget_url`, `aget_durable_url`) plus `acopy` for the concrete `copy`
    method. Default impls delegate via `asyncio.to_thread`; backends with
    native async (e.g. `aioboto3`) override directly. Streaming primitives
    (`alist`, `astream`) are deliberately deferred to a later phase — sync
    iterators threadpool-wrapped into async iterators lie about
    back-pressure and buffer the entire result.
  - Internal: `genblaze_core.storage._tracer.traced(op_name)` decorator —
    OTel-instrumentation primitive for backend methods. Returns the wrapped
    function unchanged when `opentelemetry` isn't installed (zero-overhead
    no-op). Not yet wired to the S3 backend (Phase 1+).

## [0.2.8] - 2026-04-28

### Released package versions
- Code-change bump: `genblaze-core` 0.2.7.
- Untouched (no republish): every other Python and npm package.

### Added
- `genblaze-core`: `Pipeline.step(external_inputs=[Asset, ...])` — caller-held
  Assets seed `Step.inputs` directly, without going through `input_from=` (which
  only references prior pipeline steps) or `chain=True`. Closes the gap that
  blocked multimodal first-step calls into `NvidiaChatProvider` and any future
  chat / vision-analysis / image-edit step that needs a user-uploaded asset on
  step 0. Mutually exclusive with `input_from=`. Provider must declare
  `accepts_chain_input=True` in its `ProviderCapabilities` (the existing flag
  covers all three input mechanisms; docstring updated). Pass an `Asset` with
  `sha256` populated for stable cache keys and manifest canonical hashes —
  `WARNING` is logged otherwise. Defensive copy of the input list is taken at
  construction so post-construction caller mutation doesn't bleed into the
  deferred step. Reserved kwargs `inputs=` / `input=` raise a friendly
  `GenblazeError` pointing at the right name (prevents silent `**params`
  swallow). `Pipeline.to_template()` raises when any step uses
  `external_inputs=` (templates describe pipeline shape, not runtime Asset
  payloads). Stable additive API; Wave 4's planned `Pipeline.input(asset_or_path)`
  will be sugar over this primitive that adds local-path acceptance via
  `LocalFilesystemSink` — both APIs will coexist.

## [0.2.7] - 2026-04-24

### Released package versions
- `genblaze-core` 0.2.6, `genblaze-gmicloud` 0.2.6, `genblaze-google` 0.2.4,
  `genblaze-openai` 0.2.4, `genblaze-nvidia` 0.2.1, `genblaze-s3` 0.2.4,
  `genblaze-runway` 0.2.3, `genblaze-luma` 0.2.3.
- `@genblaze/spec` (npm) 0.3.3 — adds `step-queued` event schema +
  queued/heartbeat/ETA/request_id fields on existing event schemas.
- Untouched (no republish): `genblaze-replicate` 0.2.2, `genblaze-decart`
  0.2.2, `genblaze-elevenlabs` 0.2.2, `genblaze-lmnt` 0.2.2,
  `genblaze-stability-audio` 0.2.2, `genblaze-langsmith` 0.2.1,
  `genblaze-cli` 0.2.0, `genblaze` (umbrella) 0.3.1.

### Fixed
- `genblaze-core`: `StepRetriedEvent` now actually reaches `Pipeline.stream()`
  / `astream()` consumers. The schema shipped in `@genblaze/spec` 0.3.2 but
  the `_install_progress_tracer` composite only wrapped `on_progress`, so
  `BaseProvider._emit_retry` events fired into a void — UIs subscribed to
  the stream saw silence between attempt 1 and attempt N. Adds
  `QueueEmitter.on_retry`, extends the tracer-install composite to wrap
  `on_retry` with the same user-callback-then-emit pattern as `on_progress`,
  and exposes `on_retry=` as a kwarg on `Pipeline.run()` / `arun()`.

### Added
- `genblaze-core`: typed multimodal `ChatMessage.content` — accepts
  structured content parts (text + image) instead of plain strings, plus a
  `response_format` helper for JSON-mode / structured-output across
  providers. The four chat-bearing connectors (`gmicloud`, `google`,
  `openai`, `nvidia`) all wire to the new shape.
- `genblaze-nvidia`: new `chat_provider.py` (272 lines) and `models/chat.py`
  surface the multimodal contract end-to-end. Includes 269 lines of new
  test coverage.
- `genblaze-core`: streaming events gain `queued` lifecycle state,
  heartbeats, and ETA propagation. `BaseProvider` plumbing emits these from
  any connector that overrides the new hooks; `Runway` and `Luma`
  connectors implement the emission code in this release. Spec gains
  `events/v1/step-queued.schema.json` and adds the relevant fields on
  step-started / step-progress / step-completed / step-failed.
- `genblaze-s3`: `request_id` / upstream prediction tracking on the storage
  backend (38 lines + 74 lines of tests) for end-to-end traceability from
  manifest → upstream provider request.
- `genblaze-core`: every media handler updated (jpeg, png, mp3, wav, webp,
  mp4, flac, aac, embedder, sidecar) plus comprehensive new test coverage
  for the media pipeline.

### Added
- `genblaze-core`: `BaseProvider.poll_progress(prediction_id)` hook —
  connectors return mid-poll signals (`preview_url`, `progress_pct`,
  `message`) that the base poll loop merges into the next `step.progress`
  event. Default `None`; backwards compatible. Closes the long-standing
  spec/implementation gap where `StepProgressEvent.preview_url` shipped
  on the wire but no connector populated it.
- `genblaze-runway`: surfaces `task.progress` and `task.thumbnail_url` /
  `task.preview_url` via `poll_progress()` so dashboards see live
  intermediate frames during Gen-4 video generation.
- `genblaze-luma`: surfaces intermediate Dream Machine preview frames
  (`assets.preview` / `assets.image` / `assets.thumbnail`) and the
  current `state` string via `poll_progress()`.
- `genblaze-core`: new `StepQueuedEvent` (`type="step.queued"`) — additive
  signal for steps waiting on capacity. Sequential pipelines emit it for
  every upcoming step at run start (`reason="serial"`); concurrent
  pipelines with `max_concurrency` emit it when a coroutine finds the
  semaphore locked at entry (`reason="concurrency_limit"`). `step.started`
  semantics are unchanged — consumers that don't care about queued state
  ignore the event type. `Pipeline._build_step` now accepts an optional
  `step_id=` so queued and started events reference the same UUID. New
  schema, parent stream-event union extended, TypeScript types
  regenerated.
- `genblaze-core`: heartbeat ticks during long polls. When the adaptive
  poll interval grows past 15s, `BaseProvider` splits the sleep into 10s
  chunks and emits an `is_heartbeat=True` `step.progress` event between
  chunks so SSE proxies, load balancers, and impatient users see the
  connection is alive. New `is_heartbeat: bool` field on `StepProgressEvent`
  and `ProgressEvent`. `Pipeline.stream(heartbeats=False)` /
  `astream(heartbeats=False)` drops keepalive ticks at the emitter for
  high-volume deployments where the overhead outweighs the benefit.
  Schema updated; TypeScript types regenerated.
- `genblaze-core`: `Pipeline.step(expected_duration_sec=...)` and matching
  `expected_duration_sec` field on `StepStartedEvent`. Caller-supplied ETA
  hint surfaces on the wire so progress UIs can render meaningful bars
  without hard-coding per-model knowledge. SDK does not synthesize the
  value — apps own accuracy. Schema updated; TypeScript types regenerated.
- `genblaze-core`: `request_id` field on `StepProgressEvent`, `StepCompletedEvent`,
  and `StepFailedEvent` carries the upstream provider's prediction/job id
  (e.g. Replicate prediction id, Runway task id) on the wire. `BaseProvider`
  stashes the id in `step.metadata["upstream_id"]` as soon as `submit()`
  returns, then `_fire_progress` and the streaming-event translators thread
  it through. Lets dashboards render live debug links ("view in
  Replicate") without waiting for step completion. Available from any
  progress tick fired after submit; pre-submit ticks carry `null` (id
  doesn't exist yet). Schemas updated; TypeScript types regenerated.
- `genblaze-core`: `RetryPolicy` frozen dataclass in `genblaze_core.providers.retry`
  with seven knobs (`max_attempts`, `initial_backoff_sec`, `max_backoff_sec`,
  `backoff_multiplier`, `jitter`, `respect_retry_after`, `retryable_codes`,
  `idempotency_key_strategy`) and three preset classmethods
  (`conservative()`, `aggressive()`, `disabled()`). Pass to any provider via
  `Provider(retry_policy=...)` to tune transient-retry behavior per instance.
  Defaults reproduce the historical `BaseProvider.poll_transient_retries=5`
  behavior so existing code (including instance-level
  `provider.poll_transient_retries = N` mutations) keeps working unchanged.
  **Closes the gap left by the [0.2.5] release notes** — that version's
  changelog promised a `RetryPolicy` users could override per-provider, but
  the class itself wasn't shipped. This is the actual implementation.
- `genblaze-core`: `BaseProvider.IDEMPOTENCY_HEADER_NAME` opt-in class
  attribute and `_inject_idempotency_header(headers, step)` helper. When a
  provider sets the header name, retried submits carry a stable
  idempotency key (default: `step.step_id`) so the upstream can dedupe.
  Per-provider header opt-ins are individual follow-up PRs.
- `genblaze-core`: cross-provider conformance test
  (`tests/conformance/test_provider_contract.py::test_accepts_retry_policy_kwarg`)
  asserts every `BaseProvider` subclass forwards `retry_policy=` to
  `super().__init__()` — same shape as the existing `models=` test.
- `genblaze-core`: `EmbedResult.embed_error` — populated when `SmartEmbedder` falls
  back from inline embed to sidecar so callers can see *why* the inline path
  failed (previously logged at WARNING and silently swallowed).
- `genblaze-core`: `WebpHandler.embed(lossless=None)` (default) auto-detects VP8L
  source codec and preserves losslessness. Pass `lossless=True/False` to override.
- `genblaze-core`: `media.atomic_write(path)` context manager — single
  source of truth for atomic temp-file + rename writes, replacing eight
  copies of the same boilerplate across handlers.
- `genblaze-core`: `media.sniff_mime(path)` — magic-byte MIME detection
  (PNG, JPEG, WebP, MP4, MP3, WAV, FLAC). `guess_mime()` now prefers
  content over extension so a misnamed file (`image.png` containing JPEG
  bytes) dispatches to the correct handler instead of failing inside the
  wrong one.
- Docs: `docs/features/retry-policy.md` — when to override the default,
  preset chooser, idempotency-key rollout status table, migration from
  `poll_transient_retries`.
- Docs: `docs/features/trust-modes.md` — three-mode trust model (integrity / signed
  / C2PA), threat model table, asset-binding caveat.
- Docs: `docs/features/manifest-provenance.md` — explicit hash-payload-vs-canonical-JSON
  documentation for third-party verifiers.
- `genblaze-core`: `ObjectStorageSink.manifest_key_for(run)` /
  `manifest_url_for(run)` / `read_manifest(run, *, verify=True)` — public
  helpers so app code can locate, link to, and re-fetch a stored manifest
  without re-implementing `_build_manifest_key` or parsing
  `manifest.manifest_uri`. `read_manifest` enforces the `MAX_MANIFEST_BYTES`
  cap and verifies the canonical hash by default.
- `genblaze-core`: `StorageBackend.key_from_url(url)` — inverse of
  `get_durable_url`. Default raises `NotImplementedError` so backends opt
  in explicitly; the S3 implementation handles both raw-endpoint and
  `public_url_base` URL shapes and returns `None` for foreign URLs
  (different host, different bucket, malformed) so callers can route
  across backends without `try/except`.

### Changed
- All 11 provider connectors (`genblaze-openai`, `genblaze-google`,
  `genblaze-runway`, `genblaze-luma`, `genblaze-decart`, `genblaze-replicate`,
  `genblaze-elevenlabs`, `genblaze-stability-audio`, `genblaze-lmnt`,
  `genblaze-gmicloud`, `genblaze-nvidia`) now accept and forward
  `retry_policy=` to `super().__init__()`. Backwards-compatible — providers
  constructed without the kwarg behave identically to prior releases.

### Corrected
- The [0.2.5] release-notes entry "exposes a `RetryPolicy` the caller can
  override per-provider" was aspirational — only utility functions and
  constants shipped in that release. The class itself ships in this release
  (see above). No code change is required for callers who only used the
  default behavior; callers who tried to import `RetryPolicy` from
  `genblaze_core.providers` in 0.2.5 will now find it.

### Fixed
- `genblaze-core`: `ObjectStorageSink.write_run` now populates
  `manifest.manifest_uri` even when the manifest object already exists in
  the backend. Previously the assignment was inside the `if not exists:`
  branch, so retries (or any second `write_run` call against the same
  run) left the in-memory `Manifest.manifest_uri` as `None`, breaking
  pointer-mode embedders downstream. The durable URL is now set
  unconditionally whenever the manifest is present in storage.
- `genblaze-core`: `StepRetriedEvent` now reaches `Pipeline.stream()` /
  `astream()` consumers. `_install_progress_tracer` previously wrapped only
  `on_progress`, so retry events fired by `BaseProvider._emit_retry` never
  propagated to the queue. UIs subscribed to the stream now see retry
  attempts in real time as advertised by the 0.3.2 schema. `Pipeline.run()`
  / `arun()` also gain an `on_retry=` kwarg for synchronous side-effects
  (metrics, logs); the same event continues to flow through the stream.
- `genblaze-core`: JPEG, WebP, MP3, FLAC, and AAC inline embed are now atomic
  (temp file + `os.replace`). A crash mid-save no longer corrupts the source
  file in any handler.
- `genblaze-core`: `WavHandler` explicitly rejects RF64 / BW64 / RIFX variants
  with a clear `EmbeddingError` instead of silently misparsing them as RIFF.
- `genblaze-core`: lossless WebP sources are no longer silently re-encoded as
  lossy when the caller doesn't pass `lossless=True`. VP8X containers with
  leading metadata chunks (ICCP, EXIF, XMP) are now correctly detected by
  walking RIFF chunks instead of peeking at a fixed offset.
- `genblaze-core`: PNG embed now patches chunks directly instead of
  re-encoding through Pillow. Ancillary chunks (`pHYs`, `gAMA`, `cHRM`,
  `sRGB`, `bKGD`, `tIME`, `iCCP`, plus private/custom chunks) survive
  embed → extract verbatim. Also avoids decoding pixel data, faster for
  large images.
- `genblaze-core`: JPEG/WebP extract walks every XMP packet in the file
  instead of returning on the first one. Files with leading non-genblaze
  XMP (Photoshop, Lightroom) now correctly surface a later genblaze
  packet rather than failing with a misleading "no manifest" error.
- `genblaze-core`: FLAC and AAC extract apply `MAX_MANIFEST_BYTES` cap
  before parsing to prevent OOM on hostile metadata payloads, matching
  the existing guard in WAV/MP4.
- `genblaze-core`: `MAX_FILE_BYTES` (500 MB) size cap now applies to PNG
  inputs (was bypassed because PNG handler used Pillow's lazy loader).

### Performance
- `genblaze-core`: `SmartEmbedder.embed()` skips the JSON serialize → parse →
  pydantic-revalidate round-trip when applying a no-redaction policy. Hot-path
  win for large manifests.
- `genblaze-core`: PNG embed bypasses Pillow's pixel decode/encode cycle
  via direct chunk-level patching — typically 5-50× faster for non-trivial
  images depending on size.

### Refactor
- `genblaze-core`: pointer-mode embed in `SmartEmbedder` now delegates to
  `SidecarHandler.embed(policy=...)` rather than reimplementing the
  atomic write inline.
- `genblaze-core`: removed `Mp4Handler._atomic_write_bytes` (subsumed by
  the shared `atomic_write` context manager).

### Docs
- README: rewrote the provenance bullet to drop "no bolt-on signing" framing and
  link the new trust-modes page. Manifests are described as "SHA-256-bound" and
  "tamper-evident in trusted storage" — honest about what the integrity hash
  proves and where signing/C2PA fit on the roadmap.

## [0.2.6] - 2026-04-24

### Released package versions
- Code-change bumps: `genblaze-core` 0.2.5, `genblaze-gmicloud` 0.2.5.
- Untouched (no republish): every other Python and npm package.

### Added
- `genblaze-core`: `providers/probe.py` — model-probe contract for runtime
  capability discovery against a live provider, plus a conformance test
  suite (`tests/conformance/test_provider_contract.py`) that every provider
  adapter must pass.
- `genblaze-core`: `providers/params.py` — shared parameter-standardization
  hooks (canonical → native rewriting at the provider boundary).
- `genblaze-core`: `models/voice.py` — first-class `Voice` model for TTS
  providers.
- `genblaze-core`: `Pipeline` gains `batch_items` / `batch_raise` /
  `estimated_cost` / `raise_on_failure` — execution controls covered by
  four new dedicated test files.
- `genblaze-core`: `exceptions.py` expanded with new typed errors aligned to
  the probe + standardization contracts.
- `genblaze-gmicloud`: model registries fully reconciled with live GMICloud
  catalog — image/audio/video spec rewrites, new `models/voices.py` (172
  lines) for the audio voice catalog, standardization-hook tests.
- Tooling: `tools/probe_models.py` and `tools/gen_model_matrix.py` for
  generating the model-status matrix from live API probes (not part of any
  published package; repo-only utilities).
- Docs: `docs/reference/model-matrix.md`, `docs/reference/model-probe-status.json`.

## [0.2.5.post1] - 2026-04-24

### Fixed
- `genblaze` 0.3.1: corrects the `genblaze-nvidia` pin in the `nvidia` extra
  and the `video`/`image`/`audio`/`all` bundles. 0.3.0 pinned
  `genblaze-nvidia>=0.1.0,<0.2` but nvidia actually shipped at 0.2.0, which
  made `pip install "genblaze[nvidia]"` unresolvable. Widened to
  `>=0.2.0,<0.3`.

## [0.2.5] - 2026-04-24

### Released package versions
- **New on PyPI (first publish):** `genblaze-nvidia` 0.2.0 (NIM /
  build.nvidia.com adapters for video, image, audio, chat — aligned with
  release train), `genblaze-cli` 0.2.0 (`genblaze` CLI script for
  `extract` / `verify` / `replay` / `index`).
- **New minor on PyPI:** `genblaze` (umbrella) 0.3.0 — now ships real code
  that lazily re-exports `genblaze_core`'s public API, so
  `from genblaze import Pipeline` works. `genblaze_core` stays the canonical
  import path in docs. Also exposes the `nvidia` extra and adds NVIDIA to the
  `video` / `image` / `audio` / `all` curated bundles.
- Code-change bumps from retry-policy unification: `genblaze-core` 0.2.4,
  `genblaze-gmicloud` 0.2.4, `genblaze-google` 0.2.3, `genblaze-openai` 0.2.3,
  `genblaze-replicate` 0.2.2, `genblaze-runway` 0.2.2, `genblaze-decart` 0.2.2,
  `genblaze-elevenlabs` 0.2.2, `genblaze-lmnt` 0.2.2, `genblaze-luma` 0.2.2,
  `genblaze-stability-audio` 0.2.2.
- `@genblaze/spec` (npm) 0.3.2 — adds `events/v1/step-retried.schema.json`
  and updates the `stream-event` union for the new event.
- Untouched (no republish): `genblaze-s3` 0.2.3, `genblaze-langsmith` 0.2.1.

### Added
- `genblaze-core`: `genblaze_core.providers.retry` — unified retry policy
  module. Every provider adapter now delegates transient-error classification
  + exponential backoff to this shared surface instead of rolling its own.
  Consistent behavior across GMICloud, OpenAI, Google, Replicate, Runway,
  Luma, Decart, ElevenLabs, LMNT, Stability Audio, NVIDIA. Exposes a
  `RetryPolicy` the caller can override per-provider.
- `genblaze-core`: `BaseProvider` retry plumbing baked into `invoke` /
  `ainvoke` / `resume` / `aresume`. Also surfaces retry attempts as new
  `StepRetried` stream events for observability.
- `genblaze-nvidia` (new package): video (Cosmos), image (Flux, SDXL, etc.),
  audio (TTS, Parakeet ASR), and chat (OpenAI-compatible NIM endpoints)
  providers for NVIDIA's NIM / build.nvidia.com platform. Ships with 1146
  lines of tests.
- `genblaze-cli` (first public publish): the `genblaze` command-line tool
  with `extract`, `verify`, `replay`, and `index` subcommands for manifest
  operations. Script entry point: `genblaze`.
- `genblaze` (umbrella) 0.3.0: now ships a real Python package (was an empty
  metapackage). Re-exports the top-level public surface of `genblaze_core`
  lazily, so `from genblaze import Pipeline` works after
  `pip install genblaze`. `genblaze_core` remains the canonical import path
  used in docs and examples. Submodules (`genblaze_core.media`,
  `genblaze_core.canonical`) and provider adapters (`genblaze_openai`,
  `genblaze_google`, …) are not re-exported — import those from their own
  packages. Ships `py.typed` for static type-checker support.
- `@genblaze/spec`: new `events/v1/step-retried.schema.json` schema + TS type
  for the retry event.

## [0.2.4] - 2026-04-24

### Released package versions
- Code-change bumps: `genblaze-core` 0.2.3, `genblaze-gmicloud` 0.2.3,
  `genblaze-google` 0.2.2, `genblaze-openai` 0.2.2.
- `@genblaze/spec` (npm) 0.3.1 — minor schema + TS-type touch-up.
- Untouched since last wave (no republish): `genblaze-s3` 0.2.3,
  `genblaze-replicate` 0.2.1, `genblaze-decart` 0.2.1, `genblaze-elevenlabs`
  0.2.1, `genblaze-langsmith` 0.2.1, `genblaze-lmnt` 0.2.1, `genblaze-luma`
  0.2.1, `genblaze-runway` 0.2.1, `genblaze-stability-audio` 0.2.1,
  `genblaze` (umbrella) 0.2.3 — its dep ranges already satisfy the new
  versions.

### Added
- `genblaze-gmicloud`: HTTP client injection in `GMICloudBase` — providers
  accept an optional `client=` for deterministic tests and custom transport
  (proxies, retries, observability). New `test_base_client_injection.py`
  covers the contract. Part of the gmi-hardening exec plan.
- `genblaze-google`: `_errors.py` module with explicit `ProviderErrorCode`
  mapping for Gemini SDK exceptions. New `test_error_mapping.py`.
- `genblaze-openai`: `test_chat.py` covers the `chat()` / `achat()` wrapper
  paths end-to-end.
- `genblaze-core`: new `Modality` enum member.

### Fixed
- `genblaze-core`: OTel tracer event-type check no longer misclassifies a
  subset of events; poll-cache cleanup test hardened.
- `genblaze-gmicloud`: media URL extraction handles a previously-missed
  envelope shape; chat client initialization condition corrected.

### Added
- `genblaze-core`: `ChatMessage`, `ToolCall`, `ChatResponse` in
  `genblaze_core.models.chat` — uniform return shape for the standalone
  chat wrappers below. Not part of the manifest wire protocol.
- `genblaze-openai`, `genblaze-google`, `genblaze-gmicloud`: standalone
  `chat()` / `achat()` wrappers around each provider's chat / completion
  endpoint. Sit outside the Pipeline / Step machinery — convenience for
  callers driving media steps from an LLM. See
  `docs/features/llm-calls.md`.
- `genblaze-core`: `ProviderErrorCode.CONTENT_POLICY` — new normalized
  code for safety / content-policy refusals. Deterministic, never
  retryable. Wire schema (`step.schema.json`) and TS types regenerated.
  Shared `classify_api_error()` detects the common keywords; per-provider
  mappers (Google, GMICloud) prioritize policy detection over status-code
  classification so a 400 policy refusal isn't misclassified as
  `INVALID_INPUT`.
- `genblaze-gmicloud`: `base_url=` ctor kwarg (+ `GMI_BASE_URL` env) and
  `http_client=` kwarg on all three provider classes. Enables staging /
  proxy / VPC deployments and lets multi-modality pipelines share one
  `httpx.Client` across video / image / audio providers. Externally
  supplied clients are never closed by `close()`.
- Root README: provider × modality capability matrix now includes a
  "Chat (LLM)" column — single-page answer to "which connector does
  what?".

### Fixed
- `genblaze-gmicloud`: `GMICloudImageProvider.fetch_output` now emits one
  `Asset` per URL in the `media_urls` envelope. Previously discarded all
  but the first when `number_of_images > 1`, silently returning one asset
  for an N-asset bill. The new `extract_media_urls()` helper exposes the
  full list; `extract_media_url()` remains as a single-output thin
  wrapper for the video and audio paths.

### Changed
- `genblaze-core`: `ModelRegistry` now logs dropped non-allowlisted
  params at `INFO` (was `DEBUG`). Silent allowlist drops now surface in
  typical production logs without WARNING-level noise.
- `genblaze-gmicloud` README: removed the SDK email/password auth claim
  (only API-key auth is implemented). Added a naming-reference table,
  the `base_url` / `http_client` injection pattern, LLM-access surface,
  and the canonical status-check idiom for reading `step.assets` safely.
- `GMICloudAudioProvider` docstring now documents that audio input is a
  reference voice for cloning, not a source for speech-to-text. STT is
  out of scope for this class.

## [0.2.3] - 2026-04-23

### Released package versions
- **New:** `genblaze` 0.2.3 — umbrella metapackage. `pip install genblaze`
  installs `genblaze-core` + `genblaze-s3` by default; provider adapters are
  opt-in extras (e.g. `pip install "genblaze[gmicloud,video]"`). Curated
  bundles: `[video]`, `[image]`, `[audio]`, `[all]`.
- Code-change bumps: `genblaze-core` 0.2.2, `genblaze-replicate` 0.2.1,
  `genblaze-s3` 0.2.3.
- Metadata-only force-bumps (author + Homepage fill-in, no code changes):
  `genblaze-gmicloud` 0.2.2, `genblaze-openai` 0.2.1, `genblaze-google` 0.2.1,
  `genblaze-decart` 0.2.1, `genblaze-elevenlabs` 0.2.1, `genblaze-langsmith`
  0.2.1, `genblaze-lmnt` 0.2.1, `genblaze-luma` 0.2.1, `genblaze-runway` 0.2.1,
  `genblaze-stability-audio` 0.2.1.
- `@genblaze/spec` (npm) 0.3.0 — minor bump for new events schema namespace.

### Added
- `genblaze` metapackage for discoverable `pip install genblaze` UX.
- Every published Python package now has `authors` and `Homepage` URL
  populated, so `pip show` and the PyPI project page render correctly.
- Root README: "Install" section with package-to-import mapping table
  (resolves hyphen/underscore confusion for new users).

### Added
- `genblaze-core`: `genblaze_core.pipeline` package now uses PEP 562
  module-level `__getattr__` for lazy attribute resolution (`Pipeline`,
  `StepCache`, `PipelineResult`, `StepCompleteEvent`). `from
  genblaze_core.pipeline import Pipeline` still works; loading the heavy
  `pipeline.py` module is deferred until first access. Lets
  `observability.events` import from `pipeline.result` without a
  circular import through `pipeline.py`.
- `libs/spec/ts/genblaze.d.ts` — TypeScript type declarations generated
  from the JSON Schemas. Eliminates hand-rolled type drift in downstream
  TS consumers (studio UIs, Node backends). Regenerate via
  `make ts-types`. Phase 1a: committed in-repo; phase 1b will publish
  `@genblaze/spec` to npm. See `libs/spec/README.md`.
- `libs/core/tests/unit/test_spec_conformance.py` — bidirectional
  conformance tests between Pydantic models and `libs/spec/schemas/`.
  Catches field-set drift, enum drift, missing descriptions. Runs under
  `make test`.
- CI `ts-types` job — drift guard that regenerates the TS types and
  fails if the committed file would change.
- `genblaze-core`: `StreamEvent` is now a Pydantic discriminated union —
  ten per-variant classes (`PipelineStartedEvent`, `PipelineCompletedEvent`,
  `PipelineFailedEvent`, `StepStartedEvent`, `StepProgressEvent`,
  `StepCompletedEvent`, `StepFailedEvent`, `AgentIterationStartedEvent`,
  `AgentIterationEvaluatedEvent`, `AgentCompletedEvent`) under a common
  `StreamEvent` base. `AnyStreamEvent` + `StreamEventAdapter` (a
  `TypeAdapter`) parse inbound event dicts into the correct variant via
  the `type` discriminator.
- `libs/spec/schemas/events/v1/*.schema.json` — Draft 2020-12 JSON Schemas
  for all ten variants plus a parent `stream-event.schema.json` with
  `oneOf` + `discriminator`. Generated TypeScript types now include the
  full discriminated-union `StreamEvent` surface.
- Conformance coverage extended to event variants (field-set parity,
  description required, `additionalProperties: false`, `type` const
  matches Pydantic `Literal`, round-trip validation, discriminator
  completeness).

### Changed
- `libs/spec/schemas/manifest/v1/policy.schema.json`:
  `prompt_visibility` enum now includes `"encrypted"` to match the
  Pydantic `PromptVisibility` enum. Schema-only fix; Pydantic
  acceptance was already unchanged.
- `Asset.url` field description now documents the durable-URL-is-the-
  handle invariant (no separate storage-key field; parse key from URL
  if needed) so the contract flows into JSON Schema and generated
  TypeScript JSDoc.
- **Breaking (pre-1.0)**: `StreamEvent(type=..., ...)` with a raw
  discriminator string no longer validates — construct the specific
  variant class (`StepStartedEvent(...)`, etc.). `isinstance(ev,
  StreamEvent)` still narrows all variants; per-variant narrowing via
  `isinstance(ev, StepFailedEvent)` or `ev.type == "step.failed"` is now
  supported and produces precise field types under pyright/mypy. Agent
  events flatten their former `data` dict into proper fields — e.g.
  `event.data["iteration"]` is now `event.iteration`.
- **Breaking wire format for `step.failed`**: the serialized event no
  longer carries a `message` key — the failure reason lives on a
  dedicated `error` field. Previously the failure string was duplicated
  into both `message` and `error`. Webhook / log / SSE consumers that
  key on `message` for failed steps should switch to `error`.
- `StreamEvent.to_dict()` now delegates to `model_dump(mode="json",
  exclude_none=True)` and always emits `type`+`timestamp` plus the
  variant's declared fields. In-process-only fields (`step`, `result`)
  remain on the Python object but are excluded from the wire shape;
  derived `step_status`/`manifest_hash`/`run_status`/`error` fields are
  pre-populated at construction so consumers don't lose context.
- `StepCompletedEvent.step` / `StepFailedEvent.step` are now typed as
  `Step | None` (was `Any`); `PipelineCompletedEvent.result`,
  `PipelineFailedEvent.result`, `AgentIterationEvaluatedEvent.result`,
  and `AgentCompletedEvent.result` are typed as `PipelineResult | None`.
  Pydantic passes instances through by identity
  (`revalidate_instances="never"`) — no copy, no perf regression. IDE
  autocomplete and static type checkers now narrow `event.step.assets`
  and `event.result.manifest` correctly.

## [0.2.2] - 2026-04-23

### Released package versions
- `genblaze-core` 0.2.1, `genblaze-gmicloud` 0.2.1, `genblaze-s3` 0.2.2.
- First-time PyPI releases at 0.2.0: `genblaze-decart`, `genblaze-elevenlabs`,
  `genblaze-langsmith`, `genblaze-lmnt`, `genblaze-luma`, `genblaze-replicate`,
  `genblaze-runway`, `genblaze-stability-audio`. All pin
  `genblaze-core>=0.2.0,<0.3`.
- `genblaze-openai` and `genblaze-google` remain at 0.2.0 — no code changes
  since the 0.2.0 release.

### Added
- `genblaze-core`: `ModelSpec.deprecated_aliases` — old model ids keep resolving
  but emit a `DeprecationWarning` pointing to the canonical slug. Drop the
  alias after one minor version.
- `genblaze-core`: `ModelRegistry.resolve_canonical(model_id)` — returns the
  canonical slug the upstream API expects (or passes caller input through when
  only the fallback spec matches). Use in connectors with case-sensitive
  upstream APIs instead of poking at `spec.model_id`.
- `genblaze-gmicloud`: `extract_media_url()` envelope parser covering both the
  live `outcome.media_urls[0].url` shape and the legacy flat `*_url` keys.
  Image modality also falls back to `outcome.thumbnail_image_url`.
- `genblaze-gmicloud`: legacy-slug and error-unwrap test coverage.

### Changed
- `genblaze-gmicloud`: all image and video model ids rewritten to the live
  lowercase slugs the request-queue API actually accepts (`seedream-5.0-lite`,
  `veo3`, `wan2.6-i2v`, `kling-text2video-v1.6-pro`, etc.). Old PascalCase ids
  (`Seedream-5.0-Lite`, `Veo3`, `Wan-2.6-I2V`, …) still resolve via
  `deprecated_aliases` and will be removed in 0.4.
- `genblaze-gmicloud`: submits now send the canonical slug on the wire, not the
  caller-supplied string — matters because the GMICloud request queue is
  case-sensitive.
- `genblaze-gmicloud`: JSON error bodies (`{"error": "..."}`) are unwrapped
  before being surfaced, replacing the confusing double-encoded
  `GMICloud submit failed (500): {"error":"Backend error (400)..."}` message.
- `genblaze-gmicloud`: new model families registered — reve-create/edit/remix
  (+ fast variants), bria-fibo-* (blend/relight/restore/genfill/eraser),
  pixverse-v5.6 i2v/t2v/transition, wan2.6/2.7 t2v/i2v/r2v.

### Fixed
- `genblaze-gmicloud`: `fetch_output` parsed the wrong envelope path; live API
  returns `outcome.media_urls[0].url` but the connector read `outcome.*_url`
  keys only. Mock fixtures were aligned with the real shape in the same change.

## [0.2.1] - 2026-04-23

### Changed
- `genblaze-s3`: dependency pin widened to `genblaze-core>=0.1.0,<0.3` so the
  S3 backend is installable alongside core 0.2.x (ModelRegistry release).
  No code changes; s3 is compatible with both 0.1.x and 0.2.x core.

## [0.2.0] - 2026-04-23

### Added
- `genblaze-core`: `ModelRegistry` + `ModelSpec` — declarative, user-extensible
  per-model configuration across every provider connector. New public surface
  at `genblaze_core.providers`: `ModelSpec`, `ModelRegistry`, `PricingContext`,
  `PricingStrategy`, pricing helpers (`per_unit`, `per_input_chars`,
  `per_output_second`, `per_response_metric`, `tiered`, `bucketed_by_duration`,
  `by_param`, `by_model_and_param`, `first_match`), input routers
  (`route_images`, `route_audio`, `route_by_media_type`, `route_keyframes`,
  `chain_routers`), constraints (`requires_together`, `mutually_exclusive`,
  `required_one_of`, `implies`), and param schemas (`IntSchema`, `EnumSchema`,
  `StringSchema`, `BoolSchema`, `FloatSchema`, `ArraySchema`).
- `genblaze-core`: `BaseProvider.create_registry()`, `models_default()`,
  `prepare_payload(step)`, and `models=` ctor kwarg. Registry pricing is
  applied automatically after `fetch_output()` when `step.cost_usd` is unset.
- All 12 provider connectors (`gmicloud` video/image/audio, `openai`
  dalle/tts/sora, `google` imagen/veo, `elevenlabs` tts/sfx, `replicate`,
  `runway`, `luma`, `stability-audio`, `lmnt`, `decart` image/video) migrated
  to `ModelRegistry`. Inline `_PRICING` / `_MODELS` / `forward_keys` dicts
  removed; data now lives in spec-building functions.
- Docs: `docs/features/model-registry.md`, README quickstart for custom
  pricing/models, runnable `examples/custom_model_registry.py` (no API keys
  needed).

### Fixed
- `genblaze-s3`: credentials preserved across region auto-detection. Previously
  `_reconfigure_for_region` tried to recover credentials from
  `boto3.client.meta.config.__dict__`, which doesn't hold them — the
  reconfigured client silently lost creds and failed mid-upload with
  `NoCredentialsError`. Credentials are now persisted on the backend at
  construction and threaded through the rebuild.
- `genblaze-s3`: region preflight runs on `get`/`exists`/`delete`/`get_url`
  (presigned), not just `put`. Previously the first `exists()` call —
  routinely made by `ObjectStorageSink` before `put` — could skip
  verification and hit the wrong region.
- `genblaze-s3`: region-redirect endpoint rewrite now only fires for B2
  endpoints. AWS S3 / R2 / MinIO users hitting a 301 are no longer
  silently retargeted at `s3.{region}.backblazeb2.com`.
- `genblaze-s3`: explicit `ChecksumSHA256` in `extra_args` now routes
  through `put_object` (single-PUT). Whole-object SHA-256 is only valid
  for single-part uploads; the previous path would have let
  `upload_fileobj` take the multipart code path with an invalid header.
- `genblaze-core`: `ObjectLockConfig` rejects naive datetimes with a
  clear error. S3's handling of naive timestamps is ambiguous and we
  refuse to silently accept multi-year retention with a wrong anchor.
  Past retention still allowed but logs a loud warning.
- Regression tests added for every item above.

### Added
- `genblaze-core`: `ObjectLockConfig` dataclass + `ObjectStorageSink(manifest_lock=...)`
  parameter. Applies B2 Object Lock retention (GOVERNANCE or COMPLIANCE) to
  manifest uploads — turns genblaze's canonical-hash provenance into an
  immutable, audit-grade on-disk artifact. GOVERNANCE is the default;
  COMPLIANCE logs a prominent warning at construction because its retention
  cannot be shortened, even by the account root. See
  `docs/features/object-storage.md` for the full recipe.
- `genblaze-s3`: multipart uploads via `upload_fileobj` + `TransferConfig` —
  assets >16 MB now split into 16 MB parts uploaded 4-way in parallel, each
  part individually retryable. Transforms multi-GB video uploads from a
  lottery ticket into a reliable operation.
- `genblaze-s3`: per-part SHA-256 integrity via `ChecksumAlgorithm=SHA256`
  on every upload so B2 server-side-verifies transfer integrity.
- `genblaze-s3`: `S3StorageBackend.ensure_lifecycle_defaults()` helper that
  applies idempotent `AbortIncompleteMultipartUpload` (7 days) and
  `NoncurrentVersionExpiration` (30 days) rules. Called automatically by
  `for_backblaze(auto_lifecycle=True)` (default).
- `genblaze-s3`: automatic bucket-region auto-detection — the first
  `put()`/`exists()` call runs a HeadBucket preflight and transparently
  reconfigures the client if the bucket lives in a different region.
- `genblaze-s3`: `StorageBackend.put()` gains an `extra_args` passthrough
  for boto3-style `ExtraArgs` (Cache-Control, SSE, Object Lock, etc.).
- `genblaze-core`: immutable Cache-Control on CONTENT_ADDRESSABLE uploads
  (`public, max-age=31536000, immutable`), shorter private TTL on
  HIERARCHICAL. Unlocks the B2 + Cloudflare Bandwidth Alliance zero-egress
  delivery pattern documented in `docs/features/object-storage.md`.
- Docs: "Serving media at zero egress: B2 + Cloudflare" recipe.

### Changed
- `genblaze-s3`: `for_backblaze()` raises a clear `ValueError` when both
  `B2_KEY_ID`/`B2_APP_KEY` env vars and explicit `key_id`/`app_key` are
  missing — prevents opaque mid-upload `NoCredentialsError`.
- `genblaze-s3`: `BotoConfig` now pins
  `request_checksum_calculation="when_required"` /
  `response_checksum_validation="when_required"` so boto3 never sends
  `x-amz-sdk-checksum-algorithm` trailer headers that older B2 deployments
  and other S3-compat endpoints reject. Genblaze sets SHA-256 explicitly.
- `genblaze-s3`: default `max_pool_connections` bumped to 20 to accommodate
  concurrent multipart uploads.

## [0.1.0] - 2026-04-22

### Added
- Core models: `Asset`, `Step`, `Run`, `Manifest`
- Fluent builders: `StepBuilder`, `RunBuilder`, `ManifestBuilder`
- Canonical JSON serialization with SHA-256 hashing
- Unicode NFC normalization in canonical JSON
- New enums: `RunStatus`, `StepType`, `ProviderErrorCode`
- Asset fields: `width`, `height`, `duration`
- Step fields: `step_type`, `model_version`, `model_hash`, `seed`, `inputs`, `provider_payload`, `retries`, `error_code`
- Run fields: `status`, `project_id`
- Manifest fields: `manifest_uri`, `encryption_scheme`, `signature`
- `EmbedPolicy` model with `Manifest.to_embed_json()` for redaction
- PNG media handler (iTXt chunk embedding)
- JPEG and WebP media handlers (XMP-based embedding)
- Sidecar media handler (JSON alongside media)
- `SmartEmbedder` with auto-fallback to sidecar
- `get_handler()` media handler registry
- `MediaCapability` dataclass for handler introspection
- `Runnable[In, Out]` ABC with `|` composition operator
- `BaseProvider` with submit/poll/fetch_output lifecycle
- `ReplicateProvider` adapter (stores `provider_payload`)
- `genblaze-openai` `DalleProvider` expanded to OpenAI's full image lineup: `gpt-image-2` (free-form sizing), `gpt-image-1.5`, `gpt-image-1`, `gpt-image-1-mini`, alongside `dall-e-3` / `dall-e-2`
- `/v1/images/edits` support in `DalleProvider` — routed automatically when `step.inputs` is non-empty; accepts `file://` and `https://` inputs, optional `mask`, and multi-image composites
- New param passthroughs in `DalleProvider`: `output_format` (png/jpeg/webp), `output_compression`, `moderation`, `input_fidelity`, `mask`
- Registry-driven model metadata (`_ImageModelSpec`) replaces scattered size/pricing/format dicts; unknown models (`chatgpt-image-latest`, dated snapshots) pass through with `cost_usd=None`
- Provider error classification via `ProviderErrorCode`
- `Pipeline` fluent API for multi-step generation
- `PipelineResult` with `.save()` method and tuple unpacking support
- Pipeline: `step_type` parameter, `sink` parameter on `.run()`
- `ParquetSink` for structured run data output
- Parquet sink split into `runs/`, `steps/`, `assets/` tables with idempotency
- Parquet partitioning by `dt=/tenant_id=/modality=/provider=`
- `pyarrow` as optional `[parquet]` extra
- `StepSpan` timing context manager with `run_id`, `step_id`, `retries`, `cost` fields
- `StructuredLogger` for JSON log events; `.with_context()` for correlation IDs
- CLI: `extract`, `verify`, `replay`, `index` commands
- Builder methods: `.step_type()`, `.seed()`, `.model_version()`, `.model_hash()`, `.input_asset()`, `.project()`, `.status()`
- JSON Schema specifications (v1)
- GitHub Actions CI with Python 3.11/3.12/3.13 matrix
- Pre-commit hooks configuration
- `py.typed` PEP 561 marker
- Code coverage configuration (70% minimum)
- Shared test fixtures via `conftest.py`
- README.md with install, quickstart, and architecture docs
