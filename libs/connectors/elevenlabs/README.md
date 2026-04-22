<!-- last_verified: 2026-04-22 -->
# genblaze-elevenlabs

**[ElevenLabs](https://elevenlabs.io) text-to-speech + sound-effects provider adapter for [genblaze](https://github.com/backblaze-labs/genblaze) — AI voice and SFX pipelines with SHA-256 provenance manifests on every clip.**

`genblaze-elevenlabs` wraps ElevenLabs' TTS models (Eleven v3, Multilingual v2, Flash v2.5) and text-to-sound-effects API as genblaze providers. Generate narration, dialogue, and sound design with tracked provenance; compose into multi-step AI pipelines; persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store.

## Why genblaze-elevenlabs

- **Eleven v3 + Multilingual v2 + Flash v2.5** — Pick quality (v3), breadth (multilingual, 29 languages), or latency (~75 ms for Flash).
- **Text-to-sound-effects** — generate Foley / ambient sound with the `ElevenLabsSFXProvider`.
- **Provenance by default** — SHA-256-verified manifest on every clip; embed manifest directly into MP3 files.
- **Word-level timings** — ElevenLabs alignment flows into genblaze's `WordTiming` / `Track` models for downstream subtitling + AV compositing.
- **Production-ready** — retries, timeouts, moderation hooks, step caching.
- **Durable storage** — plug `genblaze-s3` in for Backblaze B2 / AWS S3 / R2 / MinIO persistence.

## Providers + models

| Provider class | Modality | Models |
|---|---|---|
| `ElevenLabsTTSProvider` | audio | `eleven_v3` (most expressive, 70+ languages), `eleven_multilingual_v2` (stable, 29 languages), `eleven_flash_v2_5` (~75 ms latency) |
| `ElevenLabsSFXProvider` | audio | `eleven_text_to_sound_v2` |

Registered via entry points as `elevenlabs-tts` and `elevenlabs-sfx`.

## Install

```bash
pip install genblaze-elevenlabs
```

## Quickstart — TTS narration

```bash
pip install genblaze-core genblaze-elevenlabs
export ELEVENLABS_API_KEY="..."
```

```python
from genblaze_core import Modality, Pipeline
from genblaze_elevenlabs import ElevenLabsTTSProvider

run, manifest = (
    Pipeline("narration")
    .step(ElevenLabsTTSProvider(output_dir="output/audio"),
          model="eleven_v3",
          prompt="Welcome to Genblaze — AI media pipelines with full provenance.",
          modality=Modality.AUDIO,
          voice_id="JBFqnCBsd6RMkjVDRZzb",
          output_format="mp3_44100_128")
    .run(timeout=60)
)
print(run.steps[0].assets[0].url, manifest.canonical_hash)
```

## Quickstart — text-to-sound-effects

```python
from genblaze_elevenlabs import ElevenLabsSFXProvider

run, manifest = (
    Pipeline("sfx")
    .step(ElevenLabsSFXProvider(output_dir="output/sfx"),
          model="eleven_text_to_sound_v2",
          prompt="Thunder crashing during a heavy rainstorm with distant rumbling",
          modality=Modality.AUDIO, duration_seconds=10)
    .run(timeout=60)
)
```

## Persist to Backblaze B2

```python
from genblaze_core import KeyStrategy, ObjectStorageSink
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.HIERARCHICAL,
)
# pass sink=storage to .run(…)
```

[Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default sink for AI-generated audio — cost-efficient, S3-compatible, Object Lock for immutable manifests.

## Credentials

| Env var | Where to get it |
|---|---|
| `ELEVENLABS_API_KEY` | https://elevenlabs.io/app/settings/api-keys |

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Examples**: [`elevenlabs_tts_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/elevenlabs_tts_pipeline.py) · [`elevenlabs_sfx_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/elevenlabs_sfx_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on Backblaze B2 and other S3-compatible backends
- Other audio providers: [`genblaze-openai`](https://pypi.org/project/genblaze-openai/) (TTS) · [`genblaze-lmnt`](https://pypi.org/project/genblaze-lmnt/) (fast TTS) · [`genblaze-stability-audio`](https://pypi.org/project/genblaze-stability-audio/) (music) · [`genblaze-gmicloud`](https://pypi.org/project/genblaze-gmicloud/)

## License

MIT
