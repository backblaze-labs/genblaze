<!-- last_verified: 2026-03-06 -->
# Feature: Embed Policy

## Purpose
Control what provenance data gets embedded into media files — redact prompts, use pointer mode, or strip parameters.

## Used By
- API: `EmbedPolicy`, `Manifest.to_embed_json()`, `SmartEmbedder.embed()`
- Pipeline: `PipelineResult.save(policy=...)`

## Core Functions
- `EmbedPolicy` — Configuration model for embedding behavior
- `Manifest.to_embed_json(policy)` — Apply policy and return filtered JSON

## Canonical Files
- EmbedPolicy model: `libs/core/genblaze_core/models/policy.py`

## Inputs
- `prompt_visibility`: "public" | "private" (redact prompts)
- `embed_mode`: "full" | "pointer" | "none"
- `include_params`: bool
- `include_seed`: bool

## Outputs
- Filtered JSON string suitable for embedding
- In pointer mode: only `manifest_uri` + `canonical_hash`

## Flow
- Create `EmbedPolicy` with desired settings
- Pass to `manifest.to_embed_json(policy)` or `embedder.embed(path, manifest, policy=policy)`
- Policy filters manifest data before serialization

## Edge Cases
- `embed_mode="none"` → skip embedding entirely
- `prompt_visibility="private"` → prompts replaced with redaction marker
- Missing `manifest_uri` in pointer mode → should include hash only

## Verification
- Test files: `libs/core/tests/unit/test_policy.py`
- Required cases: full mode, pointer mode, none mode, prompt redaction, param stripping
- Quick verify: `cd libs/core && pytest tests/unit/test_policy.py -v`
- Full verify: `make test`
- Pass criteria: filtered output matches expected redaction for each mode
