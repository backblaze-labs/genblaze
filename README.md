<!-- last_verified: 2026-04-25 -->
<h1 align="center" style="border-bottom: none">
    Genblaze
</h1>
<h2 align="center" style="border-bottom: none">
    Pipeline SDK for AI-generated video, audio, and images with built-in provenance.
</h2>


<div align="center">

[![PyPI](https://img.shields.io/pypi/v/genblaze-core)](https://pypi.org/project/genblaze-core/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![CI](https://github.com/backblaze-labs/genblaze/actions/workflows/ci.yml/badge.svg)](https://github.com/backblaze-labs/genblaze/actions/workflows/ci.yml)

</div>

**Genblaze** is an AI pipeline SDK by [Backblaze](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) for building and orchestrating generative media workflows across video, image, and audio.

A unified `Pipeline` API spans providers like OpenAI, Google, Runway, Luma, ElevenLabs, and Stability Audio, plus models served through platforms such as GMI Cloud and NVIDIA NIM (`build.nvidia.com`) — so you swap providers without rewriting orchestration. Every run produces a SHA-256–verified provenance manifest you can embed directly into media files (`.mp4`, `.png`, `.mp3`, …) and persist to [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) or any S3-compatible store.

## Why Genblaze

Genblaze sits between "call a single video API" and "run a media pipeline in production." The differentiators:

- **Provenance by default.** Every run yields a canonical, SHA-256-bound manifest — deterministic, embeddable into `.mp4 / .png / .jpg / .webp / .mp3 / .wav`, or persisted alongside the asset. Tamper-evident in trusted storage; pair with your own signer or C2PA when adversarial verification matters. See [trust modes](docs/features/trust-modes.md).
- **One pipeline, many providers.** Eleven adapters across video, image, audio, and chat behind a single `Pipeline` / `Step` API. Swap Sora → Runway → Veo by changing one line; chain text → image → video without re-plumbing.
- **Storage is first-class.** `S3StorageBackend.for_backblaze("bucket")` ships durable, credential-free asset URLs and content-addressable layouts. Designed for Backblaze B2; works against any S3-compatible store (AWS S3, Cloudflare R2, MinIO).
- **Fallback chains and conformance.** `fallback_models=[...]` retries on `MODEL_ERROR`; CI-grade `probe_models` and provider-contract tests catch upstream drift before users do.
- **Replayable runs.** Every manifest captures the full provenance — provider, model, prompt, params, timestamps — so a run can be reconstructed via `genblaze replay manifest.json` or by feeding the canonical params back into a Pipeline.

**Reach for something else when:**

- You only need an LLM chat loop → use the provider's SDK or LangChain.
- You're building a UI-driven generation app in JS/TS → use the Vercel AI SDK.
- You're not generating media or don't care about provenance → the provider's SDK directly is simpler.

## Install

```bash
pip install genblaze                  # core + B2/S3 storage (the umbrella)
pip install "genblaze[gmicloud]"      # + GMICloud provider
pip install "genblaze[video]"         # + curated video bundle
pip install "genblaze[all]"           # + every provider
```

The umbrella pulls in `genblaze-core` (pipeline + models) and `genblaze-s3` (Backblaze B2 / S3 storage) so you have a working provenance pipeline out of the box. Provider adapters are opt-in extras.

Install packages individually if you prefer:

```bash
pip install genblaze-core            # Pipeline, Step, Run, Manifest, sinks, tracers
pip install genblaze-s3              # S3-compatible storage (B2, AWS, R2, MinIO)
pip install genblaze-cli             # CLI: extract, verify, replay, index

# Provider adapters
pip install genblaze-openai          # OpenAI: Sora, DALL-E / gpt-image, TTS, chat
pip install genblaze-google          # Google: Veo, Imagen, chat
pip install genblaze-nvidia          # NVIDIA NIM: Cosmos, SDXL/FLUX, Fugatto, Riva, chat
pip install genblaze-gmicloud        # GMICloud: video, image, audio, chat (request queue)
pip install genblaze-runway          # Runway Gen video
pip install genblaze-luma            # Luma Dream Machine video
pip install genblaze-decart          # Decart Lucy video / image
pip install genblaze-replicate       # Replicate (Flux, SDXL, etc.)
pip install genblaze-elevenlabs      # ElevenLabs TTS + sound effects
pip install genblaze-stability-audio # Stability AI Stable Audio (music)
pip install genblaze-lmnt            # LMNT fast TTS
```

Install names use hyphens, Python imports use underscores: `pip install genblaze-<name>` → `import genblaze_<name>`.

TypeScript types for the manifest schema are published on npm:

```bash
npm install @genblaze/spec
```

## Quickstart

End-to-end: generate a video, persist it and its provenance manifest to Backblaze B2, verify the hash.

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

> Runnable copy: [`examples/quickstart.py`](examples/quickstart.py). No API key? Try [`examples/quickstart_local.py`](examples/quickstart_local.py) — builds and verifies a manifest with zero external calls.

## Concepts

| Primitive | Description |
|---|---|
| **`Pipeline`** | Fluent, composable multi-step generation workflow with sync, async, and streaming runners. Supports fan-in (`input_from`), fallback chains, and AV compositing. |
| **`Step`** | A single generation operation — provider, model, prompt, params, retry budget, fallback chain, cost. |
| **`Run`** | A pipeline execution: collection of steps with shared `run_id`, `tenant_id`, and `parent_run_id` for lineage. |
| **`Asset`** | Generated media artifact with durable URL, SHA-256, MIME type, duration, and per-modality metadata. |
| **`Manifest`** | Canonical, hash-verified provenance document — embeddable into MP4 / PNG / JPEG / WebP / MP3 / WAV. |
| **`Provider`** | Adapter implementing the `submit / poll / fetch_output` lifecycle for a generation API. |
| **`ModelRegistry`** | Per-provider store of model specs (pricing, param rules, input routing). Extensible at runtime. |
| **`Sink`** | Output destination — `ObjectStorageSink` (B2/S3/R2/MinIO), `ParquetSink`, `WebhookSink`. |
| **`Tracer`** | Observability hook — `LoggingTracer`, `OTelTracer`, `LangSmith`, custom. |
| **`AgentLoop`** | Iterative refinement loop with parent-linked runs. |
| **`EmbedPolicy`** | Manifest privacy controls — redact prompts, strip params, pointer-mode sidecars. |

## Providers

Genblaze ships adapters for major generative AI platforms. The matrix below is the single reference for **what each connector can do**. The first three columns describe Provider classes integrated with `Pipeline` / `Step` / manifest; the **Chat (LLM)** column is a standalone `chat()` callable outside the Pipeline machinery — a convenience for driving media steps from an LLM, not a Pipeline citizen (see [`docs/features/llm-calls.md`](docs/features/llm-calls.md)).

<!-- Update when adding a provider, new modality, or shipping chat() for a connector. -->

|  | Video | Image | Audio | Chat (LLM) |
|---|---|---|---|---|
| **GMICloud** | Seedance, Kling, Veo, Sora, Wan, etc. | Seedream, FLUX, Gemini, etc. | ElevenLabs, MiniMax TTS / Music | `chat()` — Llama, DeepSeek, Qwen |
| **NVIDIA NIM** | Cosmos 1.0 / 2.0 (diffusion, text2world / video2world) | SDXL, SD 3.5, FLUX.1, FLUX.2 | Fugatto, Riva TTS, Maxine | `chat()` — Nemotron, Llama, Mistral, Qwen, Phi |
| **OpenAI** | Sora | DALL-E / gpt-image family (2 / 1.5 / 1 / 1-mini) + edits | TTS | `chat()` — GPT-4o / GPT-4.1 / o-series |
| **Google** | Veo | Imagen | — | `chat()` — Gemini 1.5 / 2.0 / 2.5 |
| **Runway** | Gen-4 Turbo | — | — | — |
| **Luma** | Dream Machine | — | — | — |
| **Decart** | Lucy | Lucy | — | — |
| **Replicate** | — | Flux, SDXL, etc. | — | — |
| **ElevenLabs** | — | — | TTS + Sound Effects | — |
| **Stability AI** | — | — | Stable Audio (music) | — |
| **LMNT** | — | — | TTS | — |

## Configure API keys

Every provider reads its credentials from an environment variable. You don't need all of them — just the ones whose providers you use.

| Provider | Env var(s) | Where to get it |
|---|---|---|
| **Backblaze B2** (storage) | `B2_KEY_ID`, `B2_APP_KEY` (optional: `B2_BUCKET`, `B2_REGION`) | [B2 Application Keys](https://secure.backblaze.com/app_keys.htm) |
| GMICloud | `GMI_API_KEY` | [console.gmicloud.ai](https://console.gmicloud.ai/) |
| NVIDIA NIM (Cosmos, SDXL/FLUX, Fugatto, chat) | `NVIDIA_API_KEY` | [build.nvidia.com](https://build.nvidia.com/) |
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

You can also pass any key explicitly to the provider or backend constructor (e.g. `GMICloudVideoProvider(api_key=...)`, `S3StorageBackend.for_backblaze("my-bucket", key_id=..., app_key=...)`) — the env var is just the default.

## Custom models & pricing

Every provider exposes a `ModelRegistry` you can extend at runtime. Use any model the provider supports — even ones Genblaze hasn't shipped defaults for — and plug in your own pricing.

```python
from genblaze_core.providers import ModelSpec, per_unit
from genblaze_openai import DalleProvider

# Override pricing on a known model (e.g. your volume-discount rate)
reg = DalleProvider.models_default().fork()
reg.register_pricing("dall-e-3", per_unit(0.050))

# Register a brand-new model the library hasn't seen yet
reg.register(ModelSpec(model_id="gpt-image-3-preview", pricing=per_unit(0.20)))

provider = DalleProvider(models=reg)
```

Unknown models pass through by default (`cost_usd=None` until registered). No release required to adopt a newly-released model. See [`docs/features/model-registry.md`](docs/features/model-registry.md) for param aliases, schemas, and input routing.

## Storage

Upload assets and manifests to any S3-compatible bucket with `sink=storage`. The sink handles asset transfer, manifest upload, and URL rewriting in a single operation.

[Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) is the recommended default — one-liner, reads credentials from `B2_KEY_ID` / `B2_APP_KEY`. Bucket and region can also come from `B2_BUCKET` / `B2_REGION` so everything lives in `.env`:

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

**Other S3-compatible providers** (AWS S3, Cloudflare R2, MinIO) — use the generic constructor with an explicit `endpoint_url`:

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

## More examples

Every example below uses the same `storage` sink — assets and manifests land in your Backblaze B2 bucket automatically.

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
    .step(GMICloudImageProvider(), model="seedream-5.0-lite", prompt="cyberpunk cityscape", modality=Modality.IMAGE)
    .step(GMICloudVideoProvider(), model="kling-image2video-v2.1-master", prompt="camera slowly pans right", modality=Modality.VIDEO)
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

## Runtime

- **Language:** Python 3.11+. Full async support — `Pipeline.arun()`, `Pipeline.astream()`, `Pipeline.abatch_run()`.
- **Distribution:** Library-only — no daemon, no service to run. Embeds into FastAPI handlers, AWS Lambda, Cloud Run, Modal, notebooks, scripts.
- **State:** Stateless by design. Persistence is your storage backend (B2 / S3) and your manifest store. No session affinity or shared scheduler required.
- **TypeScript:** Manifest schema is published as [`@genblaze/spec`](https://www.npmjs.com/package/@genblaze/spec) with generated `.d.ts` types from the JSON Schemas — TS consumers stay in sync with Python models automatically (see [`libs/spec/README.md`](libs/spec/README.md)).

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for full system layout, data flows, and canonical files.

```
libs/spec/              # Language-neutral contract: JSON Schemas + generated TS types
libs/core/              # genblaze-core Python SDK
libs/connectors/        # Provider adapters (openai, google, runway, luma, ...)
cli/                    # CLI tool
examples/               # Usage examples
```

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — System layout, data flows, canonical files
- [docs/features/model-registry.md](docs/features/model-registry.md) — Register models, override pricing, customize parameters
- [docs/features/](docs/features/) — Feature docs (pipeline, providers, media, policy, sinks, observability, agents)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines and [AGENTS.md](AGENTS.md) for repo conventions.

**Adding a new provider?** Provider adapters are the highest-leverage contribution — each one expands what Genblaze pipelines can generate. The [new-provider guide](docs/guides/new-provider.md) walks through package setup, the `submit`/`poll`/`fetch_output` lifecycle, entry points, error mapping, and the compliance test harness.

## License

[MIT](LICENSE)
