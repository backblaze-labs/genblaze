<!-- completed: 2026-03-16 -->
# Production-Ready Audio/Video Enhancements

## Summary
Eight features to make genblaze production-ready for AV workflows: AAC/FLAC embedding, typed word timings, pipeline timeout, multi-track assets, mock providers, error mapper extraction, and provider capabilities.

## Changes
1. **AAC/M4A + FLAC embedding** — MP4FreeForm atom (AAC) and Vorbis comment (FLAC) handlers
2. **Word-level timings** — `WordTiming` model on `AudioMetadata.word_timings` (LMNT + ElevenLabs)
3. **Pipeline timeout + on_step_complete** — Wall-clock timeout and per-step callback
4. **Multi-track assets** — `Track` list on Asset for containers with video + audio streams
5. **Mock providers** — `MockVideoProvider` / `MockAudioProvider` with typed metadata
6. **Error mapper extraction** — `_errors.py` module per connector family
7. **Provider capabilities** — `get_capabilities()` returning `ProviderCapabilities` dataclass

## Decisions
- AAC uses MP4FreeForm atom via mutagen; FLAC uses Vorbis comments
- WordTiming is a typed model with backward-compat validator for raw dicts
- Pipeline timeout uses `time.monotonic()`, checked before each step (not mid-step)
- `get_capabilities()` is non-abstract with default `None` — no breaking changes to existing providers
