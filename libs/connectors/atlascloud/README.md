# genblaze-atlascloud

Atlas Cloud image and video generation adapter for
[genblaze](https://github.com/backblaze-labs/genblaze). The connector uses the
Atlas Cloud asynchronous prediction API and keeps Atlas Cloud fully opt-in.

## Install

```bash
pip install genblaze-atlascloud
export ATLASCLOUD_API_KEY="..."
```

## Usage

```python
from genblaze_atlascloud import AtlasCloudProvider
from genblaze_core import Modality, Pipeline

run, manifest = (
    Pipeline("atlascloud-image")
    .step(
        AtlasCloudProvider(),
        model="bytedance/seedream-v5.0-lite",
        prompt="A product photograph on a clean studio background",
        modality=Modality.IMAGE,
        size="2048*2048",
        output_format="png",
    )
    .run(timeout=300)
)
print(run.steps[0].assets[0].url, manifest.canonical_hash)
```

Use `Modality.VIDEO` with a video model such as
`bytedance/seedance-2.0/text-to-video`. Model-specific parameters are passed
through unchanged after URL-bearing inputs are validated.

## Reliability

Submission is never retried automatically because a failed response can still
represent a billable generation. Prediction GET requests use a small bounded
retry with backoff for transient transport failures.

Current model IDs and schemas are available from the
[Atlas Cloud model catalog](https://www.atlascloud.ai/models).

## License

MIT
