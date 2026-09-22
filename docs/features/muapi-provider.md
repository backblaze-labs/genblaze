<!-- last_verified: 2026-09-22 -->
# MuAPI Provider

`genblaze-muapi` is an opt-in asynchronous provider for MuAPI's enabled
image, video, and audio catalog. It uses the generic model API rather than
copying a fixed list of model names into the SDK.

## Lifecycle

The connector discovers enabled media models with `GET /api/v1/models`, submits
model-specific JSON to `POST /api/v1/{model}`, and polls
`GET /api/v1/predictions/{request_id}/result`. Model fields in `Step.params`
are forwarded unchanged so newly enabled catalog models do not require a
package release.

Set `MUAPI_API_KEY` or pass `api_key=`. The key is sent only in the
`x-api-key` header and never enters request payloads or provider metadata.

## Safety and provenance

- Model IDs are constrained to one safe catalog slug and are never interpreted
  as arbitrary URL paths.
- URL-bearing inputs must be hosted HTTPS URLs or inline `data:` URIs.
- Returned assets must be HTTPS URLs and are mapped to typed image, video, or
  audio assets.
- Submit is single-attempt because retrying an ambiguous POST could duplicate a
  billable generation; result GETs use the normal provider retry handling.
- If MuAPI reports `cost.amount_usd`, it is copied to `Step.cost_usd`.

All connector tests use `httpx.MockTransport`; they do not make live or
billable MuAPI requests.
