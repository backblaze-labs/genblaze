<!-- last_verified: 2026-11-16 -->
# Review guide

What code review should flag in genblaze. Code owners maintain this file;
Claude Code Review reads it from the default branch, never from the PR under
review. Each rule exists because something shipped broken without it.

## Severity

- **Important**: wrong behavior, a security or integrity regression, a broken
  public API or semver promise, a missing test for changed behavior, or a
  checklist item below that fires and isn't handled. Blocks merge.
- **Nit**: style, naming, comments. **At most 3 per review**; skip the rest.
- **Pre-existing**: a real bug the diff didn't introduce. Report it, but the
  author isn't on the hook for it in this PR.

Every finding cites `file:line` and says why it matters. An escalation to a
human says why a human is needed.

## Always check

- The PR's **Testing** section shows real `make verify` output, not a claim.
  If behavior changed and no test changed, that's Important.
- Public API or wire-format change → CHANGELOG `[Unreleased]` entry and the
  PR's semver line agree.
- Review the latest commit only; a pass on an older commit doesn't carry over.

## Reliability checklist (trigger → check → origin)

Check only items whose trigger matches the diff.

| Trigger in the diff | Check | Origin |
|---|---|---|
| `canonical/`, `models/manifest.py`, any `json.dumps` feeding a hash | Key order and float normalization unchanged; `canonical_hash` still verifies after a round-trip; property test covers it | AGENTS.md invariant |
| A model field, `step_id`/`run_id`/`asset_id`/`tenant_id` joined into a path or object key | Value validated as a single path component before the join; writes stay under the configured base dir | #284, #328 |
| A connector writing `output_dir / f"{...}"` | Same as above; covered by `tools/tests/test_connector_output_paths.py` | #284, #328 |
| New embed path or media handler in `media/` | `EmbedPolicy` honored (redaction, pointer mode) | AGENTS.md invariant |
| Provider auth, headers, error messages, logging | No token, presigned query string or userinfo URL in manifests, logs or exceptions | #75, #201 |
| New connector or provider method | Implements `submit`/`poll`/`fetch_output`; errors map to `ProviderErrorCode` | Provider contract |
| New import in a package | Declared in that package's `pyproject.toml` (`make deptry`) | #37, #106 |
| Pydantic wire-model change | Schema in `libs/spec/schemas/` updated and `libs/spec/ts/` regenerated | `make ts-types` drift |
| Asset metadata (`width`, `media_type`, durations) | Out-of-spec values fail `verify()` and the CLI the same way | #149 |
| New job in a `pull_request` workflow | Dependabot `if:` guard copied; untrusted PR text via `env:` only; actions pinned by SHA | AGENTS.md CI policy |
| URL fetching (`storage/transfer.py`, `--fetch`) | SSRF pinning and allowed-roots still applied on every hop | verify `--fetch` |

## Skip

- `libs/spec/ts/` (generated; review the schema instead)
- `docs/reference/model-matrix.md` (generated)
- `docs/exec-plans/active/batch-plan-*.md`, `maintenance-report-*.md` (machine-written)
- `CHANGELOG.md` wording, except a missing or wrong entry

## Keeping this file useful

Add an item when a revert or incident traces back to a diff a reviewer could
have caught. Remove items that haven't fired in a year. Keep each row a
trigger you can read from a diff.
