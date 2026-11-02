# genblaze-litellm

[LiteLLM](https://github.com/BerriAI/litellm) adapters for
[genblaze](https://github.com/backblaze-labs/genblaze): image, speech, and
video generation plus a `chat()` helper, each routed through the LiteLLM SDK
to any provider it supports (OpenAI, Azure, Vertex AI, Gemini, Bedrock,
ElevenLabs, RunwayML, Recraft, xAI, ...). Models are LiteLLM ids of the form
`<provider>/<model>`.

| Class / function | genblaze contract | LiteLLM call |
|---|---|---|
| `LiteLLMImageProvider` | `SyncProvider`, `Modality.IMAGE` | `litellm.image_generation` |
| `LiteLLMSpeechProvider` | `SyncProvider`, `Modality.AUDIO` | `litellm.speech` |
| `LiteLLMVideoProvider` | `submit` / `poll` / `fetch_output`, `Modality.VIDEO` | `litellm.video_generation`, `video_status`, `video_content` |
| `chat` / `achat` | `ChatResponse` | `litellm.completion` |

## Install

```bash
pip install genblaze-litellm
```

Credentials come from each provider's own env vars, the same ones LiteLLM
reads (`OPENAI_API_KEY`, `GEMINI_API_KEY`, AWS credentials for `bedrock/`,
`GOOGLE_APPLICATION_CREDENTIALS` + `VERTEXAI_PROJECT` for `vertex_ai/`, ...).
Pass `api_key=` / `api_base=` to override them, e.g. for an Azure deployment.

## Usage

```python
from genblaze_core import Modality, Pipeline
from genblaze_litellm import LiteLLMImageProvider, LiteLLMSpeechProvider, chat

script = chat("anthropic/claude-sonnet-4-6", prompt="One line of narration about the sea.")

run, manifest = (
    Pipeline("litellm-media")
    .step(
        LiteLLMImageProvider(),
        model="vertex_ai/imagen-4.0-generate-001",
        prompt="A lighthouse at dawn, watercolor",
        modality=Modality.IMAGE,
    )
    .step(
        LiteLLMSpeechProvider(),
        model="openai/gpt-4o-mini-tts",
        prompt=script.text,
        modality=Modality.AUDIO,
        voice="coral",
    )
    .run(timeout=300)
)
print([s.cost_usd for s in run.steps], manifest.canonical_hash)
```

`LiteLLMVideoProvider` works the same way with a video model such as
`openai/sora-2` or `gemini/veo-3.0-generate-preview` (`seconds` or `duration`,
`size`).

## Behavior

- Step params are forwarded with `drop_params=True`, so a param one provider
  does not accept is dropped instead of failing the step.
- `step.cost_usd` (and `ChatResponse.cost_usd`) is set from the cost LiteLLM
  computes for the response when LiteLLM has a price for the model; otherwise
  it stays `None` and `register_pricing` recipes apply as usual.
- Base64 images, speech audio, and video are written to local files with a
  SHA-256 hash; image URLs returned by a provider are SSRF-validated and kept
  as the asset URL for a storage sink to transfer.
- Video submission is a single attempt so an ambiguous failure never bills
  twice; status checks run on `poll()`.
- LiteLLM exceptions map to `ProviderErrorCode` (content policy, auth, rate
  limit, timeout, invalid input, model, server errors).
- LiteLLM's INFO logging and error banner are silenced unless `LITELLM_LOG`
  is set.

## License

MIT
