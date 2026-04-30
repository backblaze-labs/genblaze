<!-- last_verified: 2026-04-30 -->
# Ingest workflows

genblaze's pipeline surface is generation-shaped by default — you compose
:class:`Pipeline` from `.step(provider=…)` calls that produce new content.
But many real workflows are **non-generative**: live ingest, user-uploaded
content, archival imports, DAM bulk loads, podcast hosting, cross-tenancy
migrations. The bytes already exist; the SDK's job is to record provenance
for the act of bringing them in.

`Pipeline.ingest()` is the entry point. Each ingested asset becomes a
:class:`Step` with `step_type=StepType.INGEST` (or `StepType.IMPORT`),
`provider=None`, and `model=` set to the source identifier you supply
(`"rss"`, `"ugc-upload"`, `"dam-bulk"`, etc.). The resulting manifest
documents who, what, when, and from where, with the same canonical-hash
discipline as a generative run.

## Quickstart

```python
from genblaze_core import (
    Asset, KeyStrategy, ObjectStorageSink, Pipeline,
)
from genblaze_s3 import S3StorageBackend

# Storage sink (CONTENT_ADDRESSABLE recommended for ingest — dedupes
# duplicate uploads automatically by SHA-256).
backend = S3StorageBackend.for_backblaze(
    "my-bucket", auto_lifecycle=True,
)
sink = ObjectStorageSink(
    backend,
    prefix="ingest",
    key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
)

# A list of assets to ingest. URLs may be `https://` (downloaded with
# SSRF protection) or `file://` (allowlisted local paths only).
assets = [
    Asset(url="https://feed.example.com/ep1.mp3", media_type="audio/mp3"),
    Asset(url="https://feed.example.com/ep2.mp3", media_type="audio/mp3"),
]

result = Pipeline.ingest(
    assets=assets,
    source="rss",
    source_metadata={"feed_url": "https://example.com/podcast.xml"},
    sink=sink,
    name="weekly-podcast-import",
)

# After ingest:
# - Each asset's URL is now the durable backend URL
# - sha256 / size_bytes are populated
# - The manifest documents the import event
# - sink has a sidecar index for reverse lookup
print(f"Imported {len(result.run.steps)} assets")
print(f"Manifest hash: {result.manifest.canonical_hash}")
```

## Use cases

### Podcast hosting (RSS pull → store → manifest)

A podcast app polls an RSS feed, finds new episodes, and ingests them.
`Pipeline.ingest` records each episode's source URL, ingest timestamp,
and content hash. Later transcription / transcoding runs chain on top
via `input_from=` referencing the ingest step indices.

```python
result = Pipeline.ingest(
    assets=[Asset(url=episode.enclosure_url, media_type="audio/mp3")
            for episode in feed_entries],
    source="rss",
    source_metadata={"feed_url": feed.url, "show_title": feed.title},
    sink=podcast_sink,
)
```

### UGC upload (user file → moderation → manifest)

A web app accepts a file from a user, persists it durably, and records
who uploaded it. Moderation can run before or after the ingest step
via :class:`ModerationHook`.

```python
asset = Asset(url=f"file://{tmp_path}", media_type="image/jpeg")

# Ingest first — gets the asset durable + hashed.
result = Pipeline.ingest(
    assets=[asset],
    source="ugc-upload",
    source_metadata={
        "uploader_id": current_user.id,
        "session_id": session.id,
        "ip": request.remote_addr,
    },
    sink=ugc_sink,
)

# Moderation runs against the (now-persistable) asset.
moderation_result = my_moderation_hook.check_output([asset])
```

### Archival / DAM bulk import

A digital-asset-management migration moves thousands of files from a
legacy system into B2. The CONTENT_ADDRESSABLE strategy automatically
dedupes — identical files across the source dataset land at the same
hash key.

```python
assets = [
    Asset(url=f"file://{path}", media_type=mime)
    for path, mime in legacy_inventory
]
result = Pipeline.ingest(
    assets=assets,
    source="dam-bulk",
    source_metadata={"source_system": "legacy-cms", "migration_id": run_id},
    sink=dam_sink,
)
```

### Cross-system / cross-tenant transfers

Use `step_type=StepType.IMPORT` (vs the default `INGEST`) when bringing
assets across system / tenant boundaries — semantically distinct from a
fresh external pull.

```python
result = Pipeline.ingest(
    assets=cross_tenant_assets,
    source="cross-tenancy",
    source_metadata={"src_tenant": "acme", "dst_tenant": "acme-eu"},
    sink=eu_sink,
    step_type=StepType.IMPORT,
)
```

### Live ingest (RTMP / segment-by-segment)

`Pipeline.ingest` handles a batch per call. For live streams, batch a
window of segments per call and chain calls in your producer loop. The
SDK doesn't ship a stream-aware daemon — recipe-level only.

```python
async def ingest_loop(stream):
    while True:
        window = await stream.next_window(seconds=10)
        Pipeline.ingest(
            assets=[Asset(url=seg.url, media_type="video/mp4") for seg in window],
            source="rtmp-live",
            source_metadata={"stream_id": stream.id, "window_start": window.start},
            sink=live_sink,
        )
```

## Reverse lookup: from `asset_id` back to the manifest

When an `Asset` flows through your system in isolation (e.g. you've
serialized just the asset_id to a row in your job queue), you can
recover the manifest that introduced it:

```python
manifest = sink.read_manifest_for_asset(asset.asset_id)
if manifest is not None:
    print(f"Asset {asset.asset_id} ingested in run {manifest.run.run_id}")
    print(f"Source: {manifest.run.steps[0].metadata['source']}")
```

The reverse lookup works because `Pipeline.ingest` calls
`sink.put_asset(asset, manifest_uri=...)` for every asset, and
`ObjectStorageSink.put_asset` writes a sidecar index entry at
`{prefix}/_index/{asset_id}.json` mapping each asset to its manifest URI.

Assets *not* persisted via `Pipeline.ingest` (or via direct
`sink.put_asset(asset, manifest_uri=...)` with an explicit manifest_uri)
are not discoverable via this path — by design. The index is opt-in.

## Manifest determinism

`Pipeline.ingest` sorts the input assets by `asset_id` before building
steps. The resulting manifest's `canonical_hash` is therefore **invariant
under permuted input order** — calling `ingest(assets=[a, b, c])` and
`ingest(assets=[c, a, b])` with the same asset set produces a
byte-identical manifest hash (modulo `Run.name` and a few other
deliberately-included caller-provenance fields):

```python
import itertools
hashes = set()
for permutation in itertools.permutations(assets):
    r = Pipeline.ingest(assets=list(permutation), source="t", name="ingest-test")
    hashes.add(r.manifest.canonical_hash)
assert len(hashes) == 1  # invariant holds across all 6 permutations
```

This matters for reproducibility: a DAM bulk-import that retries with a
different scan order still produces the same provenance fingerprint.

## INGEST vs IMPORT

| Step type | Use when |
|---|---|
| `StepType.INGEST` (default) | External-source pull. RSS feed, UGC upload, web crawl, RTMP live capture. Bytes are arriving from outside the system for the first time. |
| `StepType.IMPORT` | Cross-system or cross-tenancy transfer. The bytes already lived in some genblaze-managed system; you're moving them to a new context. |

Both step types may have `provider=None` — the validator on :class:`Step`
permits null provider only for these two types. Every other step type
(GENERATE / UPSCALE / TRANSCODE / etc.) continues to require a provider
because there's an upstream service to attribute.

## Differences from the generative `Pipeline.step()` builder

The generation pipeline is fluent and order-sensitive:

```python
result = (
    Pipeline("generate-video")
    .step(provider=…, model=…, prompt=…)         # step 0
    .step(provider=…, input_from=[0], …)         # step 1 chains on step 0
    .run()
)
```

`Pipeline.ingest` is **factory-style** — one call, all assets, no
chaining within the ingest itself:

```python
result = Pipeline.ingest(
    assets=[…],          # parallel — order doesn't affect manifest
    source="rss",
    sink=sink,
)
# Subsequent generative work (transcribe, classify, transform) is its own
# Pipeline instance that references the ingested assets via input_from=.
```

The two surfaces compose: ingest the assets via `Pipeline.ingest`, then
run a generative `Pipeline.step()` that consumes them as inputs.

## Composing ingest with later generation

Once assets are durable in your sink, downstream generation references
them by URL or by asset_id:

```python
# Step 1: ingest podcasts
ingest_result = Pipeline.ingest(
    assets=[Asset(url=ep.url, media_type="audio/mp3") for ep in episodes],
    source="rss",
    sink=sink,
)

# Step 2: transcribe via Whisper (chains in once Wave 6 ships)
# transcribed = (
#     Pipeline("transcribe-batch")
#     .step(WhisperProvider(), input_from=[*range(len(ingest_result.run.steps))], …)
#     .run(sink=sink)
# )
```

## See also

- [Object storage](object-storage.md) — sink setup, key strategies,
  Object Lock, the underlying `put_asset` mechanics, and the storage
  backend hardening surface.
- [Provenance](manifest-provenance.md) — what canonical hashes
  guarantee; how to verify an ingested manifest later.
- [Moderation](moderation.md) — running content checks against
  ingested assets before they're served downstream.
