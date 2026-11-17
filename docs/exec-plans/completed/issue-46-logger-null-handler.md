<!-- completed: 2026-09-02 -->
# Issue 46: Attach a NullHandler to the top-level genblaze logger

## Problem

GitHub issue: https://github.com/backblaze-labs/genblaze/issues/46

Every `genblaze.*` logger wrote to stderr by default, in any application that
had not configured logging itself. `pipeline.py:61` creates
`logging.getLogger("genblaze.pipeline")`; when a record propagated up the
hierarchy it found no handler anywhere, so Python fell through to
`logging.lastResort`, a built-in handler that emits WARNING-and-above straight
to stderr.

The symptom was reproducible with no API keys:

    python -c "
    from genblaze_core import Pipeline, Modality
    from genblaze_core.testing import MockVideoProvider
    print('--- app output starts ---')
    Pipeline('demo').step(MockVideoProvider(), model='mock-v1', prompt='x',
                          modality=Modality.VIDEO).run(progress=False, raise_on_failure=False)
    print('--- app output ends ---')
    "
    preflight.unknown step=0 provider=mock-video model=mock-v1 — no family matched; permissive fallback applies
    --- app output starts ---
    --- app output ends ---

Two defects in that output. The line appeared at all, though nobody asked for
it. And it appeared *above* the app's own markers, because logging writes to
unbuffered stderr while `print()` buffers on stdout — so SDK internals
interleaved unpredictably with the consumer's output.

This affected the whole namespace, not one logger: 29 distinct names
(`genblaze.pipeline`, `genblaze.s3`, `genblaze.storage.transfer`,
`genblaze.webhook`, `genblaze.openai.sora`, ...), and
`grep -rn "NullHandler" libs/ cli/` returned 0 matches.

Deciding whether library logs are visible is the application's job, not the
SDK's. Attaching a `NullHandler` to the namespace root is the standard Python
library convention for handing that decision back.

## Fix

Attached `logging.NullHandler()` to the `genblaze` logger at package import.

`NullHandler` (rather than `propagate = False` or a real handler) suppresses
only the `lastResort` fallback: records still propagate, so an application
that configures the root logger continues to receive everything.

The handler is attached from `genblaze_core/__init__.py`, not from the
`genblaze` umbrella package whose name it matches. The umbrella at
`libs/meta/genblaze/__init__.py` is a lazy re-export shim that is not always
installed — `pip install genblaze-core` alone is a documented, supported path
— so attaching there would leave core-only installs unprotected. The
name/package mismatch is deliberate and carries an inline comment.

Files touched:
- `libs/core/genblaze_core/__init__.py` — added `import logging` and attached
  the handler immediately after the `__version__` import, with a comment
  explaining both the convention and why the name is `genblaze` rather than
  `genblaze_core`.
- `libs/core/tests/unit/test_observability.py` — added `import subprocess`
  and two regression tests. Both run the reproduction in a fresh interpreter
  via `subprocess.run`, following the existing pattern in
  `test_all_public_names_importable.py` — pytest's own logging plugin
  installs handlers that would mask the real bug if checked in-process.
- `CHANGELOG.md` — one `**Fixed**` bullet under `[Unreleased]` →
  `genblaze-core`.

No change needed in `libs/meta/genblaze/__init__.py`: it re-exports from
`genblaze_core`, so importing it triggers the handler attachment
transitively.

No version bump — package versions are bumped per release wave, not per fix
(RELEASING.md).

Tests added:
- `test_library_logger_silent_without_app_config` — with no logging
  configured, a `genblaze.*` warning produces no stderr output. Confirmed
  this test fails against the pre-fix code with the exact leaked line as the
  assertion failure, before the fix was applied.
- `test_library_logger_visible_after_basic_config` — after
  `logging.basicConfig()`, the record still reaches a handler. Guards
  against over-suppressing.

## Risk

Low. No API surface changes, no behavior change for applications that
already configure logging. The only observable difference is the absence of
output that was never requested. The one way this could regress someone is
an operator who was relying on the accidental stderr output as their logging
setup; they now need one `logging.basicConfig()` call. That tradeoff is the
point of the fix, and it is the documented convention for Python libraries.

Out of scope: the `DeprecationWarning` about `raise_on_failure` emitted
alongside the log line comes from the `warnings` module, a separate channel,
and was not touched.

## Files Modified

| File | Change | Lines |
|---|---|---|
| `libs/core/genblaze_core/__init__.py` | modified | +16 |
| `libs/core/tests/unit/test_observability.py` | modified | +55 |
| `CHANGELOG.md` | modified | +3 |
| `docs/exec-plans/completed/issue-46-logger-null-handler.md` | created | this file |

## Verification

Confirmed the new test fails against the pre-fix code (temporarily reverted
via `git stash`) with the exact leaked line as the failure message, then
passes after restoring the fix:

    $ pytest tests/unit/test_observability.py -k "test_library_logger" -v
    tests/unit/test_observability.py::test_library_logger_silent_without_app_config PASSED
    tests/unit/test_observability.py::test_library_logger_visible_after_basic_config PASSED
    2 passed, 9 deselected in 0.36s

Full observability suite:

    $ pytest tests/unit/test_observability.py -v
    11 passed, 1 warning in 0.35s

Full core suite:

    $ pytest libs/core/tests/ -q
    2146 passed, 3 skipped, 278 warnings in 42.70s

Lint:

    $ ruff check libs/ cli/ examples/
    All checks passed!

    $ ruff format --check libs/ cli/ examples/
    17 files would be reformatted, 402 files already formatted

The 17 format findings are pre-existing markdown code-fence drift across
unrelated connector README files, confirmed present on `main` before this
change via `git stash` + re-check. None of the three files touched by this
fix are among them.

    $ cd libs/core && deptry .
    Success! No dependency issues found.
