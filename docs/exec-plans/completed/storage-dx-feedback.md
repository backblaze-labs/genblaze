<!-- created: 2026-04-27 -->
# Storage DX Feedback — `ObjectStorageSink` / `StorageBackend`

Resolve four DX rough edges reported against `ObjectStorageSink` and the
`StorageBackend` ABC. None are bugs that block correctness today; one is
a latent footgun that breaks pointer-mode embedders on the second write of
the same run, the others are missing primitives that every consumer
re-implements.

Origin: external user feedback (2026-04-27). Tracked here because each
item is small but they share a single review surface — bundling avoids
three doc-update churns and one CHANGELOG entry per fix.

## Items

| ID | Title | Shape | Evidence |
|----|-------|-------|----------|
| S-01 | `manifest_uri` left `None` when manifest already exists | **F** | `libs/core/genblaze_core/storage/sink.py:226-241` — assignment is inside `if not self._backend.exists(manifest_key):`; subsequent `write_run` calls with the same run leave the in-memory `Manifest.manifest_uri` unset, even though the durable URL is well-defined |
| S-02 | No public way to derive a stored manifest's key/URL/body from a `Run` | **A** | `libs/core/genblaze_core/storage/sink.py:129-139` — `_build_manifest_key` is leading-underscore; consumers reimplement it or parse `manifest.manifest_uri` |
| S-03 | `StorageBackend` has no `key_from_url(url)` — every consumer hand-rolls `urlparse(url).path.partition("/")` | **A** | `libs/core/genblaze_core/storage/base.py` (no method); 7+ call sites grep `urlparse` to extract a key from `Asset.url` / `Manifest.manifest_uri` |
| S-04 | `prefix="runs"` produces `runs/runs/...` under `HIERARCHICAL` | **D** | `libs/core/genblaze_core/storage/sink.py:53` (`asset_prefix = f"{prefix}/runs"`) and `:132` (`parts = [self._prefix, "runs"]`) — the `/runs/` segment is intentional and documented at `base.py:77-86`, but the doubled segment reads as a typo |

## Goals & success criteria

- **One PR, one CHANGELOG entry** under `[Unreleased]`. Bundle the fix
  (S-01) with the additive helpers (S-02, S-03) and the doc clarification
  (S-04) — all four touch the same files and the same review surface.
- **Pointer-mode embedders survive a re-run.** After this lands, calling
  `sink.write_run(run, manifest)` twice with the same `run` yields a
  `manifest.manifest_uri` populated either way.
- **No new public knobs for cosmetic concerns.** S-04 is doc-only; we do
  not add `run_root=` (rejected as over-engineering for a one-time
  naming choice). The `/runs/` segment in HIERARCHICAL is documented and
  load-bearing for downstream tools that grep paths.
- **No silent-failure conflations.** `key_from_url` distinguishes "backend
  doesn't implement" (raises `NotImplementedError` from the ABC default)
  from "URL doesn't belong to this backend" (returns `None` from the S3
  impl). These are different conditions and the API has to keep them so.
- **Provenance integrity is opt-out, not opt-in.** `read_manifest` defaults
  to `verify=True`; callers who trust the source can pass `verify=False`.

## Non-goals / deliberate omissions

- **No refactor of `BaseSink`.** `BaseSink` is shape-neutral — `ParquetSink`
  writes parquet to a directory, `ObjectStorageSink` writes to keys, the
  not-yet-shipped `LocalFilesystemSink` (P1-14) will write to `Path`. A
  shared `manifest_key_for -> str` doesn't fit; designing the ABC for one
  consumer is premature. When P1-14 lands, hoist `read_manifest` only.
- **No backfill of old manifests.** S-01 only affects the in-memory
  `Manifest.manifest_uri` on the second write; persisted manifests already
  have the URI in their JSON body (the URI is set before `to_canonical_json`
  is called on the first write).
- **No fix at `sink.py:53`/`:132` for S-04.** Removing the `/runs/` segment
  would silently relayout every existing HIERARCHICAL bucket — a real
  breaking change for a cosmetic gotcha.
- **No cross-process / cross-instance write coordination.** S3 has no CAS;
  two `ObjectStorageSink` instances racing on the same key are unaffected
  by this PR. Documented as a known limitation in the docstring.

## Implementation

### S-01 — Always populate `manifest.manifest_uri`

`libs/core/genblaze_core/storage/sink.py`, `_write_run_impl`:

```python
manifest_key = self.manifest_key_for(run)  # uses S-02 helper
with self._manifest_lock:
    if not self._backend.exists(manifest_key):
        manifest_json = manifest.to_canonical_json()
        manifest_extra: dict = {"CacheControl": self._manifest_cache_control()}
        if self._manifest_object_lock is not None:
            manifest_extra.update(self._manifest_object_lock.to_extra_args())
        self._backend.put(
            manifest_key,
            manifest_json.encode("utf-8"),
            content_type="application/json",
            extra_args=manifest_extra,
        )
        logger.info("Manifest uploaded: %s", manifest_key)

# Always reflect the durable URL on the in-memory Manifest, whether
# we put the bytes this call or a prior call did. Pointer-mode embedders
# read this; leaving it None on retries is the original footgun.
manifest.manifest_uri = self._backend.get_durable_url(manifest_key)
```

`get_durable_url` is idempotent and takes its own region-verification lock,
so calling it outside `_manifest_lock` is correct. Two threads of the same
sink instance racing on the same run both end up writing the same URL —
the assignment is to the same `Manifest` instance, not a shared dict.

### S-02 — Public helpers on `ObjectStorageSink`

```python
def manifest_key_for(self, run: Run) -> str:
    """Storage key where this run's manifest is (or would be) written."""
    if self._key_strategy == KeyStrategy.HIERARCHICAL:
        parts = [self._prefix, "runs"]
        if run.tenant_id:
            parts.append(run.tenant_id)
        parts.append(run.created_at.strftime("%Y-%m-%d"))
        parts.append(run.run_id)
        parts.append("manifest.json")
        return "/".join(parts)
    return f"{self._prefix}/manifests/{run.run_id}.json"

def manifest_url_for(self, run: Run) -> str:
    """Durable, credential-free URL for this run's manifest."""
    return self._backend.get_durable_url(self.manifest_key_for(run))

def read_manifest(self, run: Run, *, verify: bool = True) -> Manifest:
    """Fetch and parse the stored manifest for this run.

    Caps the download at MAX_MANIFEST_BYTES to bound OOM blast on
    a malicious or corrupt object. Verifies the canonical hash by
    default — pass verify=False to skip the rehash on a manifest
    you just wrote yourself.
    """
    key = self.manifest_key_for(run)
    data = self._backend.get(key)
    if len(data) > MAX_MANIFEST_BYTES:
        raise SinkError(
            f"Stored manifest at {key} is {len(data)} bytes, exceeds "
            f"MAX_MANIFEST_BYTES={MAX_MANIFEST_BYTES}"
        )
    manifest = Manifest.model_validate_json(data)
    if verify and not manifest.verify():
        raise ManifestError(
            f"Stored manifest at {key} fails canonical_hash verification"
        )
    return manifest
```

`MAX_MANIFEST_BYTES` is imported from `genblaze_core._utils` (already
used by sidecar/embedder paths). `_build_manifest_key` is kept as a
one-line internal alias — no caller migration needed in this PR.

### S-03 — `StorageBackend.key_from_url`

`libs/core/genblaze_core/storage/base.py`:

```python
def key_from_url(self, url: str) -> str | None:
    """Inverse of get_durable_url — returns None for foreign URLs.

    Default raises NotImplementedError so backends that lack a
    well-defined inverse are explicit about it. Implementations
    that recognize their own URL shape return the key; for URLs
    that clearly belong to a different backend (different host,
    different bucket) they return None.
    """
    raise NotImplementedError(
        f"{type(self).__name__} does not implement key_from_url"
    )
```

`libs/connectors/s3/genblaze_s3/backend.py`:

```python
def key_from_url(self, url: str) -> str | None:
    from urllib.parse import unquote, urlparse

    # Public-base shape: {public_url_base}/{key}. Tried first because
    # public_url_base may have been set when the URL was written even
    # if it isn't now (and vice versa) — both shapes are tried so a
    # URL written under one config still resolves under the other.
    if self._public_url_base and url.startswith(self._public_url_base + "/"):
        return unquote(url[len(self._public_url_base) + 1 :])

    # Raw S3-endpoint shape: {endpoint}/{bucket}/{key}.
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    self._ensure_region_verified()
    endpoint_host = urlparse(self._client.meta.endpoint_url or "").netloc
    if parsed.netloc != endpoint_host:
        return None
    path = parsed.path.lstrip("/")
    bucket_prefix = self._bucket + "/"
    if not path.startswith(bucket_prefix):
        return None
    return unquote(path[len(bucket_prefix) :])
```

### S-04 — Doc-only callout for `prefix="runs"`

`libs/core/genblaze_core/storage/sink.py` `__init__` docstring (constructor
parameter section):

> `prefix`: Root prefix for all keys. Default `"genblaze"`. Note: under
> `KeyStrategy.HIERARCHICAL` the layout always nests under a `runs/`
> segment (`{prefix}/runs/...`), so `prefix="runs"` produces
> `runs/runs/...`. This is intentional — see the layout diagram in
> `docs/features/object-storage.md` — but pick a different prefix if
> the doubled segment reads as a typo.

`docs/features/object-storage.md`, under the **HIERARCHICAL** section
heading: an admonition box repeating the same caveat, plus a one-line
addition to the configuration reference table noting the interaction.

No `logger.warning`. The user passed the prefix on purpose; the SDK
shouldn't second-guess working configuration.

## Tests

`libs/core/tests/unit/test_object_storage_sink.py`:

- `test_manifest_uri_set_on_existing_key` — write a run, reset
  `manifest.manifest_uri = None`, call `write_run` again with the same
  run/manifest; assert `manifest.manifest_uri == backend.get_durable_url(key)`
  even though the second `exists` short-circuited the put.
- `test_manifest_key_for_matches_written_key` — `sink.manifest_key_for(run)`
  matches the key actually present in the backend store after `write_run`.
  Cover both `HIERARCHICAL` and `CONTENT_ADDRESSABLE`, with and without
  `tenant_id`.
- `test_manifest_url_for_round_trip` — `manifest_url_for(run)` is a usable
  durable URL pointing at the manifest.
- `test_read_manifest_round_trip` — write a run, `read_manifest(run)`
  returns an equal `Manifest` (canonical_hash matches), `verify()` is True.
- `test_read_manifest_size_cap` — synthesize a `MemoryBackend` entry
  bigger than `MAX_MANIFEST_BYTES`; `read_manifest` raises `SinkError`.
- `test_read_manifest_verify_false_skips_rehash` — corrupt the stored
  manifest's payload after writing; `read_manifest(run, verify=False)`
  returns the parsed object, `read_manifest(run, verify=True)` raises
  `ManifestError`.

`libs/core/tests/unit/test_storage_backend.py` (new file or extend
existing — check at implementation time):

- `test_key_from_url_default_raises` — base ABC raises
  `NotImplementedError` for an unimplemented backend.

`libs/connectors/s3/tests/test_backend.py`:

- `test_key_from_url_public_base_round_trip` — set `public_url_base`,
  call `get_durable_url(key)`, feed the URL back to `key_from_url`,
  assert equality. Cover percent-encoded keys with `/` and `%2F`.
- `test_key_from_url_raw_endpoint_round_trip` — no `public_url_base`,
  same round-trip.
- `test_key_from_url_foreign_host_returns_none` — URL with a different
  host returns `None`.
- `test_key_from_url_foreign_bucket_returns_none` — URL with the right
  host but a different bucket returns `None`.
- `test_key_from_url_malformed_returns_none` — non-URL string returns
  `None`.

## Docs

- `docs/features/object-storage.md`:
  - HIERARCHICAL section: admonition for the `prefix="runs"` doubling
    (S-04).
  - New "Looking up a stored manifest" subsection demonstrating
    `sink.manifest_key_for(run)` / `sink.read_manifest(run)` (S-02).
  - "Round-tripping a durable URL to a key" one-liner under either
    a new "URL utilities" subsection or the existing CDN section
    (S-03).
- CHANGELOG `[Unreleased]`:
  - `### Fixed` — S-01.
  - `### Added` — S-02 (three new methods on `ObjectStorageSink`),
    S-03 (`StorageBackend.key_from_url` ABC + S3 impl).
  - `### Docs` — S-04 callout.

## Out-of-scope follow-ups (explicitly noted, not blocking)

- Hoisting `read_manifest` onto `BaseSink` — defer until
  `LocalFilesystemSink` (P1-14) has a `manifest_path_for(run)` analog.
- Cross-process write coordination on the same key — out-of-scope; S3
  has no CAS and we don't promise it.
- Migrating existing `urlparse(url).path.partition("/")` snippets in
  example apps to `backend.key_from_url(url)` — additive; sample apps
  will adopt as they touch the relevant files.

## Done when

- `make test` and `make lint` pass.
- The `manifest_uri` regression test exists and would have failed before
  S-01.
- `docs/features/object-storage.md` shows the three new doc surfaces.
- CHANGELOG `[Unreleased]` carries entries for S-01/S-02/S-03/S-04.
- Plan moves to `docs/exec-plans/completed/` in the same PR.
