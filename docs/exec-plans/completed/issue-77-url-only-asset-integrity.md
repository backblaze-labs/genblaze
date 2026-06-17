# Issue 77: URL-only asset integrity

## Issue

GitHub issue: https://github.com/backblaze-labs/genblaze/issues/77

`Manifest.verify()` currently succeeds for successful output assets that have a URL but no `sha256`. Since `asset.url` is excluded from the canonical hash payload, two different URL-only outputs can collapse to the same hash while still verifying.

## Plan

1. Update manifest hash payload construction so new schema-version unhashed assets carry an explicit URL-only marker and URL fallback in the canonical payload.
2. Update `Manifest.verify()` so output assets without `sha256` do not verify as asset-byte integrity provenance, while `Manifest.verify_hash()` remains hash-only.
3. Preserve backwards verification for schema versions before the URL-only marker behavior.
4. Preserve the durable-storage path: once `ObjectStorageSink` transfers assets and fills `sha256`, URL rewrites remain excluded from the canonical hash.
5. Add core regression tests for URL-only outputs and update partial-transfer expectations.
6. Add no-sink connector coverage for OpenAI DALL-E, Runway, and Luma URL-only outputs.
7. Update docs that describe SHA-256-bound provenance.

## Verification

- `cd libs/core && pytest tests/unit/test_models.py tests/unit/test_object_storage_sink.py -v`
- `cd libs/connectors/openai && pytest tests/test_dalle_provider.py -v`
- `cd libs/connectors/runway && pytest tests/test_runway_provider.py -v`
- `cd libs/connectors/luma && pytest tests/test_luma_provider.py -v`
- `make lint`
- `make test`
