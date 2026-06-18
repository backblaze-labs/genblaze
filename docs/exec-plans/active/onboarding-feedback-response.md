<!-- created: 2026-05-28 -->
# Onboarding Feedback Response — Tracks A / B / C

Response to multi-agent install-and-onboarding feedback (10 items + observations).
Goal: address the items that are actually broken and the highest-leverage docs/UX
gaps first, before any new public API surface.

## Goals & success criteria

- **Correctness first** — fix the one item that breaks in a base install (#3 pytest
  leak in `genblaze_core.testing`) before any docs polish.
- **No new public API in Track A** — Track A is packaging + docs only, so it can
  ship as a patch wave without an API-surface review.
- **Track B is the only one that adds public API** (`Asset.from_path`,
  `LocalFileSink`, `python -m genblaze_core.examples.local_quickstart`). It is
  designed once and lands in a single minor wave.
- **No scope creep** — items that are doc-only stay doc-only in Track C, with
  optional small code follow-ups gated behind separate decisions.

## Context

Feedback came from a fleet of agents that pip-installed `genblaze` from public
PyPI and tried to build apps from the README without internal context. Recurring
findings, ranked by how broken they are:

| # | Item | Current status | Track |
|---|------|----------------|-------|
| 3 | `genblaze_core.testing` does `import pytest` at module top (`libs/core/genblaze_core/testing.py:41`); mock providers unreachable in base install | **broken** | A |
| 6 | `genblaze` CLI commands (`genblaze index`) require pyarrow but the error message recommends a non-existent `genblaze[parquet]` extra | **broken-ish** | A |
| 1 | PyPI shows mismatched umbrella vs core versions; no compatibility table | confusing | A |
| 9 | macOS users hit Python 3.9 first; resolver errors look like "no distribution" | papercut | A |
| 5 | No `Asset.from_path` / `StepBuilder.asset_from_path` — users hand-roll hash/size/MIME | missing helper | B |
| 4 | No `LocalFileSink` — every local workflow writes manifest/sidecars/index by hand | missing helper | B |
| 2 | No-key quickstart is buried in repo, not in README/PyPI; depends on #3 + #4 to be honest | missing onboarding | B |
| 7 | `Manifest.verify()` semantics not explained vs asset-byte verification | docs gap | C |
| 8 | Replay modes (dry-run / structured plan / executable / unsupported) not delineated | docs gap | C |
| 10 | `PipelineResult` tuple-unpacking + status enum naming undocumented | docs gap | C |

## Track A — patch wave (packaging + docs, no new API)

**Sequencing:** A1 → A2 in parallel with A3/A4 → release.

### A1 — Split `genblaze_core.testing` so mocks import without pytest (#3)

- Current state: single file `libs/core/genblaze_core/testing.py` imports `pytest`
  at line 41 to support `ProviderComplianceTests`. Mock providers (`MockProvider`,
  `MockVideoProvider`, `MockAudioProvider`) live in the same file and become
  unimportable in any environment without pytest installed.
- Approach: convert `testing.py` into a package `testing/`:
  - `testing/__init__.py` — re-exports mocks unconditionally; lazy-loads
    `ProviderComplianceTests` via `__getattr__` so accessing it on the canonical
    import path without pytest raises `ImportError("install genblaze-core[dev] or pytest")`.
    The `__getattr__` is intentional UX polish on the documented path only.
  - `testing/mocks.py` — `MockProvider`, `MockVideoProvider`, `MockAudioProvider`
    (no pytest import).
  - `testing/compliance.py` — `ProviderComplianceTests` and any pytest-dependent
    helpers. `import pytest` stays at module top here. Direct submodule import
    (`from genblaze_core.testing.compliance import ...`) without pytest installed
    will raise raw `ModuleNotFoundError`; this is acceptable because the
    documented public path is `genblaze_core.testing`. Document this in the
    submodule docstring.
- Backwards compat: every public symbol available at the old import path
  (`from genblaze_core.testing import X`) must keep working. **Grep scope is the
  entire repo, not just `libs/connectors/*`** — include `cli/`, `docs/`,
  `examples/`, and any in-repo scripts. Capture grep output in the PR description
  so reviewers can verify.
- **Regression guard (blocking CI job):** add a named job
  `no-extras-import-smoke` to `.github/workflows/test.yml` that:
  1. Creates a fresh venv, runs `pip install ./libs/core` (no extras).
  2. Runs `python -c "from genblaze_core.testing import MockVideoProvider"` —
     must succeed.
  3. Runs `python -c "from genblaze_core.testing import ProviderComplianceTests"`
     in a subprocess and asserts it raises `ImportError` whose message contains
     `"pytest"` or `"genblaze-core[dev]"`. Fail the job on `ModuleNotFoundError`
     or on a missing keyword in the message.
  This step must be required-to-merge.

### A2 — CLI packaging fix for `genblaze index` (#6)

- Current state: `cli/pyproject.toml` does not declare a `parquet`/`index`
  extra; `genblaze index` errors point at a `genblaze[parquet]` extra that
  doesn't exist on the umbrella.
- Approach:
  - Add `genblaze-cli[index]` extra that pulls `pyarrow>=14.0`.
  - Add `genblaze-cli[parquet]` as a concrete alias of `[index]` in the same
    PR (two TOML lines, no separate decision). Eliminates the naming
    divergence with `genblaze-core[parquet]` and avoids deferring a 2-line
    change.
  - Update the runtime error message in the `index` command to say
    `pip install 'genblaze-cli[index]'` when pyarrow is missing.
  - Add a one-line `pip install genblaze-cli` in the README CLI section
    (currently only in the install matrix).
- **Verification (required before merge):** in a fresh venv run
  `pip install genblaze-cli[index]` and `pip install genblaze-cli[parquet]`;
  confirm `python -c "import pyarrow"` succeeds for both. Paste output in PR.
- **CHANGELOG:** add a `New extras` note that users pinning
  `genblaze-cli[parquet]` will now pull `pyarrow>=14.0` on upgrade.
- Decision deferred: whether to *also* add `genblaze[parquet]` on the umbrella
  for symmetry. Default to **no** — keeps the umbrella focused on the SDK
  surface and avoids dragging pyarrow into pure-SDK installs.

### A3 — Version compatibility table (#1)

- Current state: umbrella `genblaze==0.4.0` ships with `genblaze-core==0.3.2`
  and `genblaze-s3==0.3.2`. Agents installing `genblaze==0.3.2` from public
  PyPI got `genblaze-core==0.2.8`, which is the floor pin in that older
  umbrella. Not a republish problem — a discoverability problem.
- **Gate 1 — verification (run first, before any docs work):** in a clean venv,
  `pip install genblaze==0.3.2`, then import the package, build a trivial
  manifest, and reload it. Outcomes:
  - If install + smoke succeeds → continue with the docs-only approach below.
  - If install or smoke fails → escalate to a yank/republish decision; A3
    becomes a separate task and the docs change ships only after the decision.
  Running this gate first prevents mid-review re-scoping.
- Approach (after Gate 1 passes):
  - Add a "Version compatibility" section to README with a small table mapping
    umbrella wave → core → s3 → provider connector floor.
  - Add a clarifying paragraph to each package's PyPI long_description via the
    `readme` field in `pyproject.toml` — **not** the `description` field, which
    is a single-line ≤512-char summary and won't render Markdown. Verify
    rendering by running `twine check dist/*` against a locally built wheel
    before publish.

### A4 — Python 3.11+ install preamble (#9)

- Snippet at the top of the README install section, range-friendly so it
  doesn't go stale when newer minor versions become the macOS Homebrew default:
  ```
  python --version  # must be 3.11+
  python3.11 -m venv .venv   # or python3.12 / python3.13
  source .venv/bin/activate
  ```
- Add equivalent to each package's PyPI long_description (`readme` field in
  `pyproject.toml`, not `description`).
- No code changes.

## Track B — minor wave (new API surface, design first)

**Sequencing:** B1 design → B1 implement → B2 design → B2 implement → B3 (which
depends on both). **B1 may ship as its own minor wave** if B2 design review
exceeds one sprint — `Asset.from_path` is independently useful. B3 is the only
artifact that strictly depends on both.

### B1 — `Asset.from_path` + `StepBuilder.asset_from_path` (#5)

- Current state: `libs/core/genblaze_core/models/asset.py:72` defines `Asset` as
  a pydantic `BaseModel` with no path-based constructor. Users hand-compute
  SHA-256, size_bytes, MIME, dimensions, duration before instantiating.
- **DRY pre-condition (gate; complete before drafting the design doc):**
  audit and document the existing primitives in the design doc. Concretely:
  - `libs/core/genblaze_core/_utils.compute_sha256` (currently bytes-in) —
    reuse, but extend (see streaming requirement below).
  - `Asset.set_hash(data: bytes)` on the `Asset` model — reuse.
  - `libs/core/genblaze_core/_utils.probe_audio_duration` — reuse for audio.
  - `libs/core/genblaze_core/media/` — run
    `grep -rn 'def .*\(probe\|dimension\|duration\|sha256\|mime\)' libs/core/genblaze_core/media/`
    and document findings in the design doc. The implementer must compose with
    anything found, not duplicate it.
  - Pillow (already a core dep) for image dimensions; mutagen (existing
    `[audio]` extra) for audio duration. No new extra introduced.
- **Required design decisions (resolve in design doc before coding):**
  - **Streaming SHA-256.** `_utils.compute_sha256(data: bytes)` requires holding
    the whole file in memory; a 2 GB MP4 hashed via `from_path` would allocate
    2 GB. Extend `_utils` with a `compute_sha256_stream(path: Path, chunk: int = 1 << 20)`
    (or accept `Path | BinaryIO`) and have `from_path` use it. The existing
    bytes-in helper must remain for in-memory callers.
  - **Probe ordering (cost-aware fallback).** Default ordering for 10k-asset
    runs:
    1. MIME via `mimetypes.guess_type` (free, extension-based) as the fast path.
    2. Pillow `Image.open(...).size` only when `media_type` starts with
       `image/` AND the caller has not supplied `dimensions=`.
    3. mutagen only when `media_type` starts with `audio/` AND the caller has
       not supplied a duration. Skip silently if `[audio]` extra isn't
       installed; log debug only.
    Never open Pillow / mutagen unconditionally on every call.
  - **Hash idempotency / caller-supplied sha256.** When the caller passes
    `sha256=`, default behavior is **trust-and-skip-hash** (fast path);
    document this in the docstring with a warning. Add `verify_hash: bool = False`
    kwarg that, when true, re-hashes and asserts a match. No "always re-hash"
    mode — wasteful by default.
  - **Sync only.** Asset construction is not on the hot path.
  - **Local paths only.** `s3://` / `https://` URLs raise `ValueError` in v1.
- Tests: round-trip from PNG / WAV / MP4 fixtures already in `libs/core/tests/`.
  Include a streaming-hash test against a generated >100 MB temp file (skip on
  CI if disk-quota-limited; mark `slow`).

### B2 — `LocalFileSink` (#4)

- Current state: `libs/core/genblaze_core/sinks/` has `base.py` (the `BaseSink`
  ABC) and `parquet.py`. No filesystem-only sink.
- **Contract: implement `BaseSink` directly — not "mirror the S3 sink."**
  The S3 sink is one implementation of the ABC; mirroring it would couple
  `LocalFileSink` to S3-specific helpers and break substitutability. The design
  doc must explicitly enumerate which `BaseSink` abstractmethods + optional
  methods (`put_asset`, `put_assets`, `write_run`, `on_step_complete`, `close`)
  `LocalFileSink` implements vs leaves as the base default. Any method that
  remains `NotImplementedError` must override the message to say
  `"LocalFileSink is manifest-only in v1; see docs/features/local-sink.md"`
  so callers using `BaseSink`-typed handles get an actionable error rather
  than an opaque base-class one.
- **Mode decision: Mode 2 (in-place, no byte copy) for v1.** Mode 1 deferred.
  Rationale: composes with users who already have local files, avoids doubling
  disk usage, closer to what agents wrote by hand.
- **MockProvider URL handling (blocks B3 quickstart).** `MockProvider` returns
  assets with `https://mock.test/...` URLs, not `file://`. Mode 2 cannot write
  sidecars "next to" a remote URL. Decision: when the asset URL is not a local
  path, `LocalFileSink` writes the sidecar into the configured output
  directory using a deterministic filename derived from the asset SHA-256
  (e.g., `<sha256>.sidecar.json`). Document this in the sink docstring; B3's
  quickstart relies on it.
- **Atomic writes (production-readiness; DRY).** All file writes (manifest
  JSON, sidecars, index) must use atomic rename: `tempfile.mkstemp` in the
  same target directory → write → `os.replace`. The existing
  `_atomic_write_table` in `sinks/parquet.py` already encodes this pattern;
  factor it into `libs/core/genblaze_core/sinks/_io.py` and have both
  `ParquetSink` and `LocalFileSink` import from there. Do **not** duplicate.
- **Durability scope.** `LocalFileSink` does NOT fsync files or the containing
  directory; atomic rename gives crash-consistency but not power-loss
  durability. Document this caveat in the sink docstring and class-level
  docs. Optional `fsync=True` kwarg is a follow-up, not in this wave.
- Tests: write manifest + sidecars to a `tmp_path`, simulate mid-write crash
  by deleting the temp file, confirm no partial files left behind; reload
  and verify.

### B3 — `python -m genblaze_core._examples.local_quickstart` (#2)

- Depends on A1 (mocks importable) and B2 (LocalFileSink).
- **Placement: `genblaze_core/_examples/` (leading underscore = private).**
  Reason: top-level `genblaze_core.examples` would be a public namespace and
  becomes a "you can never take it back" commitment for OSS — module renames
  break any user who imports from it. Underscore subpackage signals private
  surface while remaining runnable via `python -m`. Confirm with a CI test
  that `python -m genblaze_core._examples.local_quickstart` succeeds.
- Ensure `_examples` is NOT added to `__all__` in `genblaze_core/__init__.py`
  and is not eagerly imported by the package `__init__`.
- One installed module that:
  - Builds a tiny pipeline with `MockVideoProvider`.
  - Emits manifest + sidecar + index via `LocalFileSink` into a tmp dir.
  - Reloads and verifies the manifest.
  - Prints the output paths.
- **Cold-start budget: < 1 second on a fresh venv with no extras installed.**
  The quickstart module must import only `MockVideoProvider`, `LocalFileSink`,
  and `Pipeline` — no top-level imports of `pyarrow`, `mutagen`, or `Pillow`.
  Verify in CI with:
  ```
  time python -c "import genblaze_core._examples.local_quickstart"
  ```
  in a minimal venv (`pip install ./libs/core` — no extras). Fail CI if it
  exceeds 1.5s (1s target + 0.5s headroom).
- README quickstart becomes: `pip install genblaze && python -m genblaze_core._examples.local_quickstart`.
- PyPI long_description gets the same two lines (in the `readme` field).

## Track C — docs (parallelizable, low urgency)

**Release anchor:** Track C items ship in the same minor wave as Track B (the
B3 quickstart release in particular benefits from C1/C2/C3 landing alongside).
Any Track C item not ready by the Track B cutoff is explicitly moved to a
follow-up issue with a linked CHANGELOG `Deferred` entry — not silently
dropped.


### C1 — Manifest verification semantics (#7)

- Doc page: `docs/features/manifest-verification.md` (or augment existing).
- Cover: what `Manifest.verify()` checks (canonical manifest hash), what it
  does **not** check (current asset bytes on disk), embedded-media caveat
  (embedding mutates bytes and breaks raw hashes).
- Code addition is **deferred** to a separate decision; this PR is docs only.

### C2 — Replay modes (#8)

- Doc page: `docs/features/replay.md` (or augment existing).
- Define and name: inspect, dry-run, structured-plan, executable, unsupported.
- For each, document: what input it needs, what it emits, what it requires
  installed (provider adapter, credentials), determinism caveats.
- Code addition (`--plan-json`) is **deferred**.

### C3 — `PipelineResult` shape + status enums (#10)

- **Pre-condition:** before writing the docs, confirm `PipelineResult.__iter__`
  is actually defined in the current code. If it is not, the tuple-unpacking
  example would be aspirational and C3 silently becomes a code addition. In
  that case, move the `__iter__` implementation to Track B (small additive
  change) and treat C3 as strictly docs-against-existing-behavior.
- One paragraph in `docs/reference/` (existing reference docs) covering:
  - `result.run`, `result.manifest`, and that `PipelineResult` is iterable
    (`run, manifest = Pipeline(...).run()`).
  - The two status enums: `StepStatus.SUCCEEDED` vs `RunStatus.COMPLETED`,
    why both exist, when each fires.

## Out of scope (explicitly)

- `genblaze doctor` — interesting but a separate plan; not bundled here.
- Use-case example apps (newsroom, ecommerce, etc.) — separate plan.
- `Manifest.verify_assets()` and `genblaze verify --check-asset-bytes` —
  deferred follow-ups to C1.
- `genblaze replay --plan-json` — deferred follow-up to C2.
- Yanking/republishing old umbrella versions — only if A3 verification finds
  an actual install break.

## Risks & open questions

- **A1 backwards compat** — any external code that does
  `from genblaze_core.testing import ProviderComplianceTests` must still work.
  Grep scope is the **entire repo** (incl. `cli/`, `docs/`, `examples/`),
  not just `libs/connectors/*`. Direct submodule import
  (`from genblaze_core.testing.compliance import ...`) without pytest
  installed will raise raw `ModuleNotFoundError`; the documented public path
  remains `genblaze_core.testing`, guarded by `__getattr__` for a nicer
  error.
- **A2 extra naming** — resolved: ship both `[index]` and `[parquet]` as
  aliases on `genblaze-cli` in the same PR.
- **A3 verification timing** — resolved: Gate 1 runs first; A3 only proceeds
  as docs-only if the gate passes.
- **B1 streaming hash** — `_utils.compute_sha256` must gain a streaming variant
  before `Asset.from_path` ships, or large MP4s will allocate full file size
  in memory. Bytes-in helper stays for in-memory callers.
- **B1/B2 DRY check** — concrete primitives the implementer must reuse, not
  duplicate: `_utils.compute_sha256` (+ new streaming variant),
  `Asset.set_hash`, `_utils.probe_audio_duration`, any helpers in
  `libs/core/genblaze_core/media/`, and the atomic-write helper to be
  factored out of `sinks/parquet.py` into `sinks/_io.py`.
- **B2 BaseSink contract** — `LocalFileSink` implements `BaseSink` directly
  (not "mirrors S3"). The design doc must enumerate which methods are
  implemented vs raise `NotImplementedError` with an actionable message.
- **B3 placement** — `_examples` private subpackage, excluded from `__all__`,
  not eagerly imported. Cold-start < 1.5s gated in CI.
- **Release sequencing** — Track A is a patch wave (0.4.1 or 0.3.4 depending
  on which umbrella we're cutting); Track B is a minor (0.5.0). Don't mix.
  **B1 may ship independently** of B2 if B2 design exceeds one sprint; B3
  remains gated on both.
- **Track C anchor** — ships in the same minor wave as Track B; deferred
  items get an explicit CHANGELOG `Deferred` entry.
