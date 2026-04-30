<!-- last_verified: 2026-04-29 -->
# genblaze-s3

**S3-compatible storage backend for [genblaze](https://github.com/backblaze-labs/genblaze) AI media pipelines — durable, content-addressable, dedup-ready. Works with [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) (recommended default), Cloudflare R2, MinIO, and AWS S3.**

`genblaze-s3` plugs into the genblaze `ObjectStorageSink` to persist AI-generated video, image, and audio — plus their SHA-256 provenance manifests — onto any S3-compatible object store. It handles streaming downloads from provider CDNs, SHA-256 hashing, multipart uploads with retries, pre-signed URLs for private buckets, and Object Lock retention for tamper-evident manifests on Backblaze B2.

## Why genblaze-s3

- **Durable by default** — Assets + manifests land in object storage, never stuck in a provider's expiring CDN URL.
- **Backblaze B2 first-class** — One-line `S3StorageBackend.for_backblaze()` helper, Object Lock support for immutable provenance.
- **Content-addressable dedup** — `KeyStrategy.CONTENT_ADDRESSABLE` stores each unique asset once by SHA-256.
- **Works with any S3 API** — AWS S3, Backblaze B2, Cloudflare R2, MinIO, SeaweedFS, Wasabi, Ceph.
- **Presigned URLs** — private buckets get time-limited URLs; public buckets get permanent `public_url_base` links.
- **Resilient multipart uploads** — credential-preserving retries, preflight checks, no partial writes.

## Backends

| Provider | Helper | Notes |
|---|---|---|
| **Backblaze B2** | `S3StorageBackend.for_backblaze("bucket")` | Reads `B2_KEY_ID` / `B2_APP_KEY`; Object Lock retention supported |
| AWS S3 | `S3StorageBackend(bucket="...", region="...")` | Standard AWS credential chain |
| Cloudflare R2 | `S3StorageBackend(bucket="...", endpoint_url="https://<acct>.r2.cloudflarestorage.com")` | |
| MinIO / self-hosted | `S3StorageBackend(bucket="...", endpoint_url="https://minio.example.com")` | |

## Install

```bash
pip install genblaze-s3
```

## Quickstart — Backblaze B2 (recommended)

```bash
export B2_KEY_ID="..."
export B2_APP_KEY="..."
```

```python
from genblaze_core import KeyStrategy, ObjectStorageSink, Pipeline
from genblaze_s3 import S3StorageBackend
from genblaze_replicate import ReplicateProvider

backend = S3StorageBackend.for_backblaze(
    "my-genblaze-bucket",
    # Defaults to "us-west-004". Pass the region your bucket actually lives
    # in (e.g. "us-east-005", "eu-central-003") to skip the redirect hop —
    # the backend auto-corrects on first use, but a right hint saves an RTT.
    region="us-west-004",
    # Optional: pass public_url_base for public buckets (get_url returns
    # permanent URLs).
    public_url_base="https://f004.backblazeb2.com/file/my-genblaze-bucket",
    # Recommended in 0.3.0+: opt in to lifecycle defaults (cancel orphaned
    # multipart uploads after 7 days, expire noncurrent versions after 30
    # days). Default flipped to False to avoid silent bucket-wide config
    # mutation; pass True or call `backend.ensure_lifecycle_defaults()`
    # post-construction.
    auto_lifecycle=True,
)

sink = ObjectStorageSink(
    backend,
    prefix="genblaze-assets",
    key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,   # dedupe by SHA-256
)

result = (
    Pipeline("b2-demo")
    .step(ReplicateProvider(), model="black-forest-labs/flux-schnell",
          prompt="a photorealistic cat wearing a tiny spacesuit")
    .run(sink=sink, timeout=120)
)

for step in result.run.steps:
    for asset in step.assets:
        print(asset.url, asset.sha256)

backend.close()
```

Resulting bucket layout with `CONTENT_ADDRESSABLE`:

```
genblaze-assets/
├── assets/{sha[:2]}/{sha[2:4]}/{sha}.ext    # one object per unique asset
└── manifests/{run_id}.json                   # one manifest per run
```

Switch to `KeyStrategy.HIERARCHICAL` for `runs/{date}/{run_id}/…` layout (better for run-grouped browsing, worse for dedup).

## Quickstart — AWS S3

```bash
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
```

```python
from genblaze_s3 import S3StorageBackend

backend = S3StorageBackend(bucket="my-genblaze-bucket", region="us-east-1")
# get_url() returns pre-signed URLs when public_url_base is not set
```

## Quickstart — Cloudflare R2 / MinIO

```python
from genblaze_s3 import S3StorageBackend

# R2
backend = S3StorageBackend(
    bucket="my-bucket",
    endpoint_url="https://<account-id>.r2.cloudflarestorage.com",
    access_key_id="...", secret_access_key="...",
)

# MinIO
backend = S3StorageBackend(
    bucket="my-bucket",
    endpoint_url="https://minio.example.com",
    access_key_id="...", secret_access_key="...",
)
```

## URL flavors and credential redaction

`backend.get_url(key)` returns either a public URL (when `public_url_base` is
set) or a presigned SigV4 URL. Pass an explicit policy when your code path
requires a specific flavor:

```python
from genblaze_s3 import URLPolicy

# Force public — raises URLPolicyError if public_url_base isn't configured.
url = backend.get_url("k", policy=URLPolicy.PUBLIC)

# Force presigned (even with public_url_base set) — useful for paid feeds.
url = backend.get_url("k", policy=URLPolicy.PRESIGNED, expires_in=900)
```

For credential-bearing URLs handed to HTTP clients, prefer the dedicated
methods — they return a `PresignedURL` value object that **redacts the
SigV4 signature in `repr()`/`str()`/`f"{...}"`**, so accidental log-line
interpolation no longer leaks credentials:

```python
download = backend.presigned_get("k", expires_in=3600)
upload = backend.presigned_put("k", expires_in=600, content_type="image/png")

print(f"download link: {download}")
# → download link: PresignedURL(... url='...?X-Amz-Signature=redacted...')

requests.get(download.url)  # explicit `.url` accessor for the unredacted form
```

`put()` no longer returns a presigned URL (it returns the storage key
instead) — this fixes the credential-leak risk callers hit by persisting
the old return value to logs/manifests/DB rows.

## Server-side encryption (SSE)

`Encryption` is a typed value object accepted symmetrically by `put`,
`get`, and `copy`:

```python
from genblaze_s3 import Encryption

# SSE-S3 (server-managed AES-256)
backend.put("k", data, encryption=Encryption.sse_s3())

# SSE-KMS
backend.put("k", data, encryption=Encryption.sse_kms("alias/my-app"))

# SSE-C — same key required on read; round-trips cleanly in 0.3.0+
key = secrets.token_bytes(32)
enc = Encryption.sse_c(key)
backend.put("k", data, encryption=enc)
backend.get("k", encryption=enc)
```

See the [main feature doc](https://github.com/backblaze-labs/genblaze/blob/main/docs/features/object-storage.md)
for SSE-C key handling, KMS configuration, and migration notes from
0.2.x.

## Object Lock for immutable manifests (Backblaze B2)

Genblaze can apply Object Lock retention to uploaded manifests, producing tamper-evident provenance suitable for compliance, legal, and content-authenticity workflows. See the main repo docs for the [Object Lock guide](https://github.com/backblaze-labs/genblaze/tree/main/docs/features).

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Storage feature doc**: https://github.com/backblaze-labs/genblaze/blob/main/docs/features/object-storage.md
- **Runnable examples**: [`b2_storage_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/b2_storage_pipeline.py), [`s3_storage_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/s3_storage_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- Provider adapters: [`genblaze-openai`](https://pypi.org/project/genblaze-openai/) · [`genblaze-google`](https://pypi.org/project/genblaze-google/) · [`genblaze-runway`](https://pypi.org/project/genblaze-runway/) · [`genblaze-luma`](https://pypi.org/project/genblaze-luma/) · [`genblaze-replicate`](https://pypi.org/project/genblaze-replicate/)

## License

MIT
