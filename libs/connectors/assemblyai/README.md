<!-- last_verified: 2026-06-23 -->
# genblaze-assemblyai

[AssemblyAI](https://www.assemblyai.com/) **speech-to-text / transcription**
provider adapter for [genblaze](https://github.com/backblaze-labs/genblaze).

This connector is the inverse of every other genblaze adapter: instead of
*generating* media it *consumes* an audio URL and *produces* a text
transcript with word-level timings. The transcript lands as a hash-verified
**TEXT asset** with a provenance manifest, so it composes into pipelines
(generate audio → transcribe, caption a generated video) and persists to
Backblaze B2 / S3 like any other genblaze step.

## Install

```bash
pip install genblaze-assemblyai      # or: pip install "genblaze[assemblyai]"
export ASSEMBLYAI_API_KEY="..."      # from https://www.assemblyai.com/app/api-keys
```

## Usage

```python
from genblaze_core import Modality, Pipeline
from genblaze_assemblyai import AssemblyAIProvider

run, manifest = (
    Pipeline("transcribe")
    .step(
        AssemblyAIProvider(),
        model="universal-3-pro",                       # speech_models: universal-3-pro | universal-2
        prompt="https://example.com/podcast-episode.mp3",  # or params={"audio_url": ...}, or a chained audio input
        modality=Modality.TEXT,
        speaker_labels=True,                           # any TranscriptionConfig flag passes through
    )
    .run()
)

asset = run.steps[0].assets[0]
print(asset.metadata["text"])          # the transcript
print(asset.audio.word_timings[:3])    # [WordTiming(word=..., start=..., end=...), ...] (seconds)
print(manifest.canonical_hash)
```

The audio URL is resolved from (in priority order) `step.inputs[0].url` →
`step.params["audio_url"]` → `step.prompt`, then SSRF-validated (https:// or
file:// only) before submission. `step.model` selects the AssemblyAI speech
model (sent on the SDK's plural `speech_models` field — the live API retired
the singular `speech_model` field and the `best` / `nano` aliases; current
values are `universal-3-pro` / `universal-2`); every other
`TranscriptionConfig` flag (`speaker_labels`, `language_code`,
audio-intelligence options, …) passes through `step.params`.

## Pricing

The SDK ships no hardcoded prices. AssemblyAI bills per minute of *input*
audio; the connector captures the input duration in
`step.provider_payload["audio_duration"]` (seconds) during `fetch_output`.
Register a recipe at runtime — see
[`docs/reference/pricing-recipes.md`](https://github.com/backblaze-labs/genblaze/blob/main/docs/reference/pricing-recipes.md)
("AssemblyAI" section):

```python
from genblaze_core.providers import per_response_metric

RATE_PER_MINUTE = RATE  # replace RATE with the per-minute USD rate from assemblyai.com/pricing


def per_minute(ctx):
    seconds = ctx.provider_payload.get("audio_duration")
    return (seconds / 60.0) * RATE_PER_MINUTE if seconds is not None else None


provider = AssemblyAIProvider(api_key="...")
# Speech-model slugs match the connector's family, so register against the
# concrete slug(s) you use (register_pricing layers onto the family spec).
for slug in ("universal-3-pro", "universal-2"):
    provider.models.register_pricing(slug, per_response_metric(per_minute))
```

## Notes

- **Out of scope for v1:** real-time/streaming transcription, LeMUR
  (LLM-over-transcript), and first-class SRT/VTT subtitle outputs. Audio-
  intelligence flags pass through to the API and land in `metadata`.

## Docs

- [AssemblyAI docs](https://www.assemblyai.com/docs)
- [Transcript API reference](https://www.assemblyai.com/docs/api-reference/transcripts/get)
- genblaze [new-provider guide](https://github.com/backblaze-labs/genblaze/blob/main/docs/guides/new-provider.md)
