<!-- completed: 2026-06-18 -->
# Plan: Issue 59 Replay Failure Exit Code

## Summary
Ensure `genblaze replay --no-dry-run` exits non-zero when the replayed pipeline
finishes with a failed or cancelled run status.

## Scope
- Inspect replay execution status after `Pipeline.run()`.
- Return exit code 1 for failed or cancelled replay runs.
- Add a CLI regression test for a replayed provider failure.

## Verification
- `cd cli && pytest tests/test_cli.py::test_replay_no_dry_run_exits_nonzero_when_run_fails -q`
- `cd cli && pytest tests/test_cli.py -q`
- `make lint`
- `make test`
