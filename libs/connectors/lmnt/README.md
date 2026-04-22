<!-- last_verified: 2026-04-22 -->
# genblaze-lmnt

**[LMNT](https://www.lmnt.com) ultra-low-latency text-to-speech provider adapter for [genblaze](https://github.com/backblaze-labs/genblaze) — real-time AI voice pipelines with SHA-256 provenance manifests on every clip.**

`genblaze-lmnt` wraps LMNT's sub-second TTS API as a genblaze provider — ideal for real-time agent voices, live narration, and interactive media where latency matters. Compose into multi-step AI pipelines, persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store, and emit a tamper-evident provenance manifest for every run.

## Why genblaze-lmnt

- **Ultra-low latency** — LMNT is optimized for real-time voice; pairs well with genblaze `AgentLoop`.
- **Provenance by default** — SHA-256-verified manifest on every clip; embed manifest directly into MP3.
- **Production-ready** — retries, timeouts, step caching, moderation hooks.
- **Composable** — chain LMNT narration with music (Stable Audio) + FFmpeg AV compositing.
- **Durable storage** — plug `genblaze-s3` in for Backblaze B2 / AWS S3 / R2 / MinIO persistence.

## Models

| Model | Notes |
|---|---|
| `lmnt-1` | LMNT's fast TTS model |

## Install

```bash
pip install genblaze-lmnt
```

Registers the `lmnt` provider via entry points; [`genblaze-core`](https://pypi.org/project/genblaze-core/) discovers it automatically.

## Quickstart — LMNT TTS

```bash
pip install genblaze-core genblaze-lmnt
export LMNT_API_KEY="..."
```

```python
from genblaze_core import Modality, Pipeline
from genblaze_lmnt import LMNTProvider

run, manifest = (
    Pipeline("lmnt-tts-demo")
    .step(LMNTProvider(output_dir="output/audio"),
          model="lmnt-1",
          prompt="The quick brown fox jumps over the lazy dog.",
          modality=Modality.AUDIO, voice="lily")
    .run(timeout=30)
)
print(run.steps[0].assets[0].url, manifest.canonical_hash)
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

[Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default sink for AI-generated audio.

## Credentials

| Env var | Where to get it |
|---|---|
| `LMNT_API_KEY` | https://app.lmnt.com/account |

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Example**: [`lmnt_tts_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/lmnt_tts_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on Backblaze B2 and other S3-compatible backends
- Other audio providers: [`genblaze-elevenlabs`](https://pypi.org/project/genblaze-elevenlabs/) (TTS + SFX) · [`genblaze-openai`](https://pypi.org/project/genblaze-openai/) (TTS) · [`genblaze-stability-audio`](https://pypi.org/project/genblaze-stability-audio/) (music) · [`genblaze-gmicloud`](https://pypi.org/project/genblaze-gmicloud/)

## License

MIT
