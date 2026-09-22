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
- Single-attempt submit (`RetryPolicy(max_attempts=1)` default). Status and
  result GETs retry transient failures, and those retries are bounded.
- Model ids are validated as `owner/app[/path]` so they cannot inject query
  params (`fal_webhook`) or traverse paths. Queue URLs returned by fal are
  followed only on the configured queue host, so the key never reaches
  another host.
- URL-bearing inputs (`image`/`video`/`audio`, `*_url`, `*_urls`) must be
  https or `data:` URIs. `sync_mode` is rejected so outputs are hosted URLs.
- Errors classified from fal's documented `detail[].type` / `error_type`
  (https://fal.ai/docs/documentation/model-apis/errors,
  https://fal.ai/docs/documentation/model-apis/request-errors) before HTTP
  status, falling back to `classify_api_error`.
- Starter families: FLUX (image), Wan `*-to-video` (video), Stable Audio
  (audio). Unknown slugs pass through the permissive fallback.
- Wired into workspace install/test/typecheck, CI, release, umbrella extras
  and bundles, pin parity, release smoke, and credential redaction (`FAL_`).

## Decisions

- The prediction id is fal's `request_id`. `status_url` and `response_url`
  come from the submit response rather than being rebuilt, because fal derives
  them from the app id rather than the full endpoint path. They are tracked
  per provider instance, with a bounded map, so `resume()` must use the
  instance that submitted the request.
- No pricing ships in the starter registry. fal bills in model-specific units,
  so `cost_usd` stays unset rather than being guessed.

## Verification

- Connector tests: 75 passed, 2 skipped (fully mocked, via `httpx.MockTransport`).
- Full `make test`, `make lint`, `make typecheck`, and connector mypy: see PR.
