<!-- completed: 2026-03-06 -->
# Plan: Close Feature Gaps

## Summary
Implemented missing capabilities: multi-format CLI support (SmartEmbedder), replay execution, MP4/MP3/WAV media handlers, step-level caching (StepCache), and async pipeline (Pipeline.arun()).

## Phases
- Phase 1: CLI multi-format extract/verify + replay execution
- Phase 2: Audio/video handlers (MP4, MP3, WAV)
- Phase 3: Pipeline caching + async execution
- Phase 4: Signing + encryption (deferred — requires design doc)

## Decisions
- CLI uses SmartEmbedder for auto-format detection rather than requiring `--format` flag
- Replay reconstructs a Pipeline from manifest steps; requires provider to be importable
- Audio/video handlers follow same BaseMediaHandler pattern as image handlers
