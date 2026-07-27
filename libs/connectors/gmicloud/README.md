<!-- last_verified: 2026-07-27 -->
# genblaze-gmicloud

**[GMICloud](https://gmicloud.ai) multi-provider video / image / audio adapters for [genblaze](https://github.com/backblaze-labs/genblaze) — access Seedance, Kling, Veo, Sora, Wan, Seedream, FLUX, Gemini image, ElevenLabs, MiniMax and more through one API with SHA-256 provenance manifests.**

`genblaze-gmicloud` wraps GMICloud's request-queue API, giving you one-call access to a large catalog of video, image, and audio models — including Kling, Veo, Sora, Wan, Seedream, FLUX-Kontext-Pro, Gemini-2.5-Flash-Image, ElevenLabs TTS, MiniMax TTS, and MiniMax Music — via three genblaze provider classes. Compose into multi-step AI pipelines, persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store, and emit a tamper-evident provenance manifest on every run.

## Why genblaze-gmicloud

- **One API, dozens of models** — text-to-video (Seedance, Kling, Veo, Sora, Wan), text-to-image (Seedream, FLUX, Gemini, Reve), audio (ElevenLabs, MiniMax TTS/Music).
- **LLM access too** — standalone `chat()` wrapper for Llama, DeepSeek, Qwen over GMICloud's OpenAI-compatible inference endpoint (see below).
- **Provenance by default** — SHA-256-verified manifest with provider, model, prompt, params, cost.
- **Cost tracking** — register a pricing strategy from [`docs/reference/pricing-recipes.md`](https://github.com/backblaze-labs/genblaze/blob/main/docs/reference/pricing-recipes.md) and `step.cost_usd` is populated automatically.
- **Production-ready** — retries, timeouts, progress streaming, step caching.
- **Durable storage** — plug `genblaze-s3` in for Backblaze B2 / AWS S3 / R2 / MinIO persistence.

## Providers + models

| Provider class | Modality | Example models |
|---|---|---|
| `GMICloudVideoProvider` | video | `Kling-Text2Video-V2.1-Master`, `Kling-Image2Video-V2.1-Master`, `Veo3`, `wan2.6-t2v`, `seedance-1-0-pro-250528`, `sora-2-pro` |
| `GMICloudImageProvider` | image | `seedream-5.0-lite`, `gemini-2.5-flash-image`, `reve-edit-fast-20251030`, `flux-kontext-pro` |
| `GMICloudAudioProvider` | audio | `elevenlabs-tts-v3`, `minimax-tts-speech-2.6-turbo`, `minimax-music-2.5` |

Registered via entry points as `gmicloud`, `gmicloud-image`, and `gmicloud-audio`. Any model on GMICloud's queue is supported — pass the exact model slug.

> **Slug casing** — GMICloud's request queue is case-sensitive and uses **per-slug casing** (per their published catalog): lowercase for Sora / Pixverse / Seedance / Seedream / Reve / Wan / Bria / Gemini-Image, the audio families (`elevenlabs-tts-*`, `minimax-tts-*`, `minimax-music-*`, `minimax-audio-voice-clone-*`, `inworld-tts-*`), and the newer Kling V2.5 / V3 series; PascalCase for Kling V2.1 (`Kling-Text2Video-V2.1-Master`, `Kling-Image2Video-V2.1-Master`) and Veo3. As of `genblaze-gmicloud` 0.3.1, the connector **rewrites caller-supplied casing to GMICloud's published wire form** for the families above via the new `ModelFamily.canonical_slug` mechanism — so `model="ElevenLabs-TTS-v3"` and `model="veo3"` (legacy mixed/lower-case) keep working, with a one-time INFO per (family, input) nudging callers toward the canonical form. Slugs that don't match a known family still pass through verbatim; refer to [console.gmicloud.ai](https://console.gmicloud.ai/) as the source of truth when a model rotates.

## Install

```bash
pip install genblaze-gmicloud
```

## Quickstart — video (Kling)

```bash
pip install genblaze-core genblaze-gmicloud
export GMI_API_KEY="..."
```

```python
from genblaze_core import Modality, Pipeline
from genblaze_gmicloud import GMICloudVideoProvider

run, manifest = (
    Pipeline("gmicloud-video-demo")
    .step(GMICloudVideoProvider(), model="Kling-Text2Video-V2.1-Master",
          prompt="A drone shot flying over a misty mountain valley at sunrise, cinematic",
          modality=Modality.VIDEO, duration=10, aspect_ratio="16:9")
    .run(timeout=600)
)
print(run.steps[0].assets[0].url)
# step.cost_usd is None unless you've registered pricing — see
# docs/reference/pricing-recipes.md for the GMICloud rate sheet.
```

## Quickstart — image (Seedream)

```python
from genblaze_gmicloud import GMICloudImageProvider

run, manifest = (
    Pipeline("gmicloud-image-demo")
    .step(GMICloudImageProvider(), model="seedream-5.0-lite",
          prompt="A photorealistic macro shot of morning dew on a spider web, soft bokeh",
          modality=Modality.IMAGE, aspect_ratio="16:9")
    .run(timeout=120)
)
```

## Quickstart — audio (ElevenLabs via GMICloud)

```python
from genblaze_gmicloud import GMICloudAudioProvider

run, manifest = (
    Pipeline("gmicloud-audio-demo")
    .step(GMICloudAudioProvider(), model="elevenlabs-tts-v3",
          prompt="Welcome to Genblaze — the fastest way to build generative AI pipelines.",
          modality=Modality.AUDIO)
    .run(timeout=120)
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

[Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default sink — cost-efficient, S3-compatible, Object Lock for immutable manifests.

### Feeding a presigned B2 URL back in as a chain input

"Upload to B2, presign, feed the presigned URL to a model" is a common pattern — e.g. restoring a previously-generated image with a follow-up edit step. If you pass that `Asset` via `external_inputs=` without a `sha256`, you'll see a `WARNING` that the step cache key and manifest canonical hash will be unstable, because presigned URLs rotate (a re-run gets a different URL, and a naive cache/hash would treat it as different content even though the bytes are identical). Precompute the hash once and the warning goes away — the manifest hash then reflects the actual bytes, not the rotating URL:

```python
import hashlib
from genblaze_core.models.asset import Asset

# `data` is the bytes you already uploaded to B2 (or re-download once to hash).
sha256 = hashlib.sha256(data).hexdigest()

asset = Asset(url=presigned_url, sha256=sha256, media_type="image/png")
# pass via `external_inputs=[asset]` — the cache key and canonical hash are
# now stable across reruns even though `presigned_url` rotates.
```

## LLM access — standalone `chat()`

For callers driving a media pipeline from an LLM — caption expansion, prompt rewriting, scene description — `genblaze-gmicloud` ships a `chat()` callable over GMICloud's OpenAI-compatible inference endpoint. It sits **outside** the `Pipeline` / `Step` machinery (text generation doesn't benefit from the polling / manifest / asset machinery built for media).

```python
from genblaze_gmicloud import chat

resp = chat("deepseek-ai/DeepSeek-V3", prompt="A cinematic sunset over Tokyo")
print(resp.text, resp.tokens_out)
```

Any GMICloud-hosted chat model is accepted — model ids pass through to the inference endpoint verbatim, so you can use models the connector hasn't been updated for. `cost_usd` is always `None` for this connector; compute cost from `tokens_in` / `tokens_out` yourself if needed.

Full signature and `ChatResponse` shape: [`docs/features/llm-calls.md`](https://github.com/backblaze-labs/genblaze/blob/main/docs/features/llm-calls.md).

## Credentials

Only API-key auth is supported. Set `GMI_API_KEY` (obtain from https://console.gmicloud.ai/) or pass `api_key=` to any provider ctor or to `chat()`.

## Configuring the endpoint (staging, proxies, VPC)

**`GMI_BASE_URL` is queue-specific — it is read only by `GMICloudVideoProvider` / `GMICloudImageProvider` / `GMICloudAudioProvider` (all three accept a `base_url=` ctor kwarg too). `chat()` never reads `GMI_BASE_URL`** — it has its own default (GMICloud's OpenAI-compatible inference endpoint) and only takes an explicit `base_url=` argument. Setting `GMI_BASE_URL` to the inference/serving URL (`api.gmi-serving.com/v1`) to try to override both surfaces at once silently 404s every image/video/audio model while `chat()` keeps working — it looks exactly like an entitlement problem. The queue providers now reject any `base_url=`/`GMI_BASE_URL` that has this shape (path ending in `/v1`) with a clear `ProviderError` instead of a confusing wall of 404s (#193).

Both `GMICloudBase` subclasses and `chat()` also accept an `http_client=` kwarg for injecting a pre-built `httpx.Client` — useful for shared connection pools across multi-modality pipelines or for mocking in tests. It's also the escape hatch for the rare legitimate proxy that itself terminates at a bare `/v1` (e.g. it rewrites the queue path internally before forwarding): `base_url=`/`GMI_BASE_URL` reject that shape, but `http_client=` bypasses the check entirely since the base URL is baked into the client you hand it.

```python
import httpx
from genblaze_gmicloud import GMICloudVideoProvider, GMICloudImageProvider

shared = httpx.Client(
    base_url="https://my-vpc-proxy.example/gmi",
    headers={"Authorization": f"Bearer {key}"},
    timeout=120,
)
video = GMICloudVideoProvider(http_client=shared)
image = GMICloudImageProvider(http_client=shared)
# Caller owns `shared` — providers never close externally-supplied clients.
```

## Naming reference

GMICloud surfaces five related names; they look interchangeable but come from different namespaces:

| Surface | Value |
|---|---|
| PyPI package | `genblaze-gmicloud` |
| Python import | `import genblaze_gmicloud` |
| Provider class prefix | `GMICloud*` (e.g. `GMICloudVideoProvider`) |
| Entry-point slug | `gmicloud`, `gmicloud-image`, `gmicloud-audio` |
| Env vars | `GMI_API_KEY`, `GMI_BASE_URL` |

The `GMI_` env prefix is short on purpose; the class / import / PyPI names use the full `gmicloud` for precision and to leave room for future `genblaze-gmi*` packages if needed.

## Reading outputs safely

`step.assets[0]` is only valid when the step succeeded. Always check `step.status` first — especially in fan-out runs where one step may fail and others succeed:

```python
for step in run.steps:
    if step.status == "succeeded" and step.assets:
        print(step.assets[0].url)
    elif step.status == "failed":
        print(f"failed ({step.error_code}): {step.error}")
```

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Examples**: [`gmicloud_video_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/gmicloud_video_pipeline.py) · [`gmicloud_image_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/gmicloud_image_pipeline.py) · [`gmicloud_audio_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/gmicloud_audio_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on Backblaze B2 and other S3-compatible backends

## License

MIT
