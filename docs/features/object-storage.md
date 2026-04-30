<!-- last_verified: 2026-04-29 -->
# Object Storage

Upload run assets and manifests to any S3-compatible bucket. **Backblaze B2 is
the recommended default**; AWS S3, Cloudflare R2, and MinIO work too via the
generic constructor.

## How it works

Pass `sink=storage` to `pipeline.run()`. The `ObjectStorageSink`:

1. **Transfers assets** — downloads from provider CDN, computes SHA-256, uploads to storage
2. **Records partial-transfer failures** on `manifest.transfer_failures` (a non-hashed Manifest field). Transport diagnostics are kept out of the provenance hash, so `manifest.verify()` remains True even on partial failures
3. **Recomputes manifest hash** — the canonical hash reflects post-transfer asset URLs/SHA-256
4. **Uploads manifest** — writes the canonical JSON manifest alongside the assets
5. **Rewrites URLs** — asset URLs in the run now point to your bucket

### Quickstart (Backblaze B2)

```python
from genblaze_core import Pipeline, Modality, ObjectStorageSink, KeyStrategy
from genblaze_s3 import S3StorageBackend

# Reads B2_KEY_ID / B2_APP_KEY from env; override with key_id=/app_key= if needed.
# Bucket and region can also come from B2_BUCKET / B2_REGION — useful when all
# B2 config lives in .env and you want `for_backblaze()` with no arguments.
# Pass auto_lifecycle=True to opt in to recommended lifecycle rules (cancel
# orphaned multipart uploads after 7 days, expire noncurrent manifest
# versions after 30 days). Default 0.3.0+ is False; the prior default
# silently mutated bucket-wide config on every construction.
storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket", auto_lifecycle=True),
    key_strategy=KeyStrategy.HIERARCHICAL,
)

result = Pipeline("my-pipeline").step(...).run(sink=storage)
```

### What `for_backblaze()` does for you

`S3StorageBackend.for_backblaze()` is the recommended entry point when your
bucket is on B2 — it encodes the B2-specific tuning so you don't have to:

- **Credentials check** — raises a clear `ValueError` at construction if
  neither env vars nor explicit args are present (no opaque mid-upload
  `NoCredentialsError`).
- **Everything from `.env`** — `bucket`, `region`, `key_id`, and `app_key`
  each fall back to `B2_BUCKET` / `B2_REGION` / `B2_KEY_ID` / `B2_APP_KEY`,
  so `S3StorageBackend.for_backblaze()` with no arguments works when the
  environment is set. Explicit arguments always win.
- **Preflight verification** — `preflight=True` (default) issues one
  `HeadBucket` to verify region + credentials at construction. Any
  permanent failure (auth, missing bucket, signature mismatch) raises a
  typed `StorageError` immediately so placeholder credentials surface up
  front instead of failing on every subsequent operation. Transient
  failures (5xx, throttle, network blip) re-raise without caching, so
  the next call retries — a one-time hiccup at construction doesn't
  brick the backend forever. Pass `preflight=False` to defer verification
  to the first real I/O call (useful for offline tests with placeholder
  creds; cannot be combined with `auto_lifecycle=True`).
- **Region hint with auto-correct** — `region=` (or `$B2_REGION`) defaults
  to `us-west-004`. If your bucket lives elsewhere (e.g. `us-east-005`,
  `eu-central-003`), set it explicitly:
  ```python
  S3StorageBackend.for_backblaze("my-bucket", region="us-east-005")
  # or in .env:  B2_REGION=us-east-005
  ```
  On first use the backend issues one `HeadBucket` and, if B2 returns a
  redirect, transparently reconfigures itself against the correct regional
  endpoint. Some regions (notably `us-east-005`) reject cross-region
  requests with **403** instead of a 301 redirect — the backend can't
  auto-correct those, so the region must be set correctly up front.
- **Lifecycle (opt-in)** — pass `auto_lifecycle=True` to apply
  `AbortIncompleteMultipartUpload` after 7 days and noncurrent-version
  expiry after 30 days. **Default in 0.3.0+ is False** — bucket-wide
  config mutation is no longer a hidden side effect. Equivalent post-
  construction call: `backend.ensure_lifecycle_defaults()`.
- **Credential kwarg aliases** — both `aws_access_key_id` (boto3 native)
  and `access_key_id` (short form, used in the README and most
  ecosystem examples) work. Same for `aws_secret_access_key` /
  `secret_access_key`. Passing both names for the same credential
  raises `TypeError`; no silent precedence.
- **Multipart uploads** — any asset larger than 16 MB is split into
  16 MB parts uploaded 4-way in parallel. Each part is individually
  retryable on transient failures.
- **Per-part SHA-256 integrity** — every upload carries
  `ChecksumAlgorithm=SHA256` so B2 server-side-verifies transfer integrity.
- **Checksum header compat** — the backend pins
  `request_checksum_calculation="when_required"`, so boto3's default
  `x-amz-sdk-checksum-algorithm` header is never sent. This keeps the
  backend portable across all S3-compatible services (including older B2
  deployments, MinIO, Wasabi).
- **User-Agent attribution** — all requests carry `b2ai-genblaze/{version}`
  for B2 usage reporting.

### Other S3-compatible providers

```python
storage = ObjectStorageSink(
    S3StorageBackend(bucket="my-bucket", endpoint_url="https://..."),
    key_strategy=KeyStrategy.HIERARCHICAL,
)
```

## Key strategies

### HIERARCHICAL (run-grouped)

Everything for a run lives in one folder — easy to browse and manage.

```
{prefix}/runs/
  {tenant}/{date}/{run_id}/
    manifest.json
    assets/
      {asset_id}.mp4
      {asset_id}.png
```

The tenant segment is omitted when `tenant_id` is not set on the run.

> **`prefix="runs"` no longer doubles in 0.3.0+:** the strategy's hardcoded
> `runs/` segment is collapsed against a prefix that already ends in `runs`
> via the new `KeyBuilder` seam-dedupe. Pre-0.3.0 the double was preserved
> ("intentional"); the bug-tracker reclassified it as bug #5 because no
> real caller used the layout intentionally. Caller-intentional doubles
> *within* the prefix (e.g. `prefix="archive/archive"`) or *within* the
> strategy segments are still preserved — the dedupe is seam-only, not
> global. See "Migrating from 0.2.x" below if you have existing buckets
> written under the old key layout.

### CONTENT_ADDRESSABLE (deduped)

Assets are keyed by SHA-256 hash. Identical files across runs are stored once.

```
{prefix}/assets/
  {sha256[:2]}/{sha256[2:4]}/{sha256}.ext
{prefix}/manifests/
  {run_id}.json
```

## Looking up a stored manifest

`ObjectStorageSink` exposes three helpers so app code never has to
re-implement the layout rules or parse `manifest.manifest_uri`:

```python
key = sink.manifest_key_for(run)            # storage key (pure function)
url = sink.manifest_url_for(run)            # durable, credential-free URL
manifest = sink.read_manifest(run)          # fetch + parse + verify()
```

`read_manifest` defaults to `verify=True` (raises `ManifestError` on hash
mismatch) — pass `verify=False` to skip the rehash on a manifest you just
wrote yourself. Downloads are capped at 16 MiB to bound OOM blast.

After `write_run` returns, `manifest.manifest_uri` is also populated on
the in-memory object — including on retries that hit an already-existing
key. Pointer-mode embedders rely on this, so it is set unconditionally
whenever the manifest exists in the backend.

## Round-tripping a durable URL to a key

`StorageBackend.key_from_url(url)` is the inverse of `get_durable_url(key)`.
The S3 backend handles both URL shapes it can emit (raw S3-endpoint and
`public_url_base` CDN), and returns `None` for URLs that don't belong to
this backend (different host, different bucket, malformed) so callers can
route across backends without `try/except`:

```python
key = backend.key_from_url(asset.url)
if key is None:
    ...  # foreign URL — route elsewhere
else:
    backend.delete(key)
```

Backends that don't implement an inverse raise `NotImplementedError` from
the default — distinct from the "URL isn't mine" `None` signal so the two
conditions can't be confused.

## URL flavors: public vs. presigned

`S3StorageBackend` produces three kinds of URL. Pick the right one for the
job:

| URL flavor | Method | Carries credentials? | Persistable? | Expires? |
|---|---|---|---|---|
| **Durable** | `backend.get_durable_url(key)` | No | **Yes** — write to manifests / logs / DB | No |
| **Public** | `backend.get_url(key, policy=URLPolicy.PUBLIC)` | No | Yes — relies on `public_url_base` config | No |
| **Presigned** | `backend.presigned_get(key)` / `.presigned_put(key)` | **Yes** (`X-Amz-Signature`) | **No** — leak risk | Yes |

### `URLPolicy` — strict vs. permissive `get_url`

`get_url(key, *, expires_in=…, policy=URLPolicy.AUTO)` accepts a
`URLPolicy` enum to pick the flavor explicitly:

```python
from genblaze_s3 import S3StorageBackend, URLPolicy, URLPolicyError

backend = S3StorageBackend.for_backblaze(
    "my-bucket",
    public_url_base="https://media.example.com",
)

# AUTO (default) — public when public_url_base is set, presigned otherwise.
# Permissive: silently ignores expires_in when public is selected. Matches
# pre-0.3.0 behavior; preserved for backward compat.
url = backend.get_url("img.png")

# PUBLIC — force the public-URL form. Raises URLPolicyError if
# public_url_base isn't configured, or if expires_in is also passed
# (conflict — public URLs don't carry an expiry).
url = backend.get_url("img.png", policy=URLPolicy.PUBLIC)

# PRESIGNED — force a SigV4 URL even when public_url_base is set. Useful
# for paid-feed / time-limited fetches against a public-bucket backend.
url = backend.get_url("paid-feed.mp4", policy=URLPolicy.PRESIGNED, expires_in=900)
```

`URLPolicy.AUTO` is the default for backward compatibility. Use
`URLPolicy.PUBLIC` when your code path **requires** a public URL and
should fail loudly if the configuration is wrong (e.g. you accidentally
deployed without `public_url_base` set).

### `presigned_get` / `presigned_put` — typed, redaction-safe

For credential-bearing URLs handed to HTTP clients, prefer the dedicated
methods over `get_url(policy=URLPolicy.PRESIGNED)`. They return a
`PresignedURL` value object whose `__repr__` and `__str__` redact the
SigV4 signature — so accidental log-line interpolation no longer leaks
the access-key-id or signature:

```python
from genblaze_s3 import PresignedURL

# GET — for downloads
download = backend.presigned_get("k", expires_in=3600)
print(f"link: {download}")
# → link: PresignedURL(method='GET', bucket='my-bucket', key='k', ...
#    url='...?X-Amz-Signature=redacted&X-Amz-Credential=redacted...')

requests.get(download.url)  # explicit .url accessor for the unredacted form

# PUT — for direct browser/client uploads. content_type binds into the
# signature so the upload MUST send the same Content-Type header.
upload = backend.presigned_put("k", expires_in=600, content_type="image/png")
```

Every leak site becomes a deliberate `.url` access rather than a default
string interpolation. Pre-0.3.0, `put()` itself returned a presigned URL —
and anything that persisted it (logs, manifests, DB rows) leaked
transient credentials. **`put()` now returns the storage key** (a plain
`str`); compose with `get_durable_url` for the persistable form, or use
the methods above for an explicit credential-bearing URL.

> **`presigned_post` is intentionally deferred.** S3 POST policies return a
> `{"url", "fields"}` shape that doesn't fit `PresignedURL`. Tracked for a
> later phase that ships a separate `PresignedPost` value object.

## Server-side encryption (SSE)

`Encryption` is a typed value object accepted symmetrically by `put`,
`get`, and `copy`. Three modes:

```python
from genblaze_s3 import Encryption

# SSE-S3 — server-managed AES-256 keys. No extra config required.
backend.put("k", data, encryption=Encryption.sse_s3())

# SSE-KMS — KMS-managed keys (ARN, alias, or short id).
backend.put("k", data, encryption=Encryption.sse_kms("alias/my-app"))

# SSE-C — caller-managed keys. Exactly 32 bytes (AES-256). The MD5 of the
# key is computed automatically; pass `key_md5_b64=` if you already have it.
key = secrets.token_bytes(32)
enc = Encryption.sse_c(key)

backend.put("k", data, encryption=enc)
backend.get("k", encryption=enc)             # SAME key required on read
backend.copy("k", "k.copy", encryption=enc)  # source + dest re-encrypted
```

**SSE-C uploads were not round-tripping cleanly pre-0.3.0** — `get` and
`copy` silently dropped the customer-key envelope, so encrypted objects
4xx'd on download. The symmetric `encryption=` plumbing closes that
asymmetry.

`Encryption.sse_c(...)` redacts the customer key in `__repr__` and
`__str__`; only direct `enc.customer_key` access exposes the raw bytes.
**Don't pass an `Encryption` instance through `dataclasses.asdict()`** —
that recurses into all fields and leaks the raw key. Use the
`to_put_extra_args()` / `to_get_extra_args()` / `to_copy_extra_args()`
helpers when you need the boto3 wire shape.

> **SSE keys + `extra_args`:** if both `encryption=Encryption.sse_kms(A)`
> and `extra_args={"SSEKMSKeyId": "B"}` are passed, `put()` raises
> `ValueError` with a "SSE envelope conflict" message. Pre-fix, the
> caller's `extra_args` overrode the value object piecewise — silently
> encrypting the object with the wrong material. Pick exactly one
> source for the SSE envelope.
>
> The check fires **before** the network try/except wrapper, so it
> propagates as `ValueError` (caller API misuse) rather than being
> masked as `StorageError` (transport failure). `try/except StorageError`
> blocks won't catch it — and shouldn't; the two have different
> debugging semantics. Same pattern applies to the per-put Object
> Lock conflict guard.

## Tuning the backend: `StorageConfig`

`StorageConfig` is a frozen dataclass exposing the eight knobs the S3
backend bakes in by default. The defaults preserve historic behavior;
override one knob at a time without subclassing:

```python
from genblaze_core import StorageConfig

cfg = StorageConfig(
    max_pool_connections=40,        # default 20 — bump for high-fan-out workloads
    multipart_threshold=64 * 1024 * 1024,  # default 16 MiB
    user_agent_extra="my-app/1.2",  # appended to b2ai-genblaze/<version>
)
# Phase 2+ wires StorageConfig through `S3StorageBackend(...)` directly.
# Until then, callers passing one is no-op; the values document the
# Phase 2 surface.
```

## Typed errors: `StorageError` and `classify_botocore_error`

Failures from any backend method raise `StorageError` (subclass of
`GenblazeError`). 0.3.0+ extends `StorageError` with structured fields
that mirror `ProviderError`:

| Field | Type | Description |
|---|---|---|
| `error_code` | `StorageErrorCode \| None` | Typed classification (NOT_FOUND / ACCESS_DENIED / RATE_LIMIT / SERVER_ERROR / NETWORK / TIMEOUT / …) |
| `request_id` | `str \| None` | Upstream request id (`x-amz-request-id`) |
| `status_code` | `int \| None` | HTTP status when applicable |
| `retry_after` | `float \| None` | Server `Retry-After` hint |
| `is_retriable` | `bool` | Derived from `error_code` via `RETRYABLE_STORAGE_CODES` |
| `operation` | `str \| None` | Backend method that raised — `"put"`, `"get"`, etc. |

For connector authors: `genblaze_core.classify_botocore_error(exc, *, operation, key=None)`
maps a `botocore.ClientError` (or any boto exception) to a populated
`StorageError`. Use it in connector implementations to surface a
typed shape for the retry helper and observability tooling.

## Read primitives: `head` / `list` / `get_range` / `stream`

Phase 2 ships four read-side primitives that replace the
"manifest-is-the-DB" workaround pattern (where apps were reaching
into `_client.list_objects_v2` directly):

```python
# head — per-object metadata, or None for missing/inaccessible.
meta = backend.head("path/to/key")
if meta is None:
    print("missing")
else:
    print(f"{meta.size} bytes, content_type={meta.content_type}, etag={meta.etag}")

# list — paginated walk via continuation_token.
token = None
while True:
    page = backend.list(prefix="run-", max_keys=1000, continuation_token=token)
    for entry in page.entries:
        print(entry.key, entry.size, entry.last_modified)
    if page.next_token is None:
        break
    token = page.next_token

# get_range — partial-file reads via HTTP Range header.
header = backend.get_range("big.mp4", offset=0, length=4096)

# stream — chunked download for objects too large to fit in memory.
with open("/tmp/big.mp4", "wb") as out:
    for chunk in backend.stream("big.mp4", chunk_size=8 * 1024 * 1024):
        out.write(chunk)
```

`head()` returns `None` for both 404 AND 403 (parity with `exists()` —
B2/AWS scoped application keys legitimately get 403 on non-existent
reads). Other errors surface as typed `StorageError`.

`list()` returns a `ListPage(entries: tuple[FileEntry, ...], next_token:
str | None)`. `FileEntry` is the cheap shape S3's `ListObjectsV2` returns
natively (key, size, last_modified, etag, storage_class) — populating a
full `ObjectMetadata` for each entry would require N extra HEAD round-
trips and defeat pagination. Call `head(entry.key)` when you need the
content_type or user metadata for a specific entry.

`get_range()` validates `offset >= 0` and `length >= 0`; `length=0`
short-circuits without contacting the backend (useful for callers
whose offset/length arithmetic may collapse to nothing).

`stream()` holds an HTTP connection until the iterator exhausts. If
you `break` out of the iteration mid-stream, the connection is
discarded rather than recycled — see the docstring caveat. Iterating
to exhaustion is always cheap; aborting frequently in a hot loop
exhausts the connection pool faster than expected.

## Bulk deletes: `delete_many` / `delete_prefix`

```python
# Delete an explicit list of keys. dry_run=False default — caller
# passed the keys so safety-by-default would just be friction.
result = backend.delete_many(["k1", "k2", "k3"])
print(f"{len(result.deleted)} deleted, {len(result.errors)} failed")
for err in result.errors:
    print(f"  {err.key}: {err.code} — {err.message}")

# Delete every key under a prefix. dry_run=True default — see what
# would be removed before actually removing it.
preview = backend.delete_prefix("temp/", dry_run=True)
print(f"would delete {len(preview.deleted)} keys")

# Then actually delete:
result = backend.delete_prefix("temp/", dry_run=False)
```

Two safety asymmetries make these primitives boring-to-use:

- `delete_many` defaults `dry_run=False`. The caller already typed
  out the key list — adding dry-run-by-default would just be friction.
- `delete_prefix` defaults `dry_run=True`. A prefix can match more
  than the caller intended; the SDK demands an explicit
  `dry_run=False` to actually delete. Empty / whitespace-only prefix
  raises `ValueError` (a `prefix=""` would match every object in the
  bucket, virtually always a typo).

`delete_prefix` **streams page-by-page** rather than collecting all
matched keys into memory. Each page (up to 1000 keys, S3's hard cap)
issues one `DeleteObjects` call. Memory stays bounded for prefixes
matching millions of keys.

`delete_prefix` also surfaces partial progress on a mid-walk `list()`
failure — the returned `DeleteResult` carries the keys actually
deleted from prior pages plus a synthetic
`DeleteError(key="", code="list_failed", message=…)`. Caller sees
the partial state and can retry from the failed page.

## Progress callbacks

`put` / `get` / `stream` accept a `progress: Callable[[TransferProgress], None]`
callback that fires with cumulative byte counts:

```python
from genblaze_core import TransferProgress

def emit_pct(p: TransferProgress) -> None:
    if p.total_bytes is not None:
        pct = 100 * p.bytes_transferred / p.total_bytes
        print(f"{p.operation} {p.key} — {pct:.1f}%")
    else:
        print(f"{p.operation} {p.key} — {p.bytes_transferred} bytes")

backend.put("big.mp4", data, progress=emit_pct)
backend.get("big.mp4", progress=emit_pct)
for chunk in backend.stream("big.mp4", progress=emit_pct):
    ...
```

`TransferProgress.total_bytes` is `None` when the total is genuinely
unknown — e.g. uploading from an arbitrary `BinaryIO` whose remaining
size would require a draining pass to determine. For `bytes` and
`io.BytesIO` payloads the total is computed automatically (and honors
the BytesIO's current `tell()` position, so partially-consumed buffers
report accurate remaining bytes).

**Thread safety:** the `put` progress callback is invoked from boto3's
multipart workers (`max_concurrency=4` by default) — the SDK
serializes the cumulative-byte counter under a lock so concurrent
workers don't drop deltas. The callback itself is invoked outside the
lock, so a slow callback (e.g. queue publish) doesn't block the next
worker.

**Single-PUT carve-out:** when the caller pins `extra_args={"ChecksumSHA256": …}`,
`put` routes through boto3's `put_object` (single-PUT) which does not
accept a progress `Callback`. The progress callback is silently
skipped on that path — only fires when the multipart-managed
`upload_fileobj` path is in use.

**Async caveat (`AsyncS3StorageBackend.aput`):** the progress callback
fires on a boto3 worker thread, not the asyncio event loop, because
`aput` is currently threadpool-delegated (native multipart-aware
`aput` is a follow-up sub-phase). **Do not `await` inside an `aput`
progress callback** — it will not be executed in an event-loop
context. Use a thread-safe handoff (e.g. `loop.call_soon_threadsafe`,
a `queue.Queue`, or a synchronization primitive) to publish progress
to the event loop. Native async paths (`aget` / `astream` on
`AsyncS3StorageBackend`) fire the callback on the event loop thread —
no thread-bridge needed.

## Per-put Object Lock

Apply Object Lock retention on a single `put()` without configuring
the sink-wide `manifest_lock`:

```python
from datetime import datetime, timedelta, timezone
from genblaze_core import ObjectLockConfig

backend.put(
    "audit/critical-manifest.json",
    data,
    object_lock=ObjectLockConfig(
        retain_until=datetime.now(timezone.utc) + timedelta(days=2555),  # 7y
        mode="GOVERNANCE",
    ),
)
```

The same conflict-guard pattern as SSE: passing both `object_lock=`
and an overlapping `extra_args` key
(`ObjectLockMode` / `ObjectLockRetainUntilDate` / `ObjectLockLegalHoldStatus`)
raises `ValueError` rather than silently merging mismatched envelopes.
Pick exactly one source.

## Async surface: `AsyncS3StorageBackend`

Phase 3 ships a native-async backend wrapping the sync one. Install
the optional dep:

```bash
pip install 'genblaze-s3[async]'
```

Use as an async context manager:

```python
import asyncio
from genblaze_s3 import AsyncS3StorageBackend

async def main():
    async with AsyncS3StorageBackend(
        bucket="my-bucket",
        endpoint_url="https://s3.us-west-004.backblazeb2.com",
        region="us-west-004",
    ) as ab:
        # Native async download
        data = await ab.aget("path/to/key")

        # Native async streaming (real AsyncIterator[bytes])
        async for chunk in ab.astream("big.mp4", chunk_size=8 * 1024 * 1024):
            ...

        # Other ops delegate to the sync backend via asyncio.to_thread.
        result = await ab.aput("k", data)
        meta = await ab.ahead("k")
        page = await ab.alist(prefix="run-")

asyncio.run(main())
```

### `from_sync` — borrow an existing backend's settings

A common pattern is starting with a sync backend (e.g. via
`for_backblaze`) and adding async to an existing app. `from_sync`
constructs an async backend that **shares** the sync backend's
verified-region state — no redundant preflight `HeadBucket` on the
async path:

```python
sync = S3StorageBackend.for_backblaze("my-bucket", auto_lifecycle=True)
async with AsyncS3StorageBackend.from_sync(sync) as ab:
    data = await ab.aget("k")  # no extra HeadBucket — sync was already verified
```

The wrapped sync backend is exposed at `ab.sync` for callers who need
non-async helpers (lifecycle, key utilities) without leaving the async
context.

### Native vs. threadpool-delegated methods

Currently:

| Method | Surface |
|---|---|
| `aget`, `astream` | **Native** via aioboto3 (`Body.read()` / `Body.iter_chunks`) |
| `aput`, `ahead`, `alist`, `aexists`, `adelete`, `acopy`, `adelete_many`, `adelete_prefix`, `aget_range`, `aget_url`, `aget_durable_url` | Threadpool-delegated to the sync backend via `asyncio.to_thread` |

Native versions of the threadpool-delegated methods are tracked as a
follow-up sub-phase. `aput` in particular needs aioboto3-native
multipart support, which is more involved.

The native paths are configured with the same B2-critical knobs as
sync: `request_checksum_calculation="when_required"` (the boto3 ≥
1.36 CRC32-trailer fix), `connect_timeout=30`, `read_timeout=300`,
`max_pool_connections=20`, and the `b2ai-genblaze/<version>`
user-agent. Without these, async calls to B2 endpoints would hit the
same trailer breakage Phase 1B fixed for sync.

### When to use sync `aget`/`aput`/etc. vs `AsyncS3StorageBackend`

The sync `S3StorageBackend` already exposes `aput`/`aget`/etc. — but
those threadpool-wrap the sync impl. For most workloads that's fine.
Pick `AsyncS3StorageBackend` when:

- You want **true async streaming** (`async for` over `astream`) without
  buffering into a queue. The threadpool wrapper of `stream` would
  defeat back-pressure.
- You're running **many concurrent downloads** in the same event loop;
  native aioboto3 doesn't pin a thread per call, so you avoid GIL
  contention at high concurrency.
- You want consistent async-ness across the surface (everything
  `await`-able, no mixed sync calls inside an async function).

Stick with `S3StorageBackend.aget`/etc. when:

- You only need occasional async dispatch from sync code; the
  threadpool wrap is fine.
- You don't want the `aioboto3` optional dep.

## Migrating from 0.2.x

Phase 1 of the storage-backend hardening tranche introduced four
intentional behavior changes. Each is independently visible:

| Change | Migration |
|---|---|
| `put()` returns the storage key (not a presigned URL) | Compose with `get_durable_url(key)` for the persistable URL form, or `presigned_get(key)` for an explicit credential-bearing URL. Internal callers in this monorepo all discard `put()`'s return value already; only external code persisting it is affected — and those callers were leaking credentials. |
| `for_backblaze(auto_lifecycle=...)` defaults `False` | Pass `auto_lifecycle=True` explicitly to keep historic behavior, or call `backend.ensure_lifecycle_defaults()` after construction. |
| `for_backblaze` preflight failures raise (was warn-and-continue) | Placeholder/invalid credentials now fail loudly at construction. Use `preflight=False` for offline tests with placeholder creds. |
| `prefix="runs"` no longer produces `runs/runs/...` | If you have existing buckets written under the old layout, the new manifest/asset key for an upcoming write will diverge. Two paths: (a) keep the old keys readable via `read_manifest(run)` (the layout function is determined by sink config — old keys still resolve from old config), or (b) re-key the existing data via `backend.copy(old_key, new_key)`. Most callers using the documented `prefix="genblaze"` (or any prefix not in the dup case) are unaffected. |

## Compose pattern: cloud + local

Upload to cloud storage *and* embed the manifest into the local copy:

```python
from genblaze_openai import DalleProvider

result = Pipeline("compose-demo").step(
    DalleProvider(output_dir="output/"),
    model="dall-e-3",
    prompt="a sunset over mountains",
    modality=Modality.IMAGE,
).run(sink=storage)

# Assets are in the bucket. Embed provenance into the local copy:
local_path = f"output/{result.run.steps[0].assets[0].asset_id}.png"
result.save(local_path)
```

The provider's `output_dir` saves a local copy during generation. After the sink
uploads to cloud and rewrites URLs, `result.save()` embeds the manifest into
the local file so it carries its own provenance.

## Compose pattern: cloud + Parquet analytics

`ObjectStorageSink` natively delegates to a `ParquetSink`:

```python
from genblaze_core import ObjectStorageSink, KeyStrategy, ParquetSink
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.HIERARCHICAL,
    parquet_sink=ParquetSink("data/"),
)

result = Pipeline("full-pipeline").step(...).run(sink=storage)
# Cloud: assets + manifest in bucket
# Local: partitioned Parquet tables in data/ (runs, steps, assets)
```

## Configuration reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `backend` | `StorageBackend` | required | S3-compatible storage backend |
| `prefix` | `str` | `"genblaze"` | Root prefix for all keys |
| `key_strategy` | `KeyStrategy` | `CONTENT_ADDRESSABLE` | Layout strategy |
| `parquet_sink` | `ParquetSink` | `None` | Optional structured data sink |
| `max_upload_workers` | `int` | `4` | Max parallel asset uploads per `write_run` call |
| `manifest_lock` | `ObjectLockConfig \| None` | `None` | When set, applies Object Lock retention to manifests. See "Immutable provenance via Object Lock" below. |

## Backward compatibility

Existing buckets using the previous HIERARCHICAL layout (assets at `{prefix}/assets/{tenant}/{date}/{run_id}/{asset_id}.ext`) continue to work — URLs stored in manifests remain valid regardless of layout changes. Only newly written data uses the updated paths.

## Immutable provenance via Object Lock

Genblaze's product promise is cryptographically verified provenance for
every generated asset. **Object Lock** is the on-disk enforcement of that
promise — once set, the manifest cannot be deleted or overwritten for the
retention period, turning a hash-verified document into an audit-grade
legal-hold artifact.

Backblaze B2 supports Object Lock natively via the S3-compatible API.
**Note:** the bucket must have Object Lock *enabled at creation time* — it
cannot be toggled on later.

```python
from datetime import datetime, timedelta, timezone

from genblaze_core import (
    KeyStrategy,
    ObjectLockConfig,
    ObjectStorageSink,
    Pipeline,
)
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-locked-bucket"),
    key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
    # GOVERNANCE: authorized admins holding s3:BypassGovernanceRetention
    # can still delete. Safe default for audit trails.
    manifest_lock=ObjectLockConfig(
        retain_until=datetime.now(timezone.utc) + timedelta(days=365),
        mode="GOVERNANCE",
    ),
)

result = Pipeline("locked-run").step(...).run(sink=storage)
# Manifest at s3://my-locked-bucket/manifests/{run_id}.json is now
# immutably retained until the retain_until date.
```

### GOVERNANCE vs. COMPLIANCE

- **GOVERNANCE** (default, recommended) — authorized users holding
  `s3:BypassGovernanceRetention` can still delete. Standard audit-trail
  retention.
- **COMPLIANCE** — *no one* can delete the object until retention expires,
  including the account root. A bad retention date cannot be shortened.
  Use only for strict regulatory scenarios (e.g. legal hold). The sink
  logs a loud warning at construction when this mode is chosen.

### Why B2 Object Lock fits genblaze's provenance story

- Native S3-API support — no separate native API required.
- Priced transparently at standard storage rates.
- Pairs with B2's always-on bucket versioning: every manifest write
  creates a new, independently-lockable version.
- Combined with genblaze's `canonical_hash`: the hash *proves* the
  manifest hasn't been tampered with; Object Lock *prevents* it from
  being tampered with in the first place.

## Serving media at zero egress: B2 + Cloudflare

Backblaze B2 and Cloudflare have a
[Bandwidth Alliance partnership](https://www.backblaze.com/blog/backblaze-and-cloudflare-partner-to-provide-free-data-transfer/)
that makes egress from B2 to Cloudflare **free**. Paired with `genblaze-s3`'s
immutable `Cache-Control` headers on content-addressable keys, this is the
cheapest production-grade media delivery path for AI-generated assets.

**Setup (one-time):**

1. Make the bucket public in the B2 console.
2. Add a CNAME in Cloudflare DNS:
   `media.example.com → f004.backblazeb2.com` (use the realm for your region).
3. Enable **Proxy** (orange cloud) on the CNAME.
4. In Cloudflare, add a **Transform Rule** to rewrite the request path:
   `/my-bucket/$1` → keeps URLs clean at `media.example.com/assets/...`.
5. Enable **Cache Rules** with `Cache everything` for your bucket prefix.

**In your code — just point `public_url_base` at your Cloudflare hostname:**

```python
from genblaze_core import ObjectStorageSink, KeyStrategy
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze(
        "my-bucket",
        public_url_base="https://media.example.com",
    ),
    key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
)
```

Because the assets are CAS-keyed (hash-derived), genblaze automatically sets
`Cache-Control: public, max-age=31536000, immutable` on each upload — so
Cloudflare caches indefinitely and B2 only serves each asset once per
Cloudflare edge. The net effect: **storage cost from B2, near-zero egress,
instant media playback for end users.**
