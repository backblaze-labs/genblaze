<!-- branch: issue/284-veo-output-path-traversal -->
# Issue 284: Veo output path interpolates unvalidated step_id — path traversal / cross-job overwrite (both Vertex and Gemini)

## Problem

GitHub issue: https://github.com/backblaze-labs/genblaze/issues/284

`VeoProvider._video_output_path()` (`libs/connectors/google/genblaze_google/provider.py:184-198`)
builds the local output path for a generated video by interpolating
`step.step_id` directly into the filename, with no validation:

    # libs/connectors/google/genblaze_google/provider.py:195
    return self._output_dir / f"{step.step_id}_{i}.mp4"

Both auth branches in `fetch_output()` (`:284-292` Vertex, `:293-314` Gemini)
call this shared helper and then `out_path.write_bytes(video_bytes)` directly
(`:291`, `:313`). This branch is stacked on `issue/263-...` (#286), whose fix
introduced the shared helper and the Gemini local-write path — so on this base
**both** auth modes flow through the identical unvalidated path construction.

`Step.step_id` only *defaults* to a UUID — `libs/core/genblaze_core/models/step.py:39`
is `Field(default_factory=new_id)`, and `new_id()` (`_utils.py:21-23`) returns
`str(uuid.uuid4())`. The UUID is a default, not an invariant: `Step` has
`model_config = ConfigDict(extra="forbid")`, but `step_id` itself is a plain
`str`, so a `Step` constructed or **deserialized** with an explicit `step_id`
can carry `../` traversal segments or an absolute path.

`Path.__truediv__` then escapes or ignores the configured `output_dir`:

- `output_dir / "../../../../tmp/pwned"` resolves outside `output_dir`.
- an **absolute** `step_id` makes `/` discard `output_dir` entirely.

The subsequent `write_bytes(...)` **follows existing symlinks and overwrites**
whatever resolves at the target — so a crafted `step_id` can clobber another
job's/tenant's output, or any process-writable path ending in the generated
`_<i>.mp4` suffix.

Impact is limited under the normal pipeline (the pipeline generates a UUID
`step_id`), which is why this is rated **P2**, not P1. But any service that
accepts externally-constructed or deserialized `Step` objects with a
caller-supplied `step_id` is exploitable.

## Fix

Harden the single shared `_video_output_path()` helper and add a safe writer,
so neither auth mode can write outside `output_dir` and neither can overwrite
an existing file via a symlink.

Production — `provider.py`:

1. **`_video_output_path(step, i)`** — validate before building the path:
   - Parse `step.step_id` with `uuid.UUID(step.step_id)`. A non-UUID value
     (`../…`, an absolute path, any traversal junk) fails to parse →
     raise `ProviderError(..., error_code=ProviderErrorCode.INVALID_INPUT)`.
     Build the filename from the canonical parsed value
     (`f"{parsed}_{i}.mp4"`), not the raw string — neutralizes traversal at
     the source.
   - When `self._output_dir` is set: `mkdir(parents=True, exist_ok=True)`,
     then resolve the candidate and assert
     `candidate.resolve().parent == self._output_dir.resolve()` (equivalent to
     `is_relative_to` for a single-segment filename; defense-in-depth against
     any future change to the filename template). Raise `ProviderError`
     (`INVALID_INPUT`) if not contained.
   - No-`output_dir` fallback unchanged: unique `tempfile.mkstemp(suffix=".mp4")`
     (already outside the traversal surface — mkstemp ignores `step_id`).
2. **New `_write_new_video_file(path, data)`** helper, used by both branches
   instead of `Path.write_bytes`:
   `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)`,
   write, close. `O_EXCL` refuses to write through a pre-existing name (file or
   symlink); `O_NOFOLLOW` refuses to traverse a symlink even if the OS ordering
   allowed it. Any `OSError` (`FileExistsError` included) →
   `ProviderError(..., error_code=ProviderErrorCode.INVALID_INPUT)`.
   The tempfile-fallback case has already atomically created the file via
   `mkstemp`, so its path is re-opened for write without `O_EXCL` (the file
   legitimately exists and is exclusively ours — `mkstemp` guarantees that).
3. Replace both `out_path.write_bytes(video_bytes)` call sites (`:291`, `:313`)
   with `self._write_new_video_file(out_path, video_bytes)`.
4. No change to audio/track metadata, pricing, or the "neither bytes nor uri"
   guard.

Tests — `test_veo_provider.py`:

- Traversal/validation, both branches (Vertex inline-bytes, Gemini download):
  - `step_id="../../../../tmp/pwned"` → `ProviderError`, nothing written
    outside `output_dir`.
  - absolute `step_id` (e.g. `"/tmp/pwned"`) → `ProviderError`.
  - non-UUID junk `step_id` (e.g. `"not-a-uuid"`) → `ProviderError`.
- Symlink safety: pre-plant a symlink at the would-be target path (using a
  legitimate UUID `step_id`) → write refuses (`ProviderError`), the symlink's
  destination file is untouched.
- No-overwrite: a pre-existing regular file at the target path is not
  clobbered (`ProviderError`).
- Duplicate `step_id` across two videos in one step still disambiguates via
  the `_<i>` index (existing behavior preserved for the legit UUID case).
- Existing Vertex + Gemini happy-path tests still pass unmodified (valid
  `uuid4` `step_id` writes under `output_dir` and exposes a `file://` URL).

Docs / release:

- CHANGELOG `[Unreleased]` → `### genblaze-google` **Fixed** (security) bullet
  (#284).
- No version bump (versions move per release wave, per RELEASING.md).

## Risk

Low, contained to `genblaze-google` Veo.

- Legit pipeline runs are unaffected: `step_id` is a `uuid4`, parses cleanly,
  resolves directly under `output_dir`, and the target doesn't pre-exist — the
  happy path is byte-for-byte equivalent aside from the safer open flags.
- Behavior change by design: a re-run that reuses an `output_dir` + `step_id`
  now raises instead of silently overwriting (per the issue's "without
  overwriting an existing file"). This matches the security requirement and is
  covered by a test.
- `O_NOFOLLOW` is available on POSIX; Python's `os.open` accepts it on Windows
  too (mapped by the CRT) — the symlink test asserts the refusal on the CI
  platform.
- Depends on #286 (#263) — this branch is stacked on it and cannot merge until
  #286 does. Called out in the PR.

## Files Modified

| File | Change |
|---|---|
| `libs/connectors/google/genblaze_google/provider.py` | modified |
| `libs/connectors/google/tests/test_veo_provider.py` | modified |
| `CHANGELOG.md` | modified |
| `docs/exec-plans/completed/issue-284-veo-output-path-traversal.md` | created |

No core/model source touched — the fix is provider-local (validation at the
Veo boundary), keeping the diff minimal and avoiding cross-provider fallout
from tightening `Step.step_id` globally. No version bump.

## Verification

1. Regression gate: the new traversal/symlink/overwrite tests fail against the
   pre-fix code (writes escape `output_dir` / overwrite silently) and pass
   after the fix.
2. `cd libs/connectors/google && pytest tests/ -q` → all green.
3. `ruff check` / `ruff format --check` on touched files — clean.
4. Full-repo `make test` gate on the final commit → exit 0.
5. `/pre-pr-check` scoped to this diff against the acceptance criterion "no
   write escapes the resolved output_dir on either Veo auth path, and no write
   overwrites an existing file or follows a symlink"; resolve blocking
   findings before opening the PR.
