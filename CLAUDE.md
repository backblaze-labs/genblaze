<!-- last_verified: 2026-04-22 -->
# Genblaze — Claude Code Config

- Follow [AGENTS.md](AGENTS.md) at all times
- Read order: README.md → ARCHITECTURE.md → AGENTS.md → relevant feature doc
- Plans go in `docs/exec-plans/active/`
- Test commands: `make test` (full-suite gate), `/test-package <name>` (one package), `/test-package changed` (only changed packages)
- Quick single-file run: `cd libs/core && pytest tests/unit/<file>.py -v`
- Lint: `make lint`. Python edits are auto-formatted via `.claude/hooks/auto-format.sh`.
- Always run `make test` before considering work complete
- Update docs in the same PR as code changes
- Keep diffs minimal — only change what's needed
- Adding a new connector: use `/scaffold-provider`. Before tagging a release: use `/release-check`. Auditing docs freshness: `/verify-docs`.
