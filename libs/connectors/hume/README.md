<!-- last_verified: 2026-06-15 -->
# genblaze-hume

[Hume AI](https://www.hume.ai/) **Octave TTS** provider adapter for
[genblaze](https://github.com/backblaze-labs/genblaze).

Octave is an LLM-based text-to-speech model. This connector exposes it as a
synchronous audio provider — generate speech, get a provenance manifest, and
persist the result to Backblaze B2 / S3 like any other genblaze step.

## Install

```bash
pip install genblaze-hume          # or: pip install "genblaze[hume]"
export HUME_API_KEY="..."          # from https://platform.hume.ai/
```

## Usage

```python
from genblaze_core import Modality, Pipeline
from genblaze_hume import HumeTTSProvider

run, manifest = (
    Pipeline("narration")
    .step(
        HumeTTSProvider(output_dir="output/"),
        model="octave-2",
        prompt="Welcome to the future of media provenance.",
        modality=Modality.AUDIO,
        voice_id="Ava Song",          # a Hume Voice Library voice name
        output_format="mp3",          # mp3 | wav | pcm
    )
    .run()
)

print(manifest.canonical_hash)
```

`step.model` selects the Octave version: `octave-2` → API `version="2"`
(multi-language, requires a voice), `octave-1` → `version="1"`.

## Pricing

The SDK ships no hardcoded prices. Octave bills per character of input text;
register a recipe at runtime — see
[`docs/reference/pricing-recipes.md`](https://github.com/backblaze-labs/genblaze/blob/main/docs/reference/pricing-recipes.md)
("Hume" section):

```python
from genblaze_core.providers import per_input_chars

provider = HumeTTSProvider(api_key="...")
provider.models.register_pricing("octave-2", per_input_chars(RATE, per=1000))
```

## Docs

- [Octave TTS overview](https://dev.hume.ai/docs/text-to-speech-tts/overview)
- [synthesize-json reference](https://dev.hume.ai/reference/text-to-speech-tts/synthesize-json)
- genblaze [new-provider guide](https://github.com/backblaze-labs/genblaze/blob/main/docs/guides/new-provider.md)
