<!-- last_verified: 2026-05-11 -->
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
- Adding a new connector: use `/scaffold-provider`. Before tagging a release: run `make pre-release` and read [RELEASING.md](RELEASING.md). After publish: `make post-release VERSION=<umbrella-version>`. Auditing docs freshness: `/verify-docs`.
- Release flow: tag name follows the CHANGELOG wave header (`v0.3.0`), not any single package's version. Workflow at `.github/workflows/release.yml` triggers on GitHub Release creation; `workflow_dispatch` runs a TestPyPI dry-run.
- CI policy: Dependabot PRs run NO CI. Every job in `.github/workflows/ci.yml` and `security.yml` is guarded by `if: ${{ github.actor != 'dependabot[bot]' && github.event.pull_request.user.login != 'dependabot[bot]' }}`. When adding a job to any `pull_request`-triggered workflow, copy that same guard onto it, otherwise Dependabot PRs would start running CI again. `release.yml` has no `pull_request` trigger, so it needs no guard.
