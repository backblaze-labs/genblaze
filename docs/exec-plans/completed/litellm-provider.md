# LiteLLM provider connector

## Goal

Add an opt-in `genblaze-litellm` connector that routes image, speech, and video
generation plus chat through the LiteLLM SDK, so one package reaches every
provider LiteLLM supports without changing existing provider defaults.

## Scope

- Add a separately installable connector with three provider entry points
  (`litellm-image`, `litellm-speech`, `litellm-video`) and `chat()` / `achat()`.
- Image and speech are `SyncProvider`s; video uses submit / poll /
  fetch_output with a single-attempt submit and a cached terminal poll.
- Forward step params with `drop_params=True`; read credentials from each
  provider's own env vars unless `api_key` / `api_base` are passed.
- Fill `step.cost_usd` and `ChatResponse.cost_usd` from LiteLLM's per-response
  cost, leaving `register_pricing` recipes in charge when LiteLLM has no price.
- Map LiteLLM's typed exceptions to `ProviderErrorCode`.
- Write base64 images, audio, and video to local files with SHA-256; keep
  SSRF-validated image URLs as asset URLs.
- Wire the package into workspace install, test, typecheck, release, pin
  parity, and docs surfaces. Expose a standalone `genblaze[litellm]` extra and
  keep it out of the curated bundles, because litellm requires `openai<3`.

## Verification

- Connector tests: 73 passed, 6 skipped (compliance harness for all three
  providers, mocked LiteLLM).
- `make test` and `make lint` (ruff + deptry) pass with the connector
  installed alongside every other package; that resolves `openai` 2.x, which
  the OpenAI and NVIDIA connectors' `openai>=1.0,<4` range allows.
- Mypy passed for the connector package.
- Connector tests pass on litellm 1.97.0 and 1.105.3.
- Live run through `Pipeline`: chat on two models, then an image step with a
  verified manifest.
