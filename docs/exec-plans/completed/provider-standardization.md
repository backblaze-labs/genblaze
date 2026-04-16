<!-- completed: 2026-04-08 -->
# Provider Layer Standardization

## Summary
Standardized all provider implementations for consistent security, metadata, error handling, poll caching, and documentation.

## Changes
- **Poll result caching** — `BaseProvider` gained `_cache_poll_result` / `_get_cached_poll_result` helpers; all 6 polling providers migrated
- **SoraProvider security** — `validate_chain_input_url()` added to submit() for chain input validation
- **VeoProvider resume support** — `submit()` returns `operation.name` for cold resume compatibility
- **DecartProvider split** — Separated into `DecartVideoProvider(BaseProvider)` + `DecartImageProvider(SyncProvider)` with backward-compat alias
- **Error mapper standardization** — Stability Audio extracted to `_errors.py` (all connectors now consistent)
- **Compliance harness expansion** — `ProviderComplianceTests` expanded from 10 to 15 tests
- **New-provider guide** — Updated with 8 new sections (capabilities, normalization, cost tracking, poll caching, etc.)
