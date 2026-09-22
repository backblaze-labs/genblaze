# fal.ai provider connector

## Goal

Add an opt-in `genblaze-fal` connector (#267) so genblaze pipelines can reach
fal.ai's image, video, and audio catalog through one queue API, without adding
the `fal-client` SDK as a dependency.

## Scope

- Separately installable connector with a `genblaze.providers` entry point.
- fal queue contract over httpx, per
  https://fal.ai/docs/documentation/model-apis/inference/queue:
  `POST https://queue.fal.run/{model_id}` with `Authorization: Key $FAL_KEY`,
  then `GET status_url` until `COMPLETED`, then `GET response_url`.
- Single-attempt submit: every submit failure is wrapped in `ProviderError`,
  so core's submit phase (which retries only raw pre-response exceptions)
  never re-sends the POST. Status and result GETs use core's `RetryPolicy`,
  which honors `Retry-After`, the step deadline, and retry events, instead of
  a connector-local loop.
- Model ids are validated as `owner/app[/path]` so they cannot inject query
  params (`fal_webhook`) or traverse paths. Status and result URLs are always
  rebuilt on the configured queue host, so the key never reaches another host.
- URL-bearing inputs (`image`/`video`/`audio`, `*_url`, `*_urls`, nested at any
  depth) must be https or `data:` URIs. `sync_mode` is rejected so outputs are
  hosted URLs.
- Terminal failures reported on the status body (`error` / `error_type`) are
  classified without reading the stored error response. They are non-retryable
  (`CONTENT_POLICY` / `INVALID_INPUT` / `MODEL_ERROR`) because fal has already
  re-queued runner failures server-side.
- Errors classified from fal's documented `detail[].type` / `error_type`
  (https://fal.ai/docs/documentation/model-apis/errors,
  https://fal.ai/docs/documentation/model-apis/request-errors) before HTTP
  status, falling back to `classify_api_error`.
- Starter families: FLUX (image), Wan `*-to-video` (video), Stable Audio
  (audio). Unknown slugs pass through the permissive fallback.
- Wired into workspace install/test/typecheck, CI, release, umbrella extras
  and bundles, pin parity, release smoke, and credential redaction (`FAL_`).

## Decisions

- The prediction id is the request's queue path relative to the queue host
  (`[namespace/]owner/app/requests/<request_id>`). It is taken from fal's
  `response_url` when that URL is well formed and on the queue host. Otherwise
  it is derived from the endpoint id the same way `fal-client` does. The id is
  self-describing, so cross-process `resume()` works with no per-instance
  state, and it is re-validated on every poll and fetch because checkpoints are
  untrusted input.
- Follow-ups (core, out of this PR's lane): a per-phase `RetryPolicy` or
  submit-ambiguity marker, so step-level `max_retries` cannot re-send an
  ambiguous submit; and a shared `classify_http_status` helper to replace the
  status tables duplicated across connectors.
- No pricing ships in the starter registry. fal bills in model-specific units,
  so `cost_usd` stays unset rather than being guessed.

## Verification

- Connector tests: 129 passed, 4 skipped (fully mocked, via `httpx.MockTransport`).
- Full `make test`, `make lint`, `make typecheck`, and connector mypy: see PR.
