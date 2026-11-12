# UberLLM provider connector

## Goal

Add an opt-in `genblaze-uberllm` connector for UberLLM's image and video
generation APIs without changing existing provider defaults.

## Scope

- Add a separately installable connector with provider discovery entry point.
- Route image steps to `/v1/images/generations` (or `/v1/images/edits` when a
  source picture is given) and video steps to `/v1/video/generations` jobs.
- Poll video jobs with bounded GET retries while never retrying submission.
- Save outputs as local `file://` assets; send the API key only to the API host.
- Record the reported `usage.cost_usd` on the step.
- Validate URL-bearing model inputs before forwarding them.
- Add focused mocked lifecycle and compliance tests.
- Wire the package into workspace install, test, release surfaces and the
  umbrella extras.

## Verification

- Connector tests: 23 passed, 3 skipped.
- Version coherence, release import smoke and pin parity tool tests passed.
- Ruff formatting and linting passed for all changed Python files.
- Mypy passed for the connector package.
- One live image generation and one live 1-second video job completed through
  `UberLLMProvider`, both saved as local assets with the reported cost.
