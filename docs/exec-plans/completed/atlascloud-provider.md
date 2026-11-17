# Atlas Cloud provider connector

## Goal

Add an opt-in `genblaze-atlascloud` connector for Atlas Cloud's asynchronous
image and video generation APIs without changing existing provider defaults.

## Scope

- Add a separately installable connector with provider discovery entry point.
- Route image and video steps to their corresponding Atlas Cloud endpoints.
- Poll predictions with bounded GET retries while never retrying submission.
- Validate URL-bearing model inputs before forwarding them.
- Add focused mocked lifecycle and compliance tests.
- Wire the package into workspace install, test, typecheck, and release surfaces.

## Verification

- Connector tests: 19 passed, 4 skipped.
- Core tests: 1858 passed, 21 skipped.
- Tool tests: 130 passed.
- Ruff formatting and linting passed for all changed Python files.
- Mypy passed for the connector package.
- Package wheel and sdist passed `twine check`.
- Provider entry-point discovery returned `atlascloud`.
