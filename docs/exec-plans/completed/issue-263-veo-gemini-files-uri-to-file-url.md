<!-- branch: issue/263-google-veo-gemini-files-uri-to-file-url -->
# Issue 263: Veo Gemini Developer API path keeps Files API URIs that ObjectStorageSink/B2 cannot fetch

## Problem

GitHub issue: https://github.com/backblaze-labs/genblaze/issues/263

`VeoProvider.fetch_output()` (`libs/connectors/google/genblaze_google/provider.py:262-308`)
handles the two auth modes asymmetrically, so B2/object-storage transfers
succeed on Vertex but fail on the Gemini Developer API (`api_key` /
`GEMINI_API_KEY`).

The branch discriminator is `video_bytes`, read at `:264` **before** any
download:

- **Vertex AI** — video comes back inline (`video.video_bytes` set, `uri` is
  `None`). The `if` branch (`:266-282`) writes the bytes to disk and exposes a
  `file://` URL via `local_file_url()`. `ObjectStorageSink` uploads to B2 with
  no Google auth. Correct.
- **Gemini Developer API** — video comes back as a Files API reference (`uri`
  set, `video_bytes` `None`). The `else` branch (`:283-290`) calls
  `client.files.download(file=video)` — which fetches the bytes — then **throws
  them away** and sets `video_uri = getattr(video, "uri", None)`, a credentialed
  `https://…` Files API URI. `validate_asset_url()` passes it (it is HTTPS), the
  asset is attached, and downstream `ObjectStorageSink` fetches it
  **unauthenticated** → the download fails and the asset never lands on B2.

#136 fixed the Vertex inline-bytes path and explicitly left the Gemini path
unchanged, leaving `api_key` + B2 users broken end-to-end.

SDK contract, confirmed against the pinned `google-genai` (`>=1.0,<3`) source
(`google/genai/files.py:675-765`):

- `download(file=video)` with `destination=None` returns the bytes **and** sets
  `video.video_bytes` as a side effect (`:759-763`) — so the current code
  already pays the full download cost, then discards it.
- `download(file=video, destination=<path>)` **streams to disk in chunks
  without holding the file in memory** (`:687-692`) and returns `None`.

A test currently **codifies the bug**: `test_fetch_output_attaches_asset`
(`libs/connectors/google/tests/test_veo_provider.py:106`) asserts the asset host
stays `storage.googleapis.com`. The default mock operation
(`_make_completed_operation`, `:17-22`) drives this same Gemini path, so several
other tests depend transitively on the buggy URI-passthrough. This is not an
additive fix — the fixture and these assertions change.

## Fix

Make the Gemini path reach the **same `file://` outcome** as Vertex, and share
the file-materialization logic so the two modes can't drift again.

Production — `provider.py` `fetch_output()` loop (`:262-308`):

1. Extract `_video_output_path(step, i) -> Path`, encapsulating the path
   selection currently inlined in the Vertex branch (`:272-280`): if
   `self._output_dir` is set, `mkdir(parents=True, exist_ok=True)` and return
   `output_dir / f"{step.step_id}_{i}.mp4"`; otherwise a unique
   `tempfile.mkstemp(suffix=".mp4")` path. One definition, both modes.
2. Restructure the loop so each mode only *fills* the file, then both share one
   `file://` emission:
   - **Vertex** (`video_bytes` truthy): `out_path.write_bytes(video_bytes)`
     (unchanged behavior).
   - **Gemini** (`elif uri`): `video_bytes = client.files.download(file=video)`
     — the byte-returning download form (also sets `video.video_bytes`); then
     `out_path.write_bytes(video_bytes)`. **Do NOT** use the streaming
     `destination=` argument: it was only added in google-genai **2.21.0**, but
     this connector supports `google-genai>=1.0,<3`, so passing it would
     `TypeError` on every earlier allowed release. Raise `ProviderError` if the
     download yields no bytes.
   - Both: `video_uri = local_file_url(out_path.resolve())`.
3. Drop the now-unreachable `validate_asset_url(video_uri)` call on the Gemini
   branch (it only guarded the remote HTTPS URI, which no longer escapes the
   provider). Preserve the guard that raises `ProviderError` when neither inline
   bytes nor a downloadable `uri` exist.
4. Remove the unused `validate_asset_url` import (ruff F401).

Net behavioral change: on `api_key` mode, `asset.url` changes from a credentialed
`https://…` Files API URI to a `file://` URL backed by bytes on local disk —
matching Vertex, matching `ImagenProvider` / `gemini_image` /
`DecartVideoProvider`, and letting `ObjectStorageSink` upload to B2 without
Google auth. Audio/track metadata handling (`:293-306`) is untouched.

Tests — `test_veo_provider.py`:

- Update the `mock_google` fixture: give `mock_client.files.download` a
  `side_effect` that returns fake bytes and sets `file.video_bytes` (mirroring
  the real byte-returning contract) and asserts `destination` is NOT passed,
  instead of `return_value = None`.
- Rewrite `test_fetch_output_attaches_asset`: assert
  `asset.url.startswith("file://")`, the file exists with the expected bytes,
  and `files.download` was called **without** a `destination=` (the compat
  guard). Remove the `storage.googleapis.com` host assertion.
- Add Gemini-path tests mirroring the existing Vertex trio:
  `test_fetch_output_gemini_downloads_to_file_url` (output_dir → indexed name),
  `test_fetch_output_gemini_no_output_dir` (tempfile fallback),
  `test_fetch_output_gemini_multiple_videos_no_collision`.
- Confirm the other operations-get-driven tests still pass under the new
  fixture; adjust any lingering URI assertion.

Docs / release:

- CHANGELOG `[Unreleased]` → `### genblaze-google` **Fixed** bullet (#263).
- No version bump (versions move per release wave, per RELEASING.md).

## Risk

Low–moderate, contained to `genblaze-google` Veo.

- The Vertex path is byte-for-byte unchanged (still `write_bytes`); only its
  path-selection lines move into a helper.
- The Gemini path already performed the full download (the old code called
  `client.files.download(file=video)` and discarded the bytes). The fix simply
  keeps those bytes and writes them to a local file — the only new external
  behavior is the local write, what every other local-output connector already
  does through `local_file_url`.
- SDK compatibility (why byte-buffering, not `destination=` streaming): the
  streaming `destination=` argument was added only in google-genai **2.21.0**,
  but the connector declares `google-genai>=1.0,<3`. Using it would `TypeError`
  on every earlier allowed release. Byte-buffering works across the whole range.
  A test asserts the download call passes no `destination=` so this can't
  regress.
- Memory: like the Vertex path, the Gemini path now holds one clip in memory
  before writing. Veo clips are short (`max_duration` 8s → tens of MB) and are
  processed one at a time, so this is bounded and acceptable. A future
  bounded-memory streaming path is possible via SDK feature-detection if the
  floor is later raised — tracked as a follow-up, not required for this fix.

## Files Modified

| File | Change |
|---|---|
| `libs/connectors/google/genblaze_google/provider.py` | modified |
| `libs/connectors/google/tests/test_veo_provider.py` | modified |
| `libs/connectors/google/README.md` | modified |
| `CHANGELOG.md` | modified |
| `.gitignore` | modified (ignore `.pre-pr-check/` audit trail) |
| `docs/exec-plans/completed/issue-263-veo-gemini-files-uri-to-file-url.md` | created |

No unrelated production source touched. No version bump (versions move per
release wave, per RELEASING.md).

## Verification

1. Regression gate — the rewritten + new tests fail against the old
   URI-passthrough code and pass after the fix. Additionally, reintroducing the
   `destination=` kwarg fails 12 tests (the compat guard), proving the
   version-safety assertion is load-bearing.
2. `cd libs/connectors/google && pytest tests/ -q` → **142 passed, 10 skipped**
   against the fixed source.
3. `ruff check` / `ruff format --check` on the touched files — clean; no F401
   from the dropped `validate_asset_url` import.
4. Full-repo `make test` gate on the final commit → **exit 0, all packages
   green** (core `2144 passed`, google `142 passed`, plus every other connector /
   cli / meta / tools).
5. Pre-PR review (3 Claude + 3 Codex checks) caught the SDK-compat blocker
   pre-merge; re-run after the fix returned zero blocking findings. Two items
   deferred to a follow-up issue (shared output-path hardening; bounded-memory
   streaming) — neither affects this acceptance criterion.
