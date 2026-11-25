# genblaze-anex

[anex.sh](https://anex.sh) video generation adapter for
[genblaze](https://github.com/backblaze-labs/genblaze). It covers MiniMax H3
text to video, first and last frame, reference to video and video editing,
plus upscaling of a finished clip, through anex.sh's MiniMax-compatible
[task API](https://anex.sh/docs/video) over plain HTTP.

## Install

```bash
pip install genblaze-anex
export ANEX_API_KEY="..."   # the Keys page of the console at https://anex.sh
```

## Usage

```python
from genblaze_anex import AnexProvider
from genblaze_core import Modality, Pipeline

run, manifest = (
    Pipeline("anex-video")
    .step(
        AnexProvider(),
        model="MiniMax-H3",
        prompt="A red fox trots across fresh snow at dawn, its breath steaming.",
        modality=Modality.VIDEO,
        duration=6,
        resolution="768P",
        aspect_ratio="16:9",
    )
    .run(timeout=3600)  # the default step timeout is 10 minutes; see Waiting
)
print(run.steps[0].assets[0].url, manifest.canonical_hash)
```

Every clip is an MP4 with its audio track, 24 frames per second, 4 to 15
seconds long.

| `model` | Request | Resolutions |
|---|---|---|
| `MiniMax-H3` | text to video, first and last frame | `768P`, `2K`, `4K` |
| `MiniMax-H3-Ref2VA` | reference images, reference audio, video editing (50 steps) | `768P`, `2K`, `4K` |
| `MiniMax-H3-Ref2VA-SMD-4step` | the same requests, 4 steps | `768P`, `2K`, `4K` |
| `FastH3-8-Step-V2` | text to video, 8 steps | `768P` |

`2K` and `4K` clips are a 768P render upscaled with SeedVR2 3B.

## Models follow the catalog

The table above is the catalog on 2026-11-26. The connector does not depend
on it: it reads anex.sh's public model catalog
(`GET https://anex.sh/api/estimate/catalog`, no key, described on
[anex.sh/docs/video-models](https://anex.sh/docs/video-models)) and takes from
it each model's request names, resolutions, the kinds of request it answers
(text, first and last frame, references) and whether it honours a seed.

- A model added on anex.sh works without a new connector release:
  `validate_model()` confirms it, and its resolutions and input routing come
  from the catalog.
- A model that is no longer listed is refused at pipeline preflight
  (`NOT_FOUND`), before any task is created.
- A model the catalog does not list can still be used by registering it:
  `provider.models.register(ModelSpec(model_id="...", modality=Modality.VIDEO))`.
- The catalog is cached for an hour (genblaze's discovery default) and never
  read more often than every five minutes. It is read with its own HTTP
  client and never receives the API key. It is the same for every region, so
  it does not follow `base_url`; `catalog_url=` points it elsewhere and
  `catalog_url=None` turns it off.
- When the catalog cannot be read, the connector falls back to the models
  built into this version, and `validate_model()` answers `OK_PROVISIONAL`.

`duration` limits, the upscale resolutions and the output sizes recorded on
assets are not in the catalog's video block and are built in.

## Parameters

| Parameter | Values |
|---|---|
| `duration` | required, 4 to 15 whole seconds |
| `resolution` | `768P` (default), or another resolution the model lists (`2K`, `4K`); `768p`, `2k` and `4k` are accepted |
| `aspect_ratio` | `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16`; sent as `ratio`. A first or last frame sets the ratio itself |
| `seed` | an integer, honoured by the models that take one; a warning is logged when the catalog says the request ignores it |
| `image_url`, `end_image_url` | first and last frame (`MiniMax-H3`) |
| `reference_image_urls`, `reference_video_urls`, `reference_audio_urls` | references (Ref2VA models): up to 9 images, 3 videos and 3 audio files |

A media value is an `https://` URL, a `data:` URI or an `input:<id>` upload.
Any other parameter is forwarded to the API unchanged. Limits on the files you
supply are on [anex.sh/docs/video](https://anex.sh/docs/video#inputs).

Chained inputs (`input_from=` or `external_inputs=`) are routed by the kinds
of request the model answers: one or two images are the first and last frame
on a model that takes frames (`MiniMax-H3`), and anything else (videos, audio
files, more images) is a set of references on a model that takes references
(the Ref2VA models, and `MiniMax-H3`, whose reference requests are rendered by
MiniMax H3 Ref2VA). A text-only model (`FastH3-8-Step-V2`) takes none. To send
one or two images as references to `MiniMax-H3`, name them in
`reference_image_urls`. Local `file://` inputs are rejected because anex.sh
cannot read them: upload them (for example to B2) first.

## Upscaling a finished clip

```python
from genblaze_anex import AnexProvider
from genblaze_core import Modality, Step, StepType

upscaled = AnexProvider().invoke(
    Step(
        provider="anex",
        model="MiniMax-H3",
        modality=Modality.VIDEO,
        step_type=StepType.UPSCALE,
        params={"resolution": "4K", "source_task_id": "318407566120873452"},
    )
)
```

An upscale takes exactly one source: `source_task_id` (one of your finished
768P tasks; a generated step records it as
`step.provider_payload["anex"]["task_id"]`), or a clip of your own as
`video_url` or a chained video input. The upscaler is SeedVR2 3B; the API
names the request by `model="MiniMax-H3"`.

## Outputs and provenance

- The asset URL is the task's signed clip link, valid for one hour. Its query
  string carries a temporary signature, and a manifest written without a
  storage sink records the link as returned. Run the pipeline with a genblaze
  storage sink, which stores the clip and rewrites the asset URL to its
  durable one, or read the task again with `resume(task_id, step)` for a fresh
  link.
- anex.sh signs every clip with C2PA Content Credentials and adds
  machine-readable marks that identify it as AI-generated. The connector
  records the clip link as returned and does not read or alter the file.
  Embedding a genblaze manifest into the MP4 afterwards rewrites the file, so
  check the Content Credentials again if you depend on them.
- `step.provider_payload["anex"]` holds the task id, status, resolution, ratio
  and the billed `usage` the task reports. The API key never appears in step
  params, provider payloads or manifests.

## Cost

No prices ship with the connector. `provider.catalog_rates()` returns the
rates the catalog publishes now (USD per second by model and resolution, the
upscale rates and the image fees), and the recipe in
[pricing-recipes.md](https://github.com/backblaze-labs/genblaze/blob/main/docs/reference/pricing-recipes.md#anexsh)
turns them into a pricing strategy that fills `step.cost_usd` from the task's
billed seconds, so a price change needs no edit. Rates are also on
[anex.sh/docs/video-pricing](https://anex.sh/docs/video-pricing).

## Regions

An organization that keeps its data in the EEA uses the EU host:

```python
from genblaze_anex import EU_BASE_URL, AnexProvider

provider = AnexProvider(base_url=EU_BASE_URL)  # https://tasks-eu.anex.sh
```

The other region's host answers 421 with a message naming the right one. The
API key is sent only in the `Authorization` header, and only to `base_url`.

## Waiting

A task is `queued` until it starts to render, and the wait can be much longer
than the render itself: when capacity for a model has to start first, it can
take tens of minutes. genblaze's default step timeout is 10 minutes, so pass
a longer one, such as `run(timeout=3600)` or `config={"timeout": 3600}` on
`invoke()`.

A step that times out does not cancel its task: the task still finishes and
is billed. Its id stays on the failed step as `step.metadata["upstream_id"]`
(and arrives earlier as `request_id` on progress events and through the
`on_submit` callback), so collect the clip later with
`AnexProvider().resume(task_id, step)` instead of submitting again.

## Reliability

Submission is never retried by the provider's `RetryPolicy`, not even after a
connect failure, because an ambiguous failure can still be a billable task.
Task reads use that policy (bounded backoff that honors `Retry-After` and the
step deadline) for timeouts, connection errors, 429s and 5xx responses.
Setting step-level `config["max_retries"]` opts in to re-running a failed
submit, so leave it at 0 when duplicate clips are unacceptable.

An organization may have 30 tasks in flight; past that a create answers 429
(`RATE_LIMIT`). A request refused by content screening is `CONTENT_POLICY`
and is not billed.

The prediction id is the task id, so a checkpointed id can be passed to
`resume()` from any process. Call `close()` to release the internal HTTP
client.

## License

MIT
