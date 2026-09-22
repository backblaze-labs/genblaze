# MuAPI Provider Connector

**Status:** completed 2026-09-22

## Scope

- Add `genblaze-muapi` as an opt-in asynchronous provider.
- Discover MuAPI's enabled image, video, and audio models at runtime.
- Implement submit, poll, result parsing, output URL validation, audio metadata,
  error mapping, checkpoint resume, and reported-cost capture.
- Wire the package into Make, CI, release, umbrella extras, import smoke, docs,
  and changelog metadata.

## Validation

- Unit and compliance tests use only `httpx.MockTransport`.
- Unsafe model IDs, local/non-HTTPS inputs, malformed prediction IDs, and
  untrusted output URLs are rejected before becoming provider assets.
- No live or billable MuAPI request is part of the test plan.
