# genblaze-uberllm

UberLLM image and video generation adapter for
[genblaze](https://github.com/backblaze-labs/genblaze). One `UBERLLM_API_KEY`
reaches image and video models from many vendors (Flux, Seedream, Nano Banana,
Kling, Veo, Wan, Vidu and others), billed from one prepaid balance. The
connector keeps UberLLM fully opt-in.

## Install

```bash
pip install genblaze-uberllm
export UBERLLM_API_KEY="..."
```

## Usage

```python
from genblaze_core import Modality, Pipeline
from genblaze_uberllm import UberLLMProvider

run, manifest = (
    Pipeline("uberllm-image")
    .step(
        UberLLMProvider(),
        model="venice/z-image-turbo",
        prompt="A product photograph on a clean studio background",
        modality=Modality.IMAGE,
        size="1024x1024",
    )
    .run(timeout=300)
)
print(run.steps[0].assets[0].url, run.steps[0].cost_usd, manifest.canonical_hash)
```

Use `Modality.VIDEO` with a video model such as
`fal-ai/kling-video/v3/standard/text-to-video` and params like
`duration_seconds`, `resolution` and `aspect_ratio`. An image step with a
source picture (`image`, `image_url`, `input_images` or `images`) goes to the
edit route, so use an edit model such as `google/nano-banana-pro-edit`. Other
model-specific parameters are passed through unchanged after URL-bearing
inputs are validated.

Outputs are saved as local `file://` assets with a SHA-256 (set `output_dir`
to choose where). `step.cost_usd` is the cost UberLLM reports for the request.

## Reliability

Submission is never retried automatically because a failed response can still
represent a billable generation. Video job GET requests use a small bounded
retry with backoff for transient transport failures. Failed, canceled and
expired video jobs are not billed.

Current model IDs and prices are listed at
[uberllm.dev/models](https://uberllm.dev/models) and in
`GET https://api.uberllm.dev/v1/models?type=image` (or `?type=video`). Request
fields are described in the [media docs](https://uberllm.dev/docs/media).

## License

MIT
