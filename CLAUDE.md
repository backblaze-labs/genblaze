@AGENTS.md

# Claude Code specifics

- Python edits are auto-formatted by `.claude/hooks/auto-format.sh`.
- Skills: `/verify` (done-check before a PR), `/test-package <name|changed>` (fast
  iteration), `/scaffold-provider` (new connector), `/verify-docs` (docs audit),
  `/prepare-release`, `/release-check`.
- Maintainers with the private companion repo: a gitignored `CLAUDE.local.md`
  containing `@.internal/AGENTS.md` loads internal context. Never copy anything
  from `.internal/` into tracked files.
