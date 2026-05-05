# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  - When a sink is supplied, ``put_asset`` is called for each asset
    with a derived ``manifest_uri`` so
    :meth:`BaseSink.read_manifest_for_asset` can later discover the
    manifest from any asset_id. Sinks that don't implement
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
  - ``BaseSink.put_asset(asset, *, manifest_uri=None) -> Asset`` —
    write a single asset's bytes via the sink's storage backend
    (no Run wrapper required). Mutates the asset in place: rewrites
    ``url`` to the durable backend URL, populates ``sha256`` and
    ``size_bytes``. Source bytes resolved from the asset's existing
    URL (``file://`` allowlisted dirs, or SSRF-protected ``https://``).
    Default impl on the ABC raises ``NotImplementedError`` so
    non-storage-backed sinks (``ParquetSink`` etc.) keep working.
  - ``BaseSink.put_assets(assets, *, manifest_uri=None) ->
    list[Asset]`` — bulk variant, parallelizes via
    ``ThreadPoolExecutor`` sized at ``min(max_upload_workers,
    len(assets))``. Returned list preserves input order.
  - ``BaseSink.read_manifest_for_asset(asset_id) -> Manifest |
    None`` — reverse-lookup. When ``put_asset`` is called with
    ``manifest_uri=``, the sink writes a sidecar index entry at
    ``{prefix}/_index/{asset_id}.json`` so future callers can
    discover the manifest from just the asset_id. Manifests for
    assets put without ``manifest_uri=`` are not discoverable
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
