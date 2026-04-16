<!-- last_verified: 2026-04-16 -->
# App Workflows

## Generate Media with Provenance

- User creates a `Pipeline` with name and tenant
- Adds one or more `.step()` calls with provider, model, and prompt
- Calls `.run()` to execute all steps
- Receives `PipelineResult` with run data and verified manifest
- Optionally saves output with `result.save(path)` to embed manifest into media
- See [Pipeline](features/pipeline.md)

## Build Manifest Manually

- User creates steps with `StepBuilder` (provider, model, prompt, params, assets)
- Wraps steps in a run via `RunBuilder`
- Builds manifest via `Manifest.from_run()`
- Manifest includes `canonical_hash` for integrity verification
- See [Manifest Provenance](features/manifest-provenance.md)

## Embed Manifest into Media

- User has a manifest and a media file (PNG, JPEG, WebP)
- Uses `SmartEmbedder.embed(path, manifest)` for auto-format detection
- Or uses format-specific handler directly (e.g., `PngHandler`)
- If inline embedding fails or format unsupported → sidecar JSON created
- Optionally applies `EmbedPolicy` to redact prompts or use pointer mode
- See [Media Embedding](features/media-embedding.md), [Embed Policy](features/embed-policy.md)

## Extract and Verify Provenance

- User has a media file with embedded manifest
- Runs `genblaze extract image.png` to see the manifest
- Runs `genblaze verify image.png` to check hash integrity
- Or programmatically: `handler.extract(path)` → `manifest.verify()`
- See [CLI](features/cli.md)

## Replay a Pipeline

- User has a manifest JSON file from a previous run
- Runs `genblaze replay manifest.json` for dry-run preview
- Runs `genblaze replay manifest.json --no-dry-run` to re-execute
- See [CLI](features/cli.md)

## Index Run Data for Analytics

- User has manifest JSON files
- Runs `genblaze index manifest.json -o ./data` to write to Parquet
- Or programmatically: `ParquetSink(dir).write_run(run, manifest)`
- Data partitioned by date/tenant/modality/provider for querying
- See [Parquet Sink](features/parquet-sink.md)

## Write a Custom Provider

- Developer subclasses `BaseProvider`
- Implements `submit(step)`, `poll(prediction_id)`, `fetch_output(prediction_id, step)`
- Uses provider with `Pipeline.step(my_provider, ...)`
- See [Provider System](features/provider-system.md)
