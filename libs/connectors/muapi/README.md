# genblaze-muapi

[MuAPI](https://muapi.ai) image, video, and audio generation adapter for
[genblaze](https://github.com/backblaze-labs/genblaze). The connector reaches
MuAPI's live media catalog through its shared asynchronous model API instead of
shipping a second copy of the catalog.

## Install

```bash
pip install genblaze-muapi
export MUAPI_API_KEY="..."   # https://muapi.ai/access-keys
```

## Usage

```python
from genblaze_core import Modality, Pipeline
from genblaze_muapi import MuAPIProvider

run, manifest = (
    Pipeline("muapi-image")
    .step(
        MuAPIProvider(),
        model="flux-schnell",
        prompt="A product photograph on a clean studio background",
        modality=Modality.IMAGE,
    )
    .run(timeout=300)
)
print(run.steps[0].assets[0].url, manifest.canonical_hash)
```

`model` is any enabled MuAPI media model slug. The provider discovers the live
catalog from `GET /api/v1/models`, filters it to image/video/audio models, and
forwards model-specific fields unchanged to `POST /api/v1/{model}`. Use the
model detail endpoint or the [MuAPI API reference](https://muapi.ai/docs/api-reference)
to choose the payload for a model. The connector ships only a few examples for
documentation; model names are not hardcoded into the runtime catalog.

MuAPI returns a `request_id` immediately. The provider polls
`GET /api/v1/predictions/{request_id}/result` until the job completes, validates
each returned HTTPS output URL, and maps image, video, or audio outputs to
genblaze assets. `step.cost_usd` is populated when the response includes
MuAPI's reported `cost.amount_usd` value; unknown pricing remains unset.

## Inputs and safety

- The API key is sent as `x-api-key` and never placed in request payloads,
  manifests, or provider payloads.
- URL-bearing model inputs must be hosted `https://` URLs or inline `data:`
  URIs. Local `file://` paths are rejected because MuAPI cannot read the
  caller's filesystem; upload chained outputs to object storage first.
- Returned media must be absolute HTTPS URLs. HTTP, data, and malformed output
  URLs fail the step instead of becoming assets.
- Submission is not retried after an ambiguous response. Poll/result GETs use
  genblaze's normal bounded retry handling.

## License

MIT
