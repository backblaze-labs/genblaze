<!-- last_verified: 2026-09-03 -->
# genblaze-google

**Google provider adapters for [genblaze](https://github.com/backblaze-labs/genblaze) — [Veo](https://deepmind.google/technologies/veo/) text-to-video and Imagen/Gemini text-to-image — with SHA-256 provenance manifests on every output.**

`genblaze-google` wraps Google's generative media models (Veo 2, Veo 3, Imagen 4, Gemini-native image) as genblaze providers via the unified `google-genai` SDK. Works with both Gemini API keys and Google Cloud Vertex AI authentication. Compose Veo/Imagen/Gemini-image calls into multi-step AI pipelines, persist outputs to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store, and emit a tamper-evident provenance manifest for every run.

## Why genblaze-google

- **Veo 3 with synchronized audio** — text-to-video + native audio, wrapped in a provenance manifest.
- **Imagen 4 high-fidelity images** — photorealistic stills with full parameter tracking.
- **Gemini-native image generation** — the only image path callable on a freshly created Gemini API key (Imagen requires account entitlement — see caveat below).
- **Two auth modes** — Gemini API (`GEMINI_API_KEY`) for quick start, Vertex AI for enterprise / GCP orgs.
- **Same SDK, any provider** — swap to Sora, Runway, Luma, Flux without rewriting pipeline logic.
- **Provenance by default** — SHA-256 hash + canonical manifest on every generation.
- **Durable storage** — plug `genblaze-s3` in for Backblaze B2 / AWS S3 / Cloudflare R2 / MinIO.

## Providers + models

| Provider class | Modality | Models |
|---|---|---|
| `VeoProvider` | video | `veo-3.0-generate-001` (with audio), `veo-3.0-fast-generate-001`, `veo-2.0-generate-001` |
| `ImagenProvider` | image | `imagen-4.0-generate-001`, `imagen-4.0-fast-generate-001` — catalog-listed but entitlement-gated for new Gemini API keys (see caveat below) |
| `GeminiImageProvider` | image | `gemini-2.5-flash-image`, `gemini-3.1-flash-image` — no entitlement gate; works on a freshly created key |

Each is registered via entry points (`google-veo`, `google-imagen`, `google-gemini-image`).

## Install

```bash
pip install genblaze-google
```

## Quickstart — Veo 3 text-to-video (with audio)

```bash
export GEMINI_API_KEY="..."   # or use Vertex AI auth
```

```python
from genblaze_core import Modality, Pipeline
from genblaze_google import VeoProvider

run, manifest = (
    Pipeline("veo-demo")
    .step(VeoProvider(), model="veo-3.0-generate-001",
          prompt="A time-lapse of a coral reef coming to life, colorful fish "
                 "swimming through vibrant coral, natural ocean lighting",
          modality=Modality.VIDEO,
          aspect_ratio="16:9", duration_seconds="8", resolution="720p",
          enhance_prompt=True)
    .run(timeout=600)
)
print(run.steps[0].assets[0].url, manifest.canonical_hash)
```

Vertex AI auth instead:

```python
provider = VeoProvider(project="my-gcp-project", location="us-central1")
```

Vertex returns generated video bytes inline (no Files API on Vertex), so
`fetch_output()` saves them to a local file and exposes a `file://` asset —
pass `output_dir` to control where those files land (default: system temp),
same as `ImagenProvider` below.

## Quickstart — Imagen 4 text-to-image

```python
from genblaze_google import ImagenProvider

run, manifest = (
    Pipeline("imagen-demo")
    .step(ImagenProvider(output_dir="output/images"),
          model="imagen-4.0-generate-001",
          prompt="A photorealistic aerial view of a coral reef teeming with tropical fish",
          modality=Modality.IMAGE, aspect_ratio="16:9")
    .run(timeout=120)
)
```

> **Entitlement caveat:** `imagen-4.0-*` is catalog-listed, so it passes
> preflight, but Google gates `:predict` access behind account entitlement
> that isn't visible until you actually try to generate — a freshly created
> Gemini API key can still get a 404 here. If that happens, use
> `GeminiImageProvider` below instead; it needs no such entitlement.

## Quickstart — Gemini-native image generation

```python
from genblaze_google import GeminiImageProvider

run, manifest = (
    Pipeline("gemini-image-demo")
    .step(GeminiImageProvider(output_dir="output/images"),
          model="gemini-2.5-flash-image",
          prompt="A photorealistic aerial view of a coral reef teeming with tropical fish",
          modality=Modality.IMAGE)
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
# pass sink=storage to .run(…) to push assets + manifest to B2
```

[Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default sink for genblaze — cost-efficient, S3-compatible, with Object Lock for tamper-evident manifests.

## Credentials

| Auth mode | Env var / config |
|---|---|
| Gemini API (quickest) | `GEMINI_API_KEY` — https://aistudio.google.com/apikey |
| Vertex AI | `VeoProvider(project=..., location=...)` + `gcloud auth application-default login` |

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Examples**: [`veo_video_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/veo_video_pipeline.py) · [`imagen_pipeline.py`](https://github.com/backblaze-labs/genblaze/blob/main/examples/imagen_pipeline.py)

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on Backblaze B2 and other S3-compatible backends

## License

MIT
