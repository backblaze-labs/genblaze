# genblaze-fal

[fal.ai](https://fal.ai) image, video, and audio generation adapter for
[genblaze](https://github.com/backblaze-labs/genblaze). One connector reaches
fal's whole model catalog through its
[queue API](https://fal.ai/docs/documentation/model-apis/inference/queue),
using plain HTTP (no `fal-client` dependency).

## Install

```bash
pip install genblaze-fal
export FAL_KEY="..."   # https://fal.ai/dashboard/keys
```

## Usage

```python
from genblaze_core import Modality, Pipeline
from genblaze_fal import FalProvider

run, manifest = (
    Pipeline("fal-image")
    .step(
        FalProvider(),
        model="fal-ai/flux/schnell",
        prompt="A product photograph on a clean studio background",
        modality=Modality.IMAGE,
        image_size="square_hd",
    )
    .run(timeout=300)
)
print(run.steps[0].assets[0].url, manifest.canonical_hash)
```

`model` is any fal endpoint id. Model-specific inputs are forwarded unchanged,
so check the model's API page (for example
[FLUX.1 schnell](https://fal.ai/models/fal-ai/flux/schnell/api)) for its input
schema. The connector ships starter defaults for a few well-known endpoints:

| Endpoint | Modality | Notes |
|---|---|---|
| `fal-ai/flux/schnell`, `fal-ai/flux/*` | image | FLUX family |
| `fal-ai/wan/v2.2-a14b/text-to-video`, `fal-ai/wan/*-to-video` | video | Wan family |
| `fal-ai/stable-audio` | audio | standard `duration` maps to `seconds_total` |

Any other endpoint id passes through as-is. Outputs are mapped to assets by
media type, using fal's `content_type` when present and the output key
(`images`, `video`, `audio_file`, and so on) or URL extension otherwise.
Chained inputs are routed to fal's conventional `image_url`, `video_url`, and
`audio_url` slots.

## Inputs and safety

- URL-bearing inputs (`image`, `video`, `audio`, and any `*_url` / `*_urls`
  param) must be `https://` URLs or inline `data:` URIs. Local `file://`
  paths are rejected because fal cannot read them. Upload them (for example to
  B2) first.
- `sync_mode` is rejected because it returns inline data instead of hosted
  media URLs.
- The API key is sent only in the `Authorization` header, and only to the
  configured queue host. It never appears in step params, provider payloads,
  or manifests.

## Reliability

Submission is never retried automatically, because an ambiguous failure can
still represent a billable generation. Status and result GETs retry transient
failures (timeouts, connection errors, 429/502/503) a small, bounded number
of times.

Status and result URLs are tracked by the `FalProvider` instance that
submitted the request, so `resume()` must use that same instance.

## License

MIT
