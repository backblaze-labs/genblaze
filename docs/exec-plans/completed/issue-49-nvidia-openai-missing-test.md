<!-- completed: 2026-09-02 -->
# Issue 49: Make the nvidia "openai missing" test actually run instead of skipping in CI

## Problem

GitHub issue: https://github.com/backblaze-labs/genblaze/issues/49

`openai` is an optional dependency of `genblaze-nvidia` — it lives in the
`[chat]` extra (`libs/connectors/nvidia/pyproject.toml:42-46`), not the hard
dependency list. Installing `genblaze-nvidia` without `[chat]` is a supported
path, and the SDK has a dedicated error for it:

    'openai package not installed. Run: pip install "genblaze-nvidia[chat]"'

The test written to cover that error disabled itself in every environment
where the suite actually runs. `test_chat_raises_when_openai_missing`
(`tests/test_chat.py:215`) opened with a guard at `:221`:

    if importlib.util.find_spec("openai") is not None:
        pytest.skip("openai is installed — ImportError branch not reachable")

`openai` is installed transitively in dev and CI, so `find_spec` always
returned non-`None` and the test always skipped:

    $ pytest tests/test_chat.py -k "openai_missing" -rs
    tests/test_chat.py::test_chat_raises_when_openai_missing SKIPPED [100%]
    SKIPPED [1] tests/test_chat.py:222: openai is installed — ImportError branch not reachable

The premise in the docstring — "the branch is un-hittable without uninstalling
openai" — was false. The branch is reachable by evicting the module from
`sys.modules`, a technique already used twice elsewhere in this repo.

The false confidence was demonstrable. Replacing the real message in
`chat.py:179` with a garbage string and running the whole file gave
`8 passed, 1 skipped` — green, with a broken user-facing error.

Two production branches raise this message, and only one had even a nominal
test. Neither passes an `error_code=`:

| Location | Enclosing callable | Coverage before |
|---|---|---|
| `genblaze_nvidia/chat.py:176-180` | `chat()` | the skipped test |
| `genblaze_nvidia/chat_provider.py:251-256` | `NvidiaChatProvider._resolve_client()` | none at all |

All 20 tests in `test_chat_provider.py` inject a mock client, which
short-circuits at `chat_provider.py:243-244` before the import is attempted.
The issue's "apply the same to `chat_provider.py`" therefore meant writing a
test that did not exist, not editing one.

## Fix

Replaced the skip guard with a deterministic simulation of the missing
module: `monkeypatch.setitem(sys.modules, "openai", None)`. CPython's import
machinery raises `ImportError` when it finds `None` at a `sys.modules` key, so
the branch executes regardless of what is installed on disk.

This reuses an idiom already load-bearing in two other connectors rather than
inventing one:

- `libs/connectors/langsmith/tests/test_tracer.py:60-70` —
  `monkeypatch.setitem(sys.modules, "langsmith", None)`
- `libs/connectors/s3/tests/test_async_backend.py:150-179` —
  `patch.dict(sys.modules, {"aioboto3": None})`

No shared "hide this module" helper exists to reuse —
`libs/core/genblaze_core/testing.py` exports only `MockProvider`,
`MockVideoProvider`, `MockAudioProvider`, `ProviderComplianceTests`, and no
`conftest.py` provides such a fixture. Two call sites did not justify creating
one.

The assertions were also tightened. The old test matched `"openai package"`,
loose enough that a mangled hint could still pass.

Both tests now assert `str(exc) == MISSING_OPENAI_MSG` rather than using
`pytest.raises(match=...)`. This matters: `match=` is `re.search`, so even a
fully `re.escape`-d pattern is only a *substring* test — a message that keeps
the hint but wraps it in unrelated text would still pass. Equality is what
actually satisfies the issue's "exact install-hint message" criterion.
`ProviderError` defines no `__str__` (it just calls `super().__init__(message)`
at `libs/core/genblaze_core/exceptions.py:47-58`), so `str(exc)` is exactly the
message and the comparison is safe. This also removes the `re` import both
files would otherwise need.

Files touched:
- `libs/connectors/nvidia/tests/test_chat.py` — dropped the `find_spec`/`skip`
  guard, evicted `openai` via `monkeypatch`, asserted the exact message.
  Dropped the now-dead `import importlib.util` (its only use was the guard;
  ruff would flag F401). `import sys` is local to the test body, matching the
  existing convention at `:152` and `:207`.
- `libs/connectors/nvidia/tests/test_chat_provider.py` — added
  `test_generate_raises_when_openai_missing`, covering the previously untested
  `_resolve_client()` branch. Drives the public `generate()` rather than the
  private `_resolve_client()`, matching how `test_no_api_key_raises_auth_failure`
  exercises the sibling auth branch, and reuses the file's `_step()` helper.
  Constructed with no `client=` so `_injected_client` is `None` and the import
  is actually attempted.

No production source changed. No version bump — versions move per release
wave, not per fix (RELEASING.md). No CHANGELOG entry — CONTRIBUTING.md:59
gates that on "if your change should ship in a release"; this is test-only
with no user-visible behavior change.

## Risk

Low. Test-only, no API surface touched, no runtime behavior changed.

`sys.modules[name] = None` → `ImportError` is CPython import-machinery
behavior already relied on by two other connectors here, so this is not a
novel dependency. `monkeypatch` restores `sys.modules` at teardown, so there
is no leakage into sibling tests; the full package suite confirms it (167
passed). Test ordering does not matter — the `None` entry shadows an
already-imported real `openai`, verified directly.

The one way this bites later: if someone converts the function-local
`import openai` to a module-level import, the `sys.modules` patch would no
longer intercept it and these tests would fail. That is the correct alarm — a
module-level import would make `openai` a hard dependency and break the
`[chat]`-extra contract.

Out of scope, flagged for follow-up, not fixed here:

- `libs/connectors/replicate/tests/test_replicate_provider.py:701-720`
  (`test_get_client_raises_when_dependency_missing`) carries the identical
  skip-if-installed anti-pattern, and its docstring says it mirrors nvidia's.
  A repo-wide sweep found these two are the *only* instances in `libs/`, so
  one follow-up issue closes the pattern family.
- Both nvidia raises omit `error_code=` while every neighboring raise in those
  files sets one, so callers cannot branch on this error programmatically —
  only string-match it.

## Files Modified

| File | Change | Lines |
|---|---|---|
| `libs/connectors/nvidia/tests/test_chat.py` | modified | +13 −6 |
| `libs/connectors/nvidia/tests/test_chat_provider.py` | modified | +22 |
| `docs/exec-plans/active/issue-49-nvidia-openai-missing-test.md` | created | this file |

No production source files modified.

## Verification

Both tests now run rather than skip:

    $ pytest tests/ -v -rs -k "openai_missing"
    tests/test_chat.py::test_chat_raises_when_openai_missing PASSED          [ 50%]
    tests/test_chat_provider.py::test_generate_raises_when_openai_missing PASSED [100%]
    2 passed, 171 deselected in 0.14s

The regression gate — both tests must fail against broken production code,
which is what the old test could never do. Temporarily replaced the message in
`chat.py:179` and `chat_provider.py:255` with `'SABOTAGED UNHELPFUL MESSAGE'`:

    E       AssertionError: Regex pattern did not match.
    E         Expected regex: 'openai\\ package\\ not\\ installed\\.\\ Run:\\ pip\\ install\\ "genblaze\\-nvidia\\[chat\\]"'
    E         Actual message: 'SABOTAGED UNHELPFUL MESSAGE'
    2 failed, 171 deselected in 0.11s

A second, stricter sabotage confirms the equality assertion earns its keep.
Keeping the hint intact but wrapping it — `'FATAL DB CORRUPTION. openai package
not installed. Run: pip install "genblaze-nvidia[chat]" -- ignore this'` — is a
case a `re.search`-based `match=` would have **passed**. Both tests fail on it:

    FAILED tests/test_chat.py::test_chat_raises_when_openai_missing
    FAILED tests/test_chat_provider.py::test_generate_raises_when_openai_missing
    2 failed, 171 deselected in 0.10s

Sabotage reverted via `git checkout -- libs/connectors/nvidia/genblaze_nvidia/`;
`git status` then showed only the two test files modified.

Full nvidia package suite — note the `openai is installed` skip is gone; the
6 remaining skips are unrelated capability-based compliance opt-outs:

    $ pytest tests/ -q -rs
    SKIPPED [3] ../../core/genblaze_core/testing.py:286: Provider opts out of cost tracking
    SKIPPED [2] ../../core/genblaze_core/testing.py:190: Provider does not declare AUDIO modality
    SKIPPED [1] ../../core/genblaze_core/testing.py:168: Not a SyncProvider
    167 passed, 6 skipped in 48.52s

Lint:

    $ ruff check libs/connectors/nvidia/
    All checks passed!

    $ ruff format --check libs/connectors/nvidia/tests/test_chat.py libs/connectors/nvidia/tests/test_chat_provider.py
    2 files already formatted
