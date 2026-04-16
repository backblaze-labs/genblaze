<!-- last_verified: 2026-04-16 -->
# Genblaze — Claude Code Config

- Follow [AGENTS.md](AGENTS.md) at all times
- Read order: README.md → ARCHITECTURE.md → AGENTS.md → relevant feature doc
- Plans go in `docs/exec-plans/active/`
- Test commands: `make test` (full), `cd libs/core && pytest tests/unit/<file>.py -v` (quick)
- Lint: `make lint`
- Always run `make test` before considering work complete
- Update docs in the same PR as code changes
- Keep diffs minimal — only change what's needed
