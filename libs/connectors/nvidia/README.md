<!-- last_verified: 2026-04-24 -->
# genblaze-nvidia

NVIDIA NIM / [build.nvidia.com](https://build.nvidia.com/models) provider adapters for
[genblaze](https://github.com/backblaze-labs/genblaze). Covers four modalities on one
`nvapi-` key: **video** (Cosmos, Edify), **image** (SDXL, SD 3.5, FLUX), **audio**
(Fugatto, Riva TTS), and **chat** (Nemotron, Llama, Mistral, Qwen, …).

## Install

```bash
pip install genblaze-nvidia            # video/image/audio providers
pip install "genblaze-nvidia[chat]"    # + the OpenAI SDK for LLM calls
```

## Auth

Sign up at [build.nvidia.com](https://build.nvidia.com) and create an API key
(starts with `nvapi-`). Export it:

```bash
export NVIDIA_API_KEY=nvapi-...
```

The free tier is rate-limited (~40 requests/minute per model) with no per-token
billing. Some models (Cosmos video) are still enterprise-gated as of
2026-04 and will return `AUTH_FAILURE` for free-tier keys until you have access.

## Two base URLs

NVIDIA's API spans two public hosts on the same key:

| Surface | Base URL | Used by |
|---|---|---|
| OpenAI-compatible chat / embeddings | `https://integrate.api.nvidia.com/v1` | `chat`, `achat` |
| Model-specific generation | `https://ai.api.nvidia.com/v1/genai/{vendor}/{slug}` | `NvidiaVideoProvider`, `NvidiaImageProvider`, `NvidiaAudioProvider` |
| NVCF async status | `https://api.nvcf.nvidia.com/v2/nvcf/pexec/status` | Async video polling |

All three are overridable per-constructor for self-hosted NIM deployments:

```python
NvidiaImageProvider(
    api_key="...",
    gen_base_url="https://self-hosted.internal/v1",
    nvcf_status_url="https://self-hosted.internal/v2/nvcf/pexec/status",
)
```

## Video — `NvidiaVideoProvider`

Cosmos and Edify Video return async (`202 Accepted` + `NVCF-REQID` header) and
the provider polls NVCF for completion. Some fast models return inline
synchronous responses — both paths converge on the same lifecycle.

```python
from genblaze_core.models.step import Step
from genblaze_nvidia import NvidiaVideoProvider

provider = NvidiaVideoProvider()  # reads NVIDIA_API_KEY
step = Step(
    provider="nvidia-video",
    model="nvidia/cosmos-1.0-7b-diffusion-text2world",
    prompt="a drone flight over a coastal cliff at sunset",
)
result = provider.invoke(step)
print(result.assets[0].url)  # file:// or https:// depending on response shape
```

## Image — `NvidiaImageProvider`

Synchronous inline base64 response. If an endpoint occasionally returns 202,
the provider short-polls NVCF inside `generate()` so the caller still sees
one blocking call.

```python
from genblaze_nvidia import NvidiaImageProvider

provider = NvidiaImageProvider()
step = Step(
    provider="nvidia-image",
    model="stabilityai/stable-diffusion-3-5-large",
    prompt="a studio photo of a brass teapot",
    params={"cfg_scale": 4.5, "aspect_ratio": "1:1"},
)
result = provider.invoke(step)
```

SDXL's schema differs from SD 3.5 / FLUX — the registry handles that
transparently, rewriting `prompt` + `negative_prompt` into the
`text_prompts` array SDXL expects.

## Audio — `NvidiaAudioProvider`

```python
from genblaze_nvidia import NvidiaAudioProvider

provider = NvidiaAudioProvider()

# TTS (mono)
step = Step(provider="nvidia-audio", model="nvidia/riva-tts", prompt="Hello, world.")

# Music / SFX (stereo)
step = Step(provider="nvidia-audio", model="nvidia/fugatto", prompt="upbeat synthwave intro")

result = provider.invoke(step)
```

## Chat — `chat` / `achat`

OpenAI-wire-compatible. Any model NIM currently serves works as a plain string —
no enumeration.

```python
from genblaze_nvidia import chat

resp = chat(
    "nvidia/nemotron-4-340b-instruct",
    prompt="Summarize the Cosmos world foundation model in one sentence.",
)
print(resp.text)
```

```python
import asyncio
from genblaze_nvidia import achat

async def main():
    r = await achat("meta/llama-3.3-70b-instruct", prompt="hi")
    print(r.text)

asyncio.run(main())
```

### Structured outputs

Pass a Pydantic class to `response_format=` and the JSON Schema is generated
automatically (NIM, OpenAI, GMICloud all speak the same `json_schema` envelope).

```python
from pydantic import BaseModel
from genblaze_nvidia import chat

class Summary(BaseModel):
    title: str
    key_points: list[str]

resp = chat(
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    prompt="Summarize: NVIDIA shipped a 30B-A3B omnimodal model.",
    response_format=Summary,
)
import json; obj = Summary.model_validate(json.loads(resp.text))
```

## Chat as a Pipeline step — `NvidiaChatProvider`

Drop NIM chat into a Pipeline alongside generation steps. Multimodal input
flows through `step.inputs[Asset]` — each asset becomes the right OpenAI-vision
content block based on its `media_type`. Verified against the Nemotron 3 Nano
Omni model card on build.nvidia.com.

```python
from genblaze_core import Asset, Pipeline
from genblaze_nvidia import NvidiaChatProvider

# Eyes-and-ears: image + audio + video in one step.
inputs = [
    Asset(url="https://example.com/scene.png",  media_type="image/png"),
    Asset(url="https://example.com/voice.wav",  media_type="audio/wav"),
    Asset(url="https://example.com/clip.mp4",   media_type="video/mp4"),
]

pipe = Pipeline().input(*inputs).step(
    NvidiaChatProvider(reasoning=False),  # turn off thinking for a fast perception pass
    model="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    prompt="Describe what's happening across these inputs.",
    input_from=[-1],
)
result = pipe.run()
print(result.steps[-1].assets[0].metadata["text"])
```

`reasoning` is tri-state: `None` (default) lets the server pick based on the
model checkpoint, `True`/`False` overrides explicitly via
`extra_body["chat_template_kwargs"]["enable_thinking"]`. Tuning fields like
`media_io_kwargs={"video": {"fps": 3.0}}` and
`mm_processor_kwargs={"max_num_tiles": 3}` are passed through to NIM untouched.

PDFs are not natively supported — Nemotron Omni processes documents as
multi-page image sequences upstream, so callers must rasterize pages
client-side and pass each one as `Asset(media_type="image/png")`.

## Models

Curated entries encode per-model behavior (SDXL's `text_prompts` shape,
canonical→native param aliases). Any model NVIDIA ships that isn't listed
still works via the permissive fallback spec — no release needed.

| Modality | Curated defaults |
|---|---|
| Video | `nvidia/cosmos-1.0-7b-diffusion-text2world`, `.../video2world`, `nvidia/cosmos-2.0-diffusion-*` |
| Image | `stabilityai/stable-diffusion-xl`, `.../stable-diffusion-3-5-{large,large-turbo,medium}`, `black-forest-labs/flux.1-{schnell,dev}` |
| Audio | `nvidia/fugatto`, `nvidia/riva-tts`, `nvidia/maxine-voice-font` |
| Chat | None — pure pass-through. Use any NIM model id. |

Discover live models at runtime (if you want the fresh catalog) via the
OpenAI-compatible `/v1/models` endpoint:

```python
import httpx, os
r = httpx.get(
    "https://integrate.api.nvidia.com/v1/models",
    headers={"Authorization": f"Bearer {os.environ['NVIDIA_API_KEY']}"},
)
for m in r.json()["data"]:
    print(m["id"])
```

## Error handling

NIM returns safety refusals as HTTP 400 with `Nemoguard` / safety markers in
the body. `map_nvidia_error` classifies these as `CONTENT_POLICY`
(non-retryable) instead of `INVALID_INPUT` — pipelines don't burn retries on
a deterministic refusal.

| HTTP / message | `ProviderErrorCode` |
|---|---|
| 401, 403 | `AUTH_FAILURE` |
| 404 | `MODEL_ERROR` |
| 429 | `RATE_LIMIT` |
| 400 with safety marker | `CONTENT_POLICY` |
| 400 plain | `INVALID_INPUT` |
| 5xx | `SERVER_ERROR` |
| transport timeout | `TIMEOUT` |
