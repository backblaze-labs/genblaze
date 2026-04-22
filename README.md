<!-- last_verified: 2026-04-22 -->
<h1 align="center" style="border-bottom: none">
    Genblaze
</h1>
<h2 align="center" style="border-bottom: none">
    Pipeline SDK for AI generated video, audio and images with built-in provenance.
</h2>


<div align="center">

[![PyPI](https://img.shields.io/pypi/v/genblaze-core)](https://pypi.org/project/genblaze-core/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![CI](https://github.com/backblaze-labs/genblaze/actions/workflows/ci.yml/badge.svg)](https://github.com/backblaze-labs/genblaze/actions/workflows/ci.yml)

</div>

Genblaze is an AI pipeline SDK by [Backblaze](https://www.backblaze.com/sign-up/ai-cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) for building and orchestrating generative media workflows across video, image, and audio providers.

It provides a unified interface to experiment with providers like Runway, Luma, ElevenLabs, Stability Audio, and Hume AI, along with models available through platforms such as GMI Cloud, without rewriting pipeline logic.

Every output includes a SHA-256–verified provenance manifest capturing how the media was generated, with support for embedding metadata directly into files. Genblaze integrates with S3-compatible storage such as [Backblaze B2](https://www.backblaze.com/sign-up/ai-cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) to store and scale AI-generated media pipelines in production.

## Providers

genblaze ships with provider adapters for major generative AI platforms:

| | Video | Image | Audio |
|---|---|---|---|
| **GMICloud** | Seedance, Kling, Veo, Sora, Wan, etc. | Seedream, FLUX, Gemini, etc. | ElevenLabs, MiniMax TTS/Music |
| **OpenAI** | Sora | DALL-E / gpt-image family (2 / 1.5 / 1 / 1-mini) + edits | TTS |
| **Google** | Veo | Imagen | — |
| **Runway** | Gen-4 Turbo | — | — |
| **Luma** | Dream Machine | — | — |
| **Decart** | Lucy | Lucy | — |
| **Replicate** | — | Flux, SDXL, etc. | — |
| **ElevenLabs** | — | — | TTS + Sound Effects |
| **Stability AI** | — | — | Stable Audio (music) |
| **LMNT** | — | — | TTS |

## Features

- **Pipeline API** — Fluent, composable multi-step generation pipelines with fan-in (`input_from`) and AV compositing
- **10 provider adapters** — OpenAI, Google, Runway, Luma, Decart, Replicate, ElevenLabs, Stability Audio, LMNT, GMICloud
- **Manifest provenance** — Every run produces a canonical, SHA-256-verified manifest
- **Media embedding** — Embed manifests into PNG, JPEG, WebP, MP4, MP3, WAV
- **S3-compatible storage** — Upload assets to Backblaze B2, AWS S3, Cloudflare R2, MinIO
- **Policy system** — Redact prompts, strip params, pointer mode for privacy control
- **Parquet sink** — Write structured run/step/asset data to partitioned Parquet
- **CLI toolkit** — Extract, verify, replay, and index manifests from the command line

## Install

```bash
pip install genblaze-core

# Add providers
pip install genblaze-openai      # OpenAI (Sora, DALL-E, TTS)
pip install genblaze-google      # Google (Veo, Imagen)
pip install genblaze-runway      # Runway Gen video
pip install genblaze-luma        # Luma Dream Machine video
pip install genblaze-decart      # Decart Lucy video/image
pip install genblaze-replicate   # Replicate (Flux, SDXL, etc.)
pip install genblaze-elevenlabs  # ElevenLabs TTS + sound effects
pip install genblaze-stability-audio  # Stability AI Stable Audio
pip install genblaze-lmnt        # LMNT fast TTS
pip install genblaze-gmicloud    # GMICloud (video, image, audio via request queue)

# Add storage + CLI
pip install genblaze-s3          # S3-compatible storage (B2, AWS, R2)
pip install genblaze-cli         # CLI tools
```

## Configure API keys

Every provider reads its credentials from an environment variable. You
don't need all of them — just the ones whose providers you use.

| Provider | Env var(s) | Where to get it |
|---|---|---|
| **Backblaze B2** (storage) | `B2_KEY_ID`, `B2_APP_KEY` | [B2 Application Keys](https://secure.backblaze.com/app_keys.htm) |
| GMICloud | `GMI_API_KEY` | [console.gmicloud.ai](https://console.gmicloud.ai/) |
| OpenAI (Sora, DALL-E, TTS) | `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com/api-keys) |
| Google (Veo, Imagen) | `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com/apikey) |
| Runway (Gen video) | `RUNWAYML_API_SECRET` | [dev.runwayml.com](https://dev.runwayml.com/) |
| Luma (Dream Machine) | `LUMAAI_API_KEY` | [lumalabs.ai/dream-machine/api](https://lumalabs.ai/dream-machine/api) |
| Decart (Lucy) | `DECART_API_KEY` | [platform.decart.ai](https://platform.decart.ai/) |
| Replicate | `REPLICATE_API_TOKEN` | [replicate.com/account/api-tokens](https://replicate.com/account/api-tokens) |
| ElevenLabs (TTS + SFX) | `ELEVENLABS_API_KEY` | [elevenlabs.io/app/settings/api-keys](https://elevenlabs.io/app/settings/api-keys) |
| Stability AI (music) | `STABILITY_API_KEY` | [platform.stability.ai](https://platform.stability.ai/account/keys) |
| LMNT (fast TTS) | `LMNT_API_KEY` | [app.lmnt.com](https://app.lmnt.com/account) |

**Example — one provider + B2 storage:**

```bash
export GMI_API_KEY="gmi-..."
export B2_KEY_ID="..."
export B2_APP_KEY="..."
```

Or drop them into a `.env` file and source it:

```bash
# .env
GMI_API_KEY=gmi-...
B2_KEY_ID=...
B2_APP_KEY=...
```
```bash
set -a && source .env && set +a
```

You can also pass any key explicitly to the provider or backend
constructor (e.g. `GMICloudVideoProvider(api_key=...)`,
`S3StorageBackend.for_backblaze("my-bucket", key_id=..., app_key=...)`) —
the env var is just the default.

## Quickstart

End-to-end: generate a video, persist it + its provenance manifest to Backblaze B2, verify the hash.

```bash
pip install genblaze-core genblaze-gmicloud genblaze-s3

export GMI_API_KEY="gmi-..."
export B2_KEY_ID="..."
export B2_APP_KEY="..."
```

```python
from genblaze_core import Modality, ObjectStorageSink, KeyStrategy, Pipeline
from genblaze_gmicloud import GMICloudVideoProvider
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.HIERARCHICAL,
)

result = (
    Pipeline("my-first-pipeline")
    .step(
        GMICloudVideoProvider(),
        model="seedance-2-0-260128",
        prompt="A drone shot soaring over a coastal city at golden hour",
        modality=Modality.VIDEO,
        duration=10,
        aspect_ratio="16:9",
    )
    .run(sink=storage, timeout=600)
)

print(f"Asset URL: {result.run.steps[0].assets[0].url}")    # B2 durable URL
print(f"SHA-256:   {result.run.steps[0].assets[0].sha256}")
print(f"Manifest:  {result.manifest.manifest_uri}")         # Provenance JSON in B2
print(f"Hash:      {result.manifest.canonical_hash}")
print(f"Verified:  {result.manifest.verify()}")
```

The manifest captures the full provenance chain — provider, model, prompt, parameters, timestamps, and a canonical hash for integrity verification — and is uploaded alongside the asset. The asset URL is durable (credential-free, never expires), safe to store anywhere.

> Runnable copy of this example: [`examples/quickstart.py`](examples/quickstart.py). No API key? Try [`examples/quickstart_local.py`](examples/quickstart_local.py) — builds and verifies a manifest with zero external calls.

## Storage

Upload assets and manifests to any S3-compatible bucket with `sink=storage`. The sink handles asset transfer, manifest upload, and URL rewriting in a single operation.

[Backblaze B2](https://www.backblaze.com/sign-up/ai-cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default, offering reliable, cost-efficient storage for large AI-generated media with strong data integrity and resilient multipart uploads.

**Backblaze B2 is the recommended default** — one-liner, reads credentials from
`B2_KEY_ID` / `B2_APP_KEY`:

```python
from genblaze_core import Pipeline, Modality, ObjectStorageSink, KeyStrategy
from genblaze_openai import SoraProvider
from genblaze_s3 import S3StorageBackend

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.HIERARCHICAL,
)

result = Pipeline("my-pipeline").step(
    SoraProvider(),
    model="sora-2",
    prompt="Aerial flyover of a mountain lake at sunrise",
    modality=Modality.VIDEO,
).run(sink=storage, timeout=300)

print(f"Asset URL: {result.run.steps[0].assets[0].url}")  # Points to your B2 bucket
```

**Other S3-compatible providers** (AWS S3, Cloudflare R2, MinIO) — use the
generic constructor with an explicit `endpoint_url`:

```python
storage = ObjectStorageSink(
    S3StorageBackend(bucket="my-bucket", endpoint_url="https://..."),
    key_strategy=KeyStrategy.HIERARCHICAL,
)
```

**Cloud + Parquet analytics** — one sink does both:

```python
from genblaze_core import ParquetSink

storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.HIERARCHICAL,
    parquet_sink=ParquetSink("data/"),  # Also write structured data locally
)

result = Pipeline("full-pipeline").step(...).run(sink=storage)
# Assets + manifest in bucket, run/step/asset tables in data/ as Parquet
```

**Bucket layouts:**

```
HIERARCHICAL (run-grouped):             CONTENT_ADDRESSABLE (deduped):
{prefix}/runs/                           {prefix}/assets/
  {tenant}/{date}/{run_id}/                {sha256[:2]}/{sha256[2:4]}/{sha256}.ext
    manifest.json                        {prefix}/manifests/
    assets/                                {run_id}.json
      {asset_id}.mp4
```

See [docs/features/object-storage.md](docs/features/object-storage.md) for full configuration reference.

## Iteration

Link runs together to track prompt refinement, parameter tuning, and forking:

```python
# First attempt
v1 = Pipeline("hero-video").step(
    SoraProvider(), model="sora-2",
    prompt="product reveal on dark background", modality=Modality.VIDEO,
).run(timeout=300)

# Refine — linked to v1 via parent_run_id
v2 = Pipeline("hero-video").from_result(v1).step(
    SoraProvider(), model="sora-2",
    prompt="product reveal on dark background, dramatic lighting, slow motion",
    modality=Modality.VIDEO,
).run(timeout=300)

# Fork into a different provider
v3 = Pipeline("hero-video-runway").from_result(v1).step(
    RunwayProvider(), model="gen4_turbo",
    prompt="slow zoom in on the product", modality=Modality.VIDEO,
).run(timeout=300)
```

Every manifest carries a `parent_run_id` pointer (excluded from the canonical hash). See [docs/features/iteration.md](docs/features/iteration.md).

### More examples

Every example below uses the same `storage` sink — assets + manifests land in your Backblaze B2 bucket automatically.

```python
from genblaze_core import ObjectStorageSink, KeyStrategy
from genblaze_s3 import S3StorageBackend

# Reused across every pipeline — credentials from B2_KEY_ID / B2_APP_KEY
storage = ObjectStorageSink(
    S3StorageBackend.for_backblaze("my-bucket"),
    key_strategy=KeyStrategy.HIERARCHICAL,
)

# Multi-step: generate image then animate to video
from genblaze_core import Pipeline, Modality
from genblaze_gmicloud import GMICloudImageProvider, GMICloudVideoProvider

run, manifest = (
    Pipeline("image-to-video", chain=True)
    .step(GMICloudImageProvider(), model="Seedream-5.0-Lite", prompt="cyberpunk cityscape", modality=Modality.IMAGE)
    .step(GMICloudVideoProvider(), model="Kling-Image2Video-V2.1-Master", prompt="camera slowly pans right", modality=Modality.VIDEO)
    .run(sink=storage, timeout=600)
)

# Video with Luma Dream Machine
from genblaze_luma import LumaProvider

run, manifest = (
    Pipeline("luma-video")
    .step(LumaProvider(), model="ray-2", prompt="a cat playing piano", modality=Modality.VIDEO)
    .run(sink=storage, timeout=300)
)

# Generate speech with ElevenLabs
from genblaze_elevenlabs import ElevenLabsTTSProvider

run, manifest = (
    Pipeline("narration")
    .step(
        ElevenLabsTTSProvider(output_dir="output/"),
        model="eleven_v3",
        prompt="Welcome to the future of media provenance.",
        modality=Modality.AUDIO,
        voice_id="JBFqnCBsd6RMkjVDRZzb",
    )
    .run(sink=storage)
)

# Generate music with Stability Audio
from genblaze_stability_audio import StabilityAudioProvider

run, manifest = (
    Pipeline("soundtrack")
    .step(
        StabilityAudioProvider(output_dir="output/"),
        model="stable-audio-2.5",
        prompt="Epic orchestral trailer music with rising tension",
        modality=Modality.AUDIO,
        duration=60,
    )
    .run(sink=storage, timeout=120)
)
```

### Embed manifest into media files

```python
from pathlib import Path
from genblaze_core.media import Mp4Handler

mp4_path = Path("output/video.mp4")

handler = Mp4Handler()
handler.embed(mp4_path, manifest)

# Later, extract and verify
extracted = handler.extract(mp4_path)
assert extracted.verify()
```

### CLI

```bash
genblaze extract video.mp4          # Extract manifest from media
genblaze verify video.mp4           # Verify manifest integrity
genblaze replay manifest.json       # Preview a replay
genblaze index manifest.json -o ./  # Index into Parquet
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for full system layout and data flows.

```
libs/spec/              # Language-neutral JSON Schemas (v1/)
libs/core/              # genblaze-core Python SDK
libs/connectors/        # Provider adapters (openai, google, runway, luma, ...)
cli/                    # CLI tool
examples/               # Usage examples
```

### Key concepts

| Concept | Description |
|---------|-------------|
| **Pipeline** | Fluent API for composing multi-step generation workflows |
| **Run** | A collection of generation steps forming a pipeline execution |
| **Step** | A single generation operation (generate, upscale, transcode) |
| **Asset** | A generated media artifact with URL, MIME type, and optional hash |
| **Manifest** | Hash-verified canonical JSON document capturing full provenance |
| **Provider** | Adapter implementing submit/poll/fetch_output for a generation API |
| **Sink** | Output destination for structured run data (Parquet, object storage) |

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — System layout, data flows, canonical files
- [docs/features/](docs/features/) — Feature docs (pipeline, providers, media, policy, sinks)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines and [AGENTS.md](AGENTS.md) for repo conventions.

**Adding a new provider?** Provider adapters are the highest-leverage
contribution — each one expands what Genblaze pipelines can generate.
The [new-provider guide](docs/guides/new-provider.md) walks through
package setup, the `submit`/`poll`/`fetch_output` lifecycle, entry
points, error mapping, and the compliance test harness.

## License

[MIT](LICENSE)
