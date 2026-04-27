<!-- last_verified: 2026-03-16 -->
# Feature: Media Embedding

## Purpose
Embed provenance manifests directly into media files (PNG, JPEG, WebP, MP4, MP3, WAV, AAC/M4A, FLAC) with automatic fallback to sidecar JSON.

## Used By
- API: `SmartEmbedder`, `PngHandler`, `JpegHandler`, `WebpHandler`, `Mp4Handler`, `Mp3Handler`, `WavHandler`, `AacHandler`, `FlacHandler`, `SidecarHandler`, `get_handler()`
- CLI: `extract` and `verify` commands (auto-detect format)

## Core Functions
- `SmartEmbedder.embed()` — Auto-select handler, embed manifest, fallback to sidecar
- `BaseMediaHandler.embed()` / `.extract()` — Format-specific embedding
- `get_handler(mime_type)` — Handler registry lookup
- `MediaCapability` — Handler introspection

## Canonical Files
- SmartEmbedder: `libs/core/genblaze_core/media/embedder.py`
- Handler base: `libs/core/genblaze_core/media/base.py`
- PNG handler: `libs/core/genblaze_core/media/png.py`
- JPEG handler: `libs/core/genblaze_core/media/jpeg.py`
- WebP handler: `libs/core/genblaze_core/media/webp.py`
- MP4 handler: `libs/core/genblaze_core/media/mp4.py`
- MP3 handler: `libs/core/genblaze_core/media/mp3.py`
- WAV handler: `libs/core/genblaze_core/media/wav.py`
- AAC/M4A handler: `libs/core/genblaze_core/media/aac.py`
- FLAC handler: `libs/core/genblaze_core/media/flac.py`
- Sidecar handler: `libs/core/genblaze_core/media/sidecar.py`

## Inputs
- `path`: file path to media file
- `manifest`: Manifest or JSON string
- Optional `policy`: EmbedPolicy for redaction

## Outputs
- Modified media file with embedded manifest (inline) or `.json` sidecar
- `EmbedResult` with `method` ("inline" or "sidecar"), `path`, `sidecar_path`

## Flow
- `SmartEmbedder` checks MIME type → selects handler via `get_handler()`
- Attempts inline embed (iTXt for PNG, XMP for JPEG/WebP, UUID box for MP4, ID3v2 TXXX for MP3, LIST/INFO for WAV, MP4 freeform atom for AAC/M4A, Vorbis comment for FLAC)
- If inline fails or unsupported format → falls back to sidecar JSON
- Extract reverses: reads format-specific metadata → returns manifest JSON

## Edge Cases
- Unsupported format → sidecar fallback
- JPEG/WebP manifest > 60KB → sidecar fallback
- MP4 files 500 MB–2 GB → seek-based streaming embed (avoids loading full file into RAM)
- MP4 files > 2 GB → `EmbeddingError` (use sidecar fallback)
- MP3/WAV/AAC/M4A/FLAC without mutagen installed → `EmbeddingError` with install instructions
- RF64/BW64/RIFX WAV variants → `EmbeddingError` (not supported; use sidecar)
- Invalid file format → `EmbeddingError`
- File without embedded manifest → `EmbeddingError`

## Asset binding caveat

`asset.sha256` in the manifest is the hash of the asset bytes **before** embedding.
Embedding modifies the file (PNG inserts an iTXt chunk, MP4 appends a UUID box, etc.),
so the on-disk file's SHA-256 after embed will not match `asset.sha256`. To verify the
asset bytes themselves, either:

1. Hash the upstream artifact (the original asset stored in B2/S3 before embedding), or
2. Extract the manifest, strip the embed region per format, and re-hash the remainder.

Manifest-content verification (`manifest.verify()` and `genblaze verify <file>`) is
unaffected by this — the canonical hash is over the manifest payload, not the
container file. See [trust-modes.md](trust-modes.md#asset-binding-caveat).

## WebP lossless preservation

When embedding into a lossless WebP (VP8L), the handler detects the source codec and
preserves losslessness automatically. Callers can still override with `lossless=False`
if a lossy re-encode is acceptable.

## Atomicity

All inline embed paths (PNG, JPEG, WebP, MP4, MP3, WAV) and the sidecar handler use
atomic temp-file + `os.replace` writes. A crash mid-embed leaves the source file
intact; partial writes never overwrite the original.

## Verification
- Test files: `libs/core/tests/unit/test_png.py`, `test_jpeg.py`, `test_webp.py`, `test_mp4.py`, `test_mp3.py`, `test_wav.py`, `test_aac_handler.py`, `test_flac_handler.py`, `test_sidecar.py`, `test_embedder.py`, `libs/core/tests/golden/test_png_roundtrip.py`
- Required cases: embed+extract round-trip per format, sidecar fallback, handler registry, invalid file handling
- Quick verify: `cd libs/core && pytest tests/unit/test_png.py tests/unit/test_jpeg.py tests/unit/test_webp.py tests/unit/test_mp4.py tests/unit/test_mp3.py tests/unit/test_wav.py tests/unit/test_aac_handler.py tests/unit/test_flac_handler.py tests/unit/test_sidecar.py tests/unit/test_embedder.py -v`
- Full verify: `make test`
- Pass criteria: round-trip embed/extract produces verifiable manifest for all formats
