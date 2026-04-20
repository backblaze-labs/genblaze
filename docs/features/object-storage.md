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
storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.HIERARCHICAL,
)

result = Pipeline("my-pipeline").step(...).run(sink=storage)
```

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

### CONTENT_ADDRESSABLE (deduped)

Assets are keyed by SHA-256 hash. Identical files across runs are stored once.

```
{prefix}/assets/
  {sha256[:2]}/{sha256[2:4]}/{sha256}.ext
{prefix}/manifests/
  {run_id}.json
```

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

## Backward compatibility

Existing buckets using the previous HIERARCHICAL layout (assets at `{prefix}/assets/{tenant}/{date}/{run_id}/{asset_id}.ext`) continue to work — URLs stored in manifests remain valid regardless of layout changes. Only newly written data uses the updated paths.
