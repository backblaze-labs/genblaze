<!-- created: 2026-03-16 -->
# P0 + P1 Production Quality Push

## Goal
Close all P0 and P1 gaps identified in the video production readiness review.
16 items total: 3 P0, 13 P1.

## Execution Waves

### Wave 1 — Quick fixes (small, independent, low risk)
Direct edits — no agents needed. Each is 5-30 lines.

| # | Item | Priority | Files |
|---|------|----------|-------|
| 1 | PipelineTimeoutError subclass | P1 | `exceptions.py`, `pipeline.py` |
| 2 | Fix compliance harness file:// URLs | P1 | `testing.py` |
| 3 | Add step.inputs to cache key | P1 | `pipeline/cache.py` |
| 4 | Fix Luma get_capabilities to include "image" | P1 | `luma/provider.py` |
| 5 | VeoProvider._operations TTL cleanup | P1 | `google/provider.py` |
| 6 | Sandbox file:// reads in _read_local_file | P1 | `storage/transfer.py` |
| 7 | TTS providers populate asset.duration | P1 | `openai/tts.py`, `elevenlabs/provider.py`, `stability-audio/provider.py` |

### Wave 2 — Pipeline architecture (P0, complex)
Agent-based — these touch core pipeline logic.

| # | Item | Priority | Files |
|---|------|----------|-------|
| 8 | Sora image-to-video input support | P0 | `openai/provider.py`, tests |
| 9 | Multi-input fan-in (step references) | P0 | `pipeline/pipeline.py`, tests |
| 10 | FFmpeg compositor provider | P0 | new `libs/core/genblaze_core/providers/compositor.py`, tests |

### Wave 3 — Storage & embedding (P1, moderate complexity)
Agent-based — touches storage layer and MP4 handler.

| # | Item | Priority | Files |
|---|------|----------|-------|
| 11 | Streaming MP4 embed (no full-file read) | P1 | `media/mp4.py`, `media/base.py`, tests |
| 12 | ObjectStorageSink parallel uploads | P1 | `storage/sink.py`, tests |
| 13 | StorageBackend.put() headers + multipart hint | P1 | `storage/base.py`, `storage/transfer.py` |
| 14 | Pre-signed URL in abstract interface | P1 | `storage/base.py` (already has expires_in) |

### Wave 4 — Infrastructure (P1, docs + patterns)

| # | Item | Priority | Files |
|---|------|----------|-------|
| 15 | Checkpoint hooks for prediction_id persistence | P1 | `providers/base.py`, docs |
| 16 | Queue integration pattern documentation | P1 | docs only |
