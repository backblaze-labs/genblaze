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
# Auto-applies recommended lifecycle rules (cancel orphaned multipart uploads
# after 7 days; expire noncurrent manifest versions after 30 days). Pass
# auto_lifecycle=False if lifecycle is managed out-of-band.
storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
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
- **Lifecycle defaults** — applies `AbortIncompleteMultipartUpload` after 7
  days and noncurrent-version expiry after 30 days. Prevents orphaned
  multipart uploads from silently accruing storage cost.
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

> **Note on `prefix="runs"`:** the `runs/` segment between `{prefix}` and
> the per-run folder is fixed under HIERARCHICAL. Passing `prefix="runs"`
> therefore produces `runs/runs/...` keys. The doubled segment is intended
> behavior — pick a different `prefix` (the default is `"genblaze"`) if it
> reads as a typo.

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
