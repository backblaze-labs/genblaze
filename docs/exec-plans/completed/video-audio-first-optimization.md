<!-- completed: 2026-03-16 -->
# Video/Audio-First Framework Optimization

## Summary
Made the framework video/audio-native with rich asset metadata, streaming transfers, adaptive polling, parameter normalization, and parallel step groups.

## Changes
1. **Rich Asset Metadata (Schema 1.3)** — `VideoMetadata` and `AudioMetadata` on Asset with frame_rate, codec, has_audio, sample_rate, channels, word_timings
2. **Streaming File Transfer** — `AssetTransfer` streams to `SpooledTemporaryFile` instead of accumulating in memory
3. **Adaptive Polling** — Poll interval doubles every 30s (capped at 30s); `SubmitResult` enables provider timing hints
4. **Parameter Normalization** — `BaseProvider.normalize_params()` maps standard params (duration, aspect_ratio, resolution) to provider-native names
5. **Parallel Step Groups** — `arun()` with concurrent execution and semaphore-based concurrency control
6. **Provider Metadata** — All video/audio providers populate typed `asset.video` / `asset.audio`
