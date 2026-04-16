---
name: scaffold-provider
description: Scaffold a new genblaze provider connector package with all required files, tests, and entry points by analyzing existing connectors for conventions.
argument-hint: <provider-name> <modality: image|video|audio|music> [sync|polling]
allowed-tools: Read Grep Glob Bash Edit Write
---

# Scaffold a New Provider Connector

Generate a complete provider connector package for: **$ARGUMENTS**

Parse the arguments:
- `$0` = provider name (e.g. `fal`, `hedra`, `picovoice`)
- `$1` = modality: `image`, `video`, `audio`, or `music`
- `$2` = API style: `sync` (default) or `polling`

## Phase 1: Learn the conventions

Read these files to understand the current patterns — do NOT skip this step:

1. **Base classes**: `libs/core/genblaze_core/providers/base.py` — read `SyncProvider` and `BaseProvider`
2. **Guide**: `docs/guides/new-provider.md` — the contributor checklist
3. **Two existing connectors** matching the modality (use Glob on `libs/connectors/*/`):
   - Read their `pyproject.toml`, `__init__.py`, `provider.py`, `_errors.py`, and test file
   - Note: naming conventions, import style, how params are normalized, how errors are mapped, how costs are tracked, how assets get metadata

Extract these patterns:
- Package naming: `genblaze-{name}` / `genblaze_{name}`
- Class naming: `{Name}Provider`
- Entry point format in pyproject.toml
- How `_errors.py` maps exceptions to `ProviderErrorCode`
- How `get_capabilities()` declares modality
- How `normalize_params()` handles standard names
- How tests mock the SDK client
- How compliance tests are wired up via `ProviderComplianceTests`

## Phase 2: Generate the scaffold

Create the package at `libs/connectors/{name}/` with these files:

### `pyproject.toml`
- Follow the exact format from existing connectors
- Set the `[project.entry-points."genblaze.providers"]` line
- Add the SDK as a dependency (use a placeholder version if unknown)
- Include `py.typed` marker in the wheel

### `genblaze_{name}/__init__.py`
- Single import + `__all__` export

### `genblaze_{name}/provider.py`
- Subclass `SyncProvider` (or `BaseProvider` if polling)
- Include: `name`, `__init__` with `api_key` + `super().__init__()`, lazy SDK import in `_get_client()`
- `get_capabilities()` returning the declared modality
- `normalize_params()` with standard param mappings for the modality
- `generate()` (sync) or `submit()`/`poll()`/`fetch_output()` (polling) with TODO placeholders for the actual API call
- `validate_asset_url()` on all output URLs
- `validate_chain_input_url()` on `step.inputs` if `accepts_chain_input=True`
- Appropriate `AudioMetadata` or `VideoMetadata` on assets
- Cost tracking with a `_PRICING` dict placeholder
- Error handling that uses `map_{name}_error()` from `_errors.py`

### `genblaze_{name}/_errors.py`
- `map_{name}_error(exc) -> ProviderErrorCode` function
- Follow the exact pattern from existing connectors (rate limit, auth, invalid input, timeout, server error, unknown)

### `genblaze_{name}/py.typed`
- Empty marker file

### `tests/__init__.py`
- Empty

### `tests/test_{name}_provider.py`
- Mock fixture that patches the SDK and injects a fake client
- Test: `test_generate_returns_asset` — basic success path
- Test: `test_invoke_full_lifecycle` — invoke returns SUCCEEDED
- Test: `test_api_error_raises` — SDK error wrapped in ProviderError
- Test: `test_normalize_params_maps_standard_names`
- Test: `test_cost_tracked`
- Compliance test class subclassing `ProviderComplianceTests`

## Phase 3: Validate

1. Run `cd libs/connectors/{name} && pip install -e ".[dev]"` to verify the package installs
2. Run `cd libs/connectors/{name} && pytest tests/ -v` to verify tests pass
3. Fix any failures before reporting

## Phase 4: Report

Show the user:
- List of files created
- TODO items they need to fill in (actual API calls, SDK import, pricing)
- Remind them to run `make test` and `make lint` from repo root when done
