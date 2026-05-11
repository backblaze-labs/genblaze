<!-- last_verified: 2026-05-07 -->
# Model registry decoupling — hardening

> Continuation of [`model-registry-decoupling.md`](model-registry-decoupling.md).
> The 14-PR rollout shipped the architectural change; this plan
> closes the security, concurrency, registry-correctness, and
> API-uniformity gaps surfaced by the post-rollout deep review.

## Goal

Resolve every BLOCKER- and HIGH-severity finding from the post-rollout
deep review so the `genblaze-core 0.3.0` surface is genuinely
production-ready — not merely CI-green. Each fix must address the
root cause: where the review flagged a patch, the redesigned approach
ships instead.

The five themes:

1. **Security** — `validate_chain_input_url` `file://` hardening.
2. **Concurrency correctness** — probe single-flight cleanup + per-probe deadline; poll-cache lock.
3. **Registry correctness** — `register_pricing` family-aware fallthrough; `register_family` cap enforcement; `fork()` carries deprecation-warning state.
4. **API uniformity** — every provider exposes the probe-cache ctor kwargs; a conformance test gates future drift.
5. **Documentation accuracy** — every claim in the migration guide and feature docs matches the actual `ValidationOutcome` / `ValidationSource` enums and the `Pipeline.preflight()` API shape.

Out of scope: MEDIUM / LOW / NIT items (deferred to a follow-up
cleanup plan), sample-app coordination (separate repos), and any
behavior changes beyond what each finding requires.

---

## Scope summary

| ID | Severity | Category | Root-cause fix |
|---|---|---|---|
| **B-NEW** | BLOCKER | security | `validate_chain_input_url` hardens `file://` (empty netloc, no `..`, allowlist of base dirs, deny `/proc`/`/dev`/`/sys`) |
| **B3** | BLOCKER | connector correctness | Veo splits into two families (`^veo-2[.-]` legacy / `^veo-` modern); `has_audio` lives on family `extras` |
| **B4** | BLOCKER | concurrency | `_cached_probe` cleanup moves into `try/finally`; stale `_probe_inflight` entry pop guaranteed; probe invocation gets a configurable per-attempt deadline |
| **B1** | BLOCKER | docs | `KNOWN_UNSTABLE` enum claim removed; `OK_PROVISIONAL{detail='known_unstable…'}` substituted everywhere |
| **B2** | BLOCKER | docs | Migration guide rewrites the `Pipeline.preflight()` paragraph; the method is a fluent setter, validation runs inside `run()` |
| **H1** | HIGH | docs | `UNKNOWN_PERMISSIVE` row added to every outcome table |
| **H2** | HIGH | docs | `ValidationSource.FALLBACK` added to every source table |
| **H3** | HIGH | docs | LMNT added to the migration guide's NONE list |
| **H4** | HIGH | registry correctness | `register_pricing` falls through to `match_family()` before minting a bare `ModelSpec` so family param contracts are preserved |
| **H5** | HIGH | registry correctness | `register_family` enforces `MAX_PROVIDER_FAMILIES` against `len(_user_families) + len(_provider_families)` |
| **H6** | HIGH | registry correctness | `fork()` carries `_warned_deprecated` so multi-tenant deployments don't spam |
| **H7** | HIGH | API uniformity | 6 providers gain `probe_cache_ttl` / `probe_cache_max_entries` ctor kwargs; **a conformance test** asserts every `BaseProvider`/`SyncProvider` subclass exposes them — root-cause fix, not a band-aid |
| **H8** | HIGH | test coverage | LMNT + Replicate get dedicated `tests/test_catalog_decoupling.py` with the full rubric matrix |
| **H9** | HIGH | docs | Migration guide says "returns `DiscoveryResult.unsupported(...)`" not "returns empty" |
| **H10** | HIGH | docs | `new-provider.md` `pyproject.toml` example bumped to `>=0.3.0,<0.4` |
| **H-CONC** | HIGH | concurrency | `_poll_cache` writes/reads behind `_poll_cache_lock`; ainvoke concurrency safe |
| **H-DEPR** | HIGH | maintenance | `probe_model()` deprecation horizon (0.4.0) added to CHANGELOG, migration guide, and the method's runtime warning |

17 fixes total: **5 BLOCKER + 12 HIGH**.

---

## Sequencing

Three phases; each phase ends in a green `make test` + per-connector
sweep + lint/typecheck. No commits across phase boundaries until the
phase passes its gate.

### Phase 1 — Core code (changes module behavior)

Highest blast radius — runs first, pinned by tests, verified before
anything else moves.

1. **B-NEW** — `validate_chain_input_url` hardening.
2. **B4** — probe `try/finally` + cleanup-on-BaseException + per-probe deadline.
3. **H-CONC** — `_poll_cache` lock.
4. **H4** — `register_pricing` family-aware fallthrough.
5. **H5** — `register_family` cap enforcement.
6. **H6** — `fork()` carries `_warned_deprecated`.

### Phase 2 — Connectors + API uniformity (changes provider surfaces)

Each connector edit is small and independent; bundled because they
share the same conformance test landing.

7. **B3** — Veo split into legacy + modern families; `has_audio` on family `extras`.
8. **H7** — `probe_cache_*` kwargs on LMNT, Replicate, NvidiaChatProvider, NvidiaAudioProvider, NvidiaVideoProvider, NvidiaImageProvider.
9. **H7 (root cause)** — new conformance test at `libs/core/genblaze_core/testing.py` (or a new test file) asserting every provider subclass accepts the kwargs.
10. **H8** — new `tests/test_catalog_decoupling.py` for LMNT and Replicate.

### Phase 3 — Docs (no behavior change)

11. **B1** + **H1** — outcome tables corrected (KNOWN_UNSTABLE → OK_PROVISIONAL with detail; UNKNOWN_PERMISSIVE added).
12. **B2** — Pipeline.preflight() doc rewrite.
13. **H2** — ValidationSource tables get FALLBACK.
14. **H3** — LMNT added to NONE list.
15. **H9** — `discover_models()` claim corrected.
16. **H10** — pyproject template constraint bumped.
17. **H-DEPR** — deprecation horizon documented in CHANGELOG, migration guide, and runtime `DeprecationWarning` includes the removal version.

---

## Per-fix design

### B-NEW — `validate_chain_input_url` `file://` hardening

**Threat model.** A pipeline that accepts user-controlled `step.inputs`
URLs (e.g. via a public API, a queue worker, a CLI flag) can today be
fed `file:///etc/passwd`. Any provider that forwards the URL to ffmpeg,
an audio decoder, or a subprocess reads arbitrary local files. Same
attack vector covers `file://remote-host/etc/passwd` (RFC violation,
non-empty netloc).

**Design (v3 — addresses percent-encoding bypass, symlink resolution, cross-platform path canonicalization).**

```python
_FORBIDDEN_FILE_PATH_PREFIXES = (
    "/proc/", "/dev/", "/sys/", "/etc/",
    "/private/etc/",         # macOS canonical alias for /etc
    "/private/var/run/",     # macOS secrets path
    "/run/secrets/",         # Linux container secrets (Docker/Kubernetes)
    "/var/run/secrets/",     # alt Linux secrets path
)

def validate_chain_input_url(
    url: str,
    *,
    file_root_allowlist: tuple[Path, ...] = (),
) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "file"):
        raise ProviderError(f"Unsupported scheme {parsed.scheme!r}",
                            error_code=INVALID_INPUT)
    if parsed.scheme == "https":
        return  # existing https validation continues to apply

    # --- file:// path ---
    # RFC 8089: empty or 'localhost' netloc; anything else is suspicious.
    if parsed.netloc and parsed.netloc != "localhost":
        raise ProviderError(
            f"file:// URL must have empty or 'localhost' netloc; got {parsed.netloc!r}",
            error_code=INVALID_INPUT)

    # Decode percent-encoded sequences BEFORE substring checks. Without
    # this, ``file:///valid/path/..%2Fetc%2Fpasswd`` slips through any
    # ".." check on parsed.path because urlparse leaves %2F encoded.
    decoded_path = unquote(parsed.path)

    if not decoded_path.startswith("/"):
        raise ProviderError(
            "file:// URLs require an absolute path",
            error_code=INVALID_INPUT)

    # Canonicalize through Path.resolve() to collapse '..', symlinks,
    # and platform aliases (/private/etc → /etc on macOS) in one pass.
    # strict=False so a path that doesn't yet exist (e.g. an output
    # location being validated before write) still validates.
    canonical = Path(decoded_path).resolve(strict=False)
    canonical_str = str(canonical)

    # Prefix denylist on the *canonical* path catches both raw forms
    # ("/etc/passwd") and resolved aliases ("/private/etc/passwd").
    if any(canonical_str.startswith(p.rstrip("/")) for p in _FORBIDDEN_FILE_PATH_PREFIXES):
        raise ProviderError(
            f"file:// path resolves to a sensitive system location: {canonical_str}",
            error_code=INVALID_INPUT)

    if file_root_allowlist:
        # Strict mode: the resolved path must sit under one of the
        # allowlisted roots. Symlinks pointing outside an allowlist
        # root resolve to their target and fail this check correctly.
        if not any(canonical.is_relative_to(root.resolve()) for root in file_root_allowlist):
            raise ProviderError(
                f"file:// path not under any allowlisted root: {canonical_str}",
                error_code=INVALID_INPUT)
```

**Two-mode design.**

- **Default (no allowlist).** Compatible with existing pipelines.
  Rejects: non-`https`/`file` schemes; non-empty/non-`localhost`
  netloc; relative paths; any percent-encoded form of `..`; paths
  resolving under `/proc`, `/dev`, `/sys`, `/etc`, `/private/etc`,
  `/private/var/run`, `/run/secrets`, `/var/run/secrets`. Sensible
  defaults; never fully secure.
- **Strict (caller passes `file_root_allowlist=(...)`).** Adds the
  containment check: every accepted path must resolve under one of
  the listed root directories. Symlinks resolve through; outside-root
  symlinks fail the check. Recommended for any deployment that
  accepts user-supplied `step.inputs` URLs.

**Cross-platform note.** `Path.resolve()` collapses `/private/etc` →
`/etc` on macOS and likewise handles other platform aliases the
denylist would otherwise miss. Windows: `parsed.path` for
`file:///C:/Users/...` is `/C:/Users/...`; `Path("/C:/Users/...").resolve()`
on Windows yields `C:\Users\...` which the prefix check correctly
fails to match against `/proc` etc. (Windows-specific sensitive paths
like `C:\Windows\System32` are not in the default denylist; Windows
deployments must use `file_root_allowlist`. The SDK currently targets
Linux/macOS as primary platforms — Windows support is best-effort.)

**Tests (corpus expanded — all parameterized).**

| Input | Expected |
|---|---|
| `file:///etc/passwd` | reject (denylist) |
| `file:///proc/self/cmdline` | reject (denylist) |
| `file:///dev/null` | reject (denylist) |
| `file:///private/etc/passwd` | reject (resolves to `/etc/passwd` on macOS via canonicalize; denylist on linux) |
| `file:///run/secrets/app-token` | reject (containers) |
| `file://remote-host/tmp/foo` | reject (non-empty netloc) |
| `file:///valid/path/../etc/passwd` | reject (canonicalize resolves `..`) |
| `file:///valid/path/..%2Fetc%2Fpasswd` | reject — `unquote` then `Path.resolve()` collapses to `/etc/passwd`; denylist hits |
| `file:///valid/path/..%252Fetc%252Fpasswd` (double-encoded) | accept in default mode (literal `%2F` is not a path separator after single-pass `unquote`; resolved path stays under `/valid/path/`) — but **reject in `file_root_allowlist` mode** if `/valid/path/` is not allowlisted. Documented as a known limitation of default mode in the function docstring; defense-in-depth requires the allowlist |
| `file://relative/path` | reject (relative) |
| `file:///tmp/output.mp4` | accept (default mode) |
| `file://localhost/tmp/output.mp4` | accept (RFC 8089 alias) |
| With `allowlist=(Path("/tmp/uploads"),)`: `file:///tmp/uploads/asset.mp4` | accept |
| With `allowlist=(Path("/tmp/uploads"),)`: `file:///tmp/other/asset.mp4` | reject (not under root) |
| With `allowlist=(Path("/tmp/uploads"),)`: `file:///tmp/uploads/symlink-to-etc-passwd` | reject (symlink resolves outside) |

The test corpus is `pytest.mark.parametrize`-driven so future entries
are one-liners; aim for ≥20 cases at landing.

### B3 — Veo `has_audio` typed via family extras (root cause, not patch)

**Why splitting into two families is right.** The current single `^veo-`
family forces a runtime `startswith("veo-3")` decision because veo-2
has no audio, veo-3+ does. One family can't carry both shapes.
`extras["has_audio"]` is correct, but only if each variant routes to
its own family. `^veo-` alone resolves both veo-2 and veo-3+ to the
same `spec_template` — wrong outcome.

**Design.**

```python
GOOGLE_VEO_LEGACY_FAMILY = ModelFamily(
    name="google-veo-legacy",
    pattern=re.compile(r"^veo-2[.-]"),  # veo-2.0-* and veo-2-* both match
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        param_aliases={"duration": "duration_seconds"},
        param_coercers={"duration_seconds": str},
        param_constraints=(_check_veo_aspect_ratio, _check_veo_resolution, _check_veo_duration),
    ),
    description="Google Veo 2 — text-to-video. No synchronized audio.",
    example_slugs=("veo-2.0-generate-001",),
    probe=google_models_get_probe,
)

GOOGLE_VEO_FAMILY = ModelFamily(
    name="google-veo",
    pattern=re.compile(r"^veo-"),  # catch-all: everything not legacy
    spec_template=ModelSpec(
        model_id="*",
        modality=Modality.VIDEO,
        param_aliases={"duration": "duration_seconds"},
        param_coercers={"duration_seconds": str},
        param_constraints=(_check_veo_aspect_ratio, _check_veo_resolution, _check_veo_duration),
        extras={"has_audio": True},
    ),
    description="Google Veo 3+ — text-to-video with synchronized audio.",
    example_slugs=("veo-3.0-generate-001", "veo-3.0-fast-generate-001"),
    probe=google_models_get_probe,
)

# Order matters: legacy must come before catch-all (first-match-wins)
provider_families=(GOOGLE_VEO_LEGACY_FAMILY, GOOGLE_VEO_FAMILY)
```

**Provider change.**

```python
# Before
is_veo3 = step.model.startswith("veo-3")

# After
spec = self._models.get(step.model)
has_audio = bool(spec.extras.get("has_audio"))
```

**Why not a typed `has_audio` field on `ModelSpec`?** One-field-doesn't-justify
new-base-class. If we accumulate 3+ video-capability flags, formalize
as `extras["video"]: VideoCapabilities` (TypedDict). Today, one
`extras["has_audio"]` per family with a key constant in
`canonical_params.py` is the right scale.

**Tests.**
- veo-2.0 → no `has_audio` in spec extras
- veo-3.0 → `has_audio=True`
- veo-3.0-fast → `has_audio=True`
- Hypothetical veo-4.0 → `has_audio=True`
- Hypothetical veo-2.5 → routed to legacy, no `has_audio`
- Family ordering test: legacy comes before catch-all in the registry's `provider_families` tuple.

### B4 — `_cached_probe` `try/finally` + delegated probe deadline

**Issues stacked.** Three concurrent problems in the same code path:
1. Cleanup not in `finally` → `BaseException` orphans the in-flight Event.
2. Stale `_probe_inflight` entry permanently makes new callers waiters on a dead event.
3. No timeout on the probe invocation itself → indefinite hang under the single-flight lock.

**Design (v3 — timeout delegated to the connector's HTTP client; framework adds `try/finally` only).**

The earlier draft wrapped probes in `concurrent.futures.ThreadPoolExecutor`
to enforce a deadline. That was over-engineering: every shipped
`FamilyProbe` is HTTP-based, and `httpx.Client(timeout=...)` already
provides a connect/read deadline that cancels the underlying socket
on expiry. A futures wrapper would add a thread layer and silently
fail to cancel the in-flight HTTP request (the future's `cancel()`
returns `False` once running). The right primitive is the existing
HTTP client.

```python
def _cached_probe(self, slug: str, *, refresh: bool = False) -> LiveProbeResult:
    # ... Phase 1+2 unchanged (cache hit / waiter election) ...

    # Phase 3 (elected fetcher) — guaranteed cleanup via try/finally.
    # Probe duration is bounded by the connector's own httpx.Client
    # timeout (or the SDK client's equivalent for non-httpx probes).
    # The framework does NOT wrap probes in a futures timeout: that
    # would add a thread layer without actually cancelling the HTTP
    # request, and would serialize probes through a single worker.
    result: LiveProbeResult = LiveProbeResult.UNKNOWN  # fail-open default
    try:
        result = self._invoke_family_probe(probe, slug)
    except Exception as exc:
        logger.warning("family.probe raised for %s: %s", slug, exc)
        result = LiveProbeResult.UNKNOWN
    finally:
        with self._probe_cache_lock:
            event = self._probe_inflight.pop(slug, None)
            if result is not LiveProbeResult.UNKNOWN:
                self._probe_cache[slug] = (time.monotonic(), result)
                self._evict_probe_cache_if_oversized()
            if event is not None:
                event.set()

    return result
```

The `try/finally` placement is the entire core fix:

- **`finally` runs even on `BaseException`.** `KeyboardInterrupt` /
  `SystemExit` propagate through after cleanup completes, so
  `_probe_inflight` never carries a stale entry.
- **`_probe_inflight.pop` is unconditional.** The previous design
  popped only inside the `with` block on the success path; under the
  `BaseException` path, the entry was never removed. Now it is.
- **Cache write happens only on definitive results** (LIVE / DEAD).
  `UNKNOWN` (transport failure, timeout) does not poison the cache.

**FamilyProbe contract addition.**

The `FamilyProbe` callable contract (in `family.py` docstring) gains
one paragraph:

> Probes must respect a bounded duration via their underlying
> transport (`httpx.Client(timeout=...)` for HTTP probes, the
> equivalent for SDK-based probes). The framework guarantees
> exception-safe single-flight cleanup but does *not* wrap the probe
> in a separate timeout — a probe that blocks indefinitely will hold
> the single-flight lock for that slug indefinitely.

The connector-level audit (Phase 2) confirms each shipped probe's
HTTP client has a timeout. NVIDIA's `_base.py::NvidiaClient` already
sets `http_timeout=120.0` (too generous for a probe — recommend
overriding to a smaller value at probe-call sites). Google's genai
client uses the upstream SDK's default. GMICloud uses `httpx_timeout`
on its `_base.py`.

**No new ctor kwarg.** The earlier draft proposed
`probe_invocation_timeout_s`. Dropped — the timeout already lives on
the connector's HTTP client; adding a parallel knob would split the
control surface. If a future use case needs probe timing distinct from
the rest of the connector's HTTP traffic, that's a per-connector
discussion, not a framework-wide knob.

**Tests.**
- Probe raising `Exception` → `UNKNOWN`, `_probe_inflight` clean, event set.
- Probe raising `BaseException` (use a custom exception subclassing
  `BaseException` to avoid actually killing the test runner) →
  cleanup runs in `finally`, exception propagates.
- Probe blocking past the connector's HTTP timeout → underlying
  `httpx.ReadTimeout` reaches the framework as `Exception` →
  `UNKNOWN`, cleanup runs.
- Concurrent calls during a hung probe: waiters wait
  `_PROBE_INFLIGHT_WAIT_SECONDS`, then fall through to `UNKNOWN`
  (existing behavior preserved).
- After 10 consecutive `BaseException`-raising probes, `_probe_inflight`
  is empty (regression pin against the original stale-entry bug).

### H-CONC — `_poll_cache` thread safety

Add `self._poll_cache_lock = threading.Lock()` (non-reentrant — none
of `_cache_poll_result`, `_get_cached_poll_result`, or
`_cleanup_poll_cache` re-enter each other; `Lock` is faster than
`RLock`). The lock guards both `_poll_cache` and `_poll_cache_times`
because they're written/read together.

```python
def _cache_poll_result(self, prediction_id, result):
    with self._poll_cache_lock:
        self._poll_cache[prediction_id] = result
        self._poll_cache_times[prediction_id] = time.monotonic()

def _get_cached_poll_result(self, prediction_id):
    with self._poll_cache_lock:
        result = self._poll_cache.pop(prediction_id, None)
        self._poll_cache_times.pop(prediction_id, None)
        return result

def _cleanup_poll_cache(self):
    cutoff = time.monotonic() - _POLL_CACHE_TTL_SECONDS
    with self._poll_cache_lock:
        stale = [pid for pid, ts in self._poll_cache_times.items() if ts < cutoff]
        for pid in stale:
            self._poll_cache.pop(pid, None)
            self._poll_cache_times.pop(pid, None)
```

**On the `threading.Lock` vs `asyncio.Lock` question.** `ainvoke`
dispatches via `asyncio.to_thread`, so the lock is acquired from a
worker thread — `threading.Lock` is correct. `asyncio.Lock` would be
wrong (it can only be `await`ed, not synchronously acquired from a
thread).

Tests (parametrized concurrency):
- Two `ainvoke` calls race on the same `prediction_id`: exactly one
  returns the cached result, the other returns `None`. No double-pop.
- Cleanup races with reads: stale entries removed without disrupting
  in-flight `_get_cached_poll_result` calls.
- 8-thread stress test (matches the connector compliance harness's
  pool sizing): 100 cycles of write/read/cleanup with assertions on
  invariants. Run with `pytest --count=20` (pytest-repeat) so a flaky
  race manifests rather than passing once and pretending to be safe.

### H4 — `register_pricing` family-aware fallthrough

```python
def register_pricing(self, model_id: str, pricing: PricingStrategy) -> None:
    with self._lock:
        existing = self._user.get(model_id)
        if existing is None:
            # Preserve family param contracts: clone the family-resolved
            # spec before applying pricing, rather than minting a bare spec.
            match = self.match_family(model_id)
            existing = match.spec if match is not None else ModelSpec(model_id=model_id)
        updated = _replace(existing, pricing=pricing)
        self._user[model_id] = updated
```

Tests:
- Register pricing on a family-matched slug → resolved spec retains
  param_aliases, param_constraints, extras from the family.
- Register pricing on a slug with no family match → bare `ModelSpec`
  with only pricing set (existing behavior).
- Register pricing on a slug already in `_user` → existing user spec is
  preserved, only pricing replaced (existing behavior).

### H5 — `register_family` cap enforcement (per-layer caps; user not blocked by connector)

**Why the earlier "combined cap" design was wrong.** Counting user
families against the same `MAX_PROVIDER_FAMILIES = 32` cap meant a
connector at 28 provider families would block a user from registering
more than four user families — at zero fault of the user. The cap's
purpose is to bound linear-scan cost, but user families are
user-scoped (one process; user controls their own perf) while
provider families are connector-shipped (every user pays the cost).
The right policy is two separate caps with different motivations.

**Design.**

```python
# In family.py — alongside MAX_PROVIDER_FAMILIES
MAX_PROVIDER_FAMILIES: int = 32
"""Hard cap on connector-shipped families per registry. Bounds the
linear-scan cost every consumer pays. Connectors hitting the cap
should consolidate patterns or split the registry by modality."""

MAX_USER_FAMILIES: int = 32
"""Hard cap on user-registered families per registry. Bounds the
linear-scan cost the user has explicitly opted into. Per-user; does
not interact with MAX_PROVIDER_FAMILIES."""

# In model_registry.py
def register_family(self, family: ModelFamily) -> None:
    with self._lock:
        if len(self._user_families) >= MAX_USER_FAMILIES:
            raise ValueError(
                f"Registry already has {len(self._user_families)} user families; "
                f"cap is {MAX_USER_FAMILIES}. Consolidate patterns or call "
                f"fork() to start fresh."
            )
        self._user_families.insert(0, family)
        if family.unstable_examples:
            self._unstable_slugs = self._unstable_slugs | frozenset(family.unstable_examples)
```

The error message names the user layer explicitly — no confusion
about "provider families" when the user triggered the call.

The total scan cost is bounded at `MAX_PROVIDER_FAMILIES + MAX_USER_FAMILIES = 64`,
which the perf budget in the original plan accommodates (linear scan
of 64 compiled regexes is still well under the 100µs adversarial
target).

Tests:
- Registry at user-family cap → next `register_family()` raises with
  user-specific error message.
- Connector with 32 provider families + user registers 32 user
  families → both succeed; the 65th raises.
- Cap message names "user families", not "provider families".

### H6 — `fork()` carries `_warned_deprecated`

```python
clone._user_families = list(self._user_families)
clone._warned_deprecated = set(self._warned_deprecated)  # NEW
```

Test:
- Parent registry warned about deprecated alias → `fork()` clone does
  not re-warn on the same slug.

### H7 — Ctor kwargs + conformance test (root-cause)

**Connector edits (mechanical, identical pattern):**
```python
def __init__(
    self,
    ... existing params ...,
    *,
    models: ModelRegistry | None = None,
    retry_policy: RetryPolicy | None = None,
    probe_cache_ttl: float | None = None,
    probe_cache_max_entries: int | None = None,
):
    super().__init__(
        models=models,
        retry_policy=retry_policy,
        probe_cache_ttl=probe_cache_ttl,
        probe_cache_max_entries=probe_cache_max_entries,
    )
```

Two kwargs (not three — `probe_invocation_timeout_s` was dropped per
the revised B4 design; probe deadlines live on the connector's HTTP
client).

Apply to: LMNT, Replicate, NvidiaChatProvider, NvidiaAudioProvider,
NvidiaVideoProvider, NvidiaImageProvider.

**Conformance test (the actual root-cause fix):**

```python
# libs/core/genblaze_core/testing.py — new method on ProviderComplianceTests
def test_accepts_probe_cache_kwargs(self):
    """Every Provider subclass must accept the probe-cache ctor kwargs.

    These are no-ops on NATIVE / NONE providers but must be accepted
    for API uniformity — calling code that passes them to ANY provider
    should not raise TypeError. The test calls the constructor with
    the kwargs rather than only inspecting the signature, so a
    ``**kwargs``-forwarding provider that doesn't actually accept the
    names also fails.
    """
    cls = type(self.make_provider())
    # Build a fresh instance with the kwargs; if the provider rejects
    # them with TypeError, the test fails with a clear message.
    try:
        cls(**self.constructor_kwargs_for_probe_cache_test(),
            probe_cache_ttl=120.0,
            probe_cache_max_entries=64)
    except TypeError as exc:
        raise AssertionError(
            f"{cls.__name__} must accept probe_cache_ttl and "
            f"probe_cache_max_entries kwargs (forward to super().__init__()). "
            f"Got: {exc}"
        ) from exc

def constructor_kwargs_for_probe_cache_test(self) -> dict:
    """Override to provide the minimum kwargs your provider needs.

    Default returns an empty dict. Connectors with required ctor args
    (e.g. ``api_key``) override this to provide test stubs.
    """
    return {}
```

Two-pronged check: signature inspection alone misses
`**kwargs`-forwarders (a provider that does `def __init__(self, **kwargs):
super().__init__(**kwargs)` looks signature-compliant but never
declared the kwarg names). Calling the constructor catches that case
because the kwargs propagate and `BaseProvider.__init__` accepts
them — but a connector that intercepts kwargs and forgets to forward
fails with `TypeError`.

Inherited by every connector's `ProviderComplianceTests` subclass —
the test discovers gaps automatically. This is what would have caught
the original drift in PR #2 before it propagated.

### H8 — LMNT + Replicate `test_catalog_decoupling.py`

Both files mirror the rubric used by other connectors:
- DiscoverySupport declaration.
- For LMNT: family resolution (none — fallback only); pricing-removed
  contract.
- For Replicate: family resolution (none — fallback only);
  `_fetch_models()` returns the upstream catalog filtered by no
  pattern (Replicate is meta-vendor; every slug is valid); pricing
  removed contract.
- Cross-provider isolation (no slugs from other connectors match).

### H9, H10, B1, B2, H1, H2, H3, H-DEPR — docs

Each is a focused edit, listed in the file-by-file section below.

---

## File-by-file changes

| File | Changes |
|---|---|
| `libs/core/genblaze_core/providers/base.py` | `validate_chain_input_url` hardening (B-NEW); `_cached_probe` try/finally cleanup (B4); `_poll_cache_lock` + lock all read/write paths (H-CONC); `probe_model()` deprecation warning includes removal version + verify `stacklevel=2` (H-DEPR) |
| `libs/core/genblaze_core/providers/family.py` | Add `MAX_USER_FAMILIES` constant alongside `MAX_PROVIDER_FAMILIES`; tighten `FamilyProbe` docstring on bounded-duration contract (B4) |
| `libs/core/genblaze_core/providers/__init__.py` | Re-export `MAX_USER_FAMILIES` alongside `MAX_PROVIDER_FAMILIES` (public surface; mirrors the existing pattern) |
| `libs/core/genblaze_core/providers/model_registry.py` | `register_pricing` family-aware fallthrough (H4); `register_family` per-layer cap on user families (H5); `fork()` carries `_warned_deprecated` (H6) |
| `libs/core/genblaze_core/testing.py` | `test_accepts_probe_cache_kwargs` calls ctor with kwargs (H7 root); `constructor_kwargs_for_probe_cache_test()` override hook |
| `libs/core/tests/unit/test_validate_chain_input_url.py` | NEW — parametrized corpus ≥20 cases (B-NEW) |
| `libs/core/tests/unit/test_probe_cache_concurrency.py` | NEW — try/finally cleanup + stale-entry regression + race tests (B4) |
| `libs/core/tests/unit/test_poll_cache_concurrency.py` | NEW — Lock guards write/read/cleanup; 8-thread stress (H-CONC) |
| `libs/core/tests/unit/test_model_registry.py` | Extend with H4 (`register_pricing` family-aware), H5 (cap on user families), H6 (`fork()` warning state) cases |
| `libs/connectors/google/genblaze_google/_families.py` | Split into `GOOGLE_VEO_LEGACY_FAMILY` + `GOOGLE_VEO_FAMILY`; ordering matters (B3) |
| `libs/connectors/google/genblaze_google/provider.py` | Read `extras["has_audio"]`; remove `startswith("veo-3")` (B3) |
| `libs/connectors/google/tests/test_catalog_decoupling.py` | Family ordering + has_audio assertions (B3) |
| `libs/connectors/{lmnt,replicate}/genblaze_*/provider.py` | Add 2 ctor kwargs (H7) |
| `libs/connectors/nvidia/genblaze_nvidia/{audio,video,image,chat_provider}.py` | Add 2 ctor kwargs (H7) |
| `libs/connectors/lmnt/tests/test_catalog_decoupling.py` | NEW (H8) |
| `libs/connectors/replicate/tests/test_catalog_decoupling.py` | NEW (H8) |
| `docs/guides/migrating-to-0.3.md` | Drop `KNOWN_UNSTABLE` (B1); rewrite `Pipeline.preflight()` paragraph — clarify it's a fluent setter and validation runs inside `run()` (B2); add `UNKNOWN_PERMISSIVE` row to outcome table (H1); add `FALLBACK` to source table (H2); add LMNT to NONE list (H3); fix `discover_models()` claim — returns `DiscoveryResult.unsupported(...)` (H9); document `validate_chain_input_url(file_root_allowlist=...)` opt-in (B-NEW); deprecation horizon (H-DEPR); add "(revised 2026-05-08)" header note |
| `docs/features/model-registry.md` | Drop `KNOWN_UNSTABLE` (B1); add `UNKNOWN_PERMISSIVE` row (H1); add `FALLBACK` to source table (H2); document `MAX_USER_FAMILIES` cap (H5) |
| `docs/guides/new-provider.md` | Bump pyproject template constraint to `>=0.3.0,<0.4` (H10); add `validate_chain_input_url` security guidance section (B-NEW) |
| `CHANGELOG.md` | Document `0.3.1` patch entries (release strategy, see below); deprecation horizon row (H-DEPR); B-NEW security note |

---

## Release strategy

The 14-PR rollout is internally tagged `0.3.0 [Unreleased]` in the
CHANGELOG; nothing has been published to PyPI yet. This hardening pass
becomes the actual `0.3.0` release — the `[Unreleased]` block gains
the hardening entries, then ships as `0.3.0` end-to-end. Connector
package versions follow:

- **`genblaze-core`** — bump to `0.3.0` on release (was `0.2.8`).
- **Connector packages with code edits in this plan** (LMNT, Replicate,
  NVIDIA chat/audio/video/image, Google) — patch-level bump (e.g.
  `0.2.x` → `0.3.0` to track core compatibility).
- **Connector packages without code edits in this plan** (OpenAI,
  ElevenLabs, GMICloud, Decart, Runway, Luma, Stability-Audio) —
  stay on their current versions; their pinned `genblaze-core` dep
  range is updated to `>=0.3.0,<0.4` only.

If urgent issues are discovered post-`0.3.0` ship, follow-up patches
go to `0.3.1`. **No fixes from this plan ship as `0.3.1`** — the goal
is one clean `0.3.0` release.

---

## Acceptance criteria

1. `make lint && make typecheck` clean.
2. `make test` clean (core + every connector + cli).
3. **Concurrency tests pass under `pytest --count=20` (pytest-repeat)**
   — covers the new `test_probe_cache_concurrency.py` and
   `test_poll_cache_concurrency.py` files (both listed in the
   file-by-file table). Repeated runs surface races that pass once
   and lie about safety.
4. New conformance test `test_accepts_probe_cache_kwargs` passes for
   every provider class — verified by **calling the constructor**
   with the kwargs, not just signature inspection.
5. New `test_catalog_decoupling.py` files for LMNT + Replicate green.
6. `validate_chain_input_url` rejects every entry in a parametrized
   corpus of ≥20 cases including percent-encoded `..` variants and
   macOS canonical aliases.
7. `_cached_probe` has zero lingering `_probe_inflight` entries after
   any test run that exercises a probe failure (assert via test
   teardown).
8. Veo `has_audio` derives from spec extras; `git grep "startswith.*veo"`
   returns nothing in `provider.py`.
9. Migration guide + model-registry.md outcome tables match
   `validation.py` exactly (every enum value present, no phantoms);
   `git grep "KNOWN_UNSTABLE"` returns nothing in `docs/`; `git grep
   "preflight() returns"` returns nothing.
10. `verify-docs` skill all four checks remain green.
11. `register_family()` raises with a user-specific message when the
    user-family cap is hit, regardless of how many provider families
    the connector ships.
12. `_poll_cache` 8-thread stress test (100 cycles) shows zero
    double-pops, zero torn reads under `pytest --count=20`.
13. Every changed package's `python -m build && twine check dist/*`
    passes (catches pyproject regressions before release).

---

## Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `validate_chain_input_url` denylist drift: a new platform-specific sensitive path emerges (e.g. new Kubernetes secrets mount, new `cgroup` namespace) | Medium | Medium | Default mode is best-effort; deployments accepting user-controlled URLs MUST use `file_root_allowlist`. Denylist is documented as non-exhaustive in the function docstring. Add new entries as they're reported |
| `validate_chain_input_url` percent-encoded bypass surface — double or triple URL-encoding | Low | Medium | `unquote()` handles single-pass percent-encoding; `Path.resolve()` collapses traversal regardless of how the input was encoded. Test corpus covers `%252F` (double-encoded `/`) — if a multi-pass encoding exploits, test corpus expands. Allowlist mode catches all of these by canonicalizing first |
| Veo split missing a future variant — e.g. `veo-2.5-pro` shipped with audio later but matches legacy | Low | Medium | Family pattern `^veo-2[.-]` is tight; `veo-2.5-pro` matches legacy and gets `has_audio=False` by mistake. Acceptable trade-off given today's catalog; will revisit when Google announces. Long-term root-cause is detecting `has_audio` from the API response payload, not the slug — deferred to a follow-up plan because it requires reading the response shape |
| Probe held forever by an HTTP client without a timeout | Low | Medium | `FamilyProbe` contract (in `family.py` docstring) makes the bounded-duration requirement explicit; connector audit confirms each shipped probe's HTTP client has a finite timeout. Connectors that ship a probe without one fail review |
| `_poll_cache_lock` adds contention on hot polling paths | Very Low | Low | `threading.Lock` (not RLock); reads are <1µs; polling cadence is seconds-to-minutes; benchmark before merge if concern |
| Conformance test (calling ctor with kwargs) requires connectors to provide `constructor_kwargs_for_probe_cache_test` override when their ctor has required positional args | Low | Low | One line of test override per connector; defaulted to empty so providers with optional-only ctor args (most) need no override |
| Doc-only fixes silently break copy-paste of old prose in third-party tutorials | Low | Low | Migration guide is new (just shipped 2026-05-07); unlikely cited externally. Add a `(revised YYYY-MM-DD)` header note so external readers know to re-read |
| Test for `_cached_probe` `BaseException` cleanup is hard to write portably | Medium | Low | Use a custom `class _BoomBase(BaseException): pass` and patch `_invoke_family_probe` to raise it. Assert `_probe_inflight` empty in test teardown. Works in pytest without killing the runner |
| User-family cap (32) too low for power-users registering many private model lines | Very Low | Low | If reported, raise the cap or add a `MAX_USER_FAMILIES_OVERRIDE` env var. 32 is a generous default — typical registries ship <10 |

---

## Test strategy

- **Unit tests**: per-fix tests added in the same PR as the fix.
- **Conformance**: `test_accepts_probe_cache_kwargs` is the gate
  against future drift on H7.
- **Property-style for B-NEW**: parameterized list of malicious /
  benign URLs; assert reject/accept symmetrically.
- **Concurrency for B4 + H-CONC**: stress test with
  `concurrent.futures.ThreadPoolExecutor(max_workers=8)` racing on the
  same slug / prediction id; assert no deadlocks, no torn reads, no
  stale entries.
- **Regression**: existing test suites for every connector continue to
  pass without modification (modulo the 6 ctor-kwarg additions whose
  tests gain a kwarg-acceptance assertion via the new conformance test).

---

## Out of scope (explicitly)

- MEDIUM/LOW/NIT items from the deep review (separate cleanup PR).
- Sample app rewrites (PR #15, separate repos — user is handling).
- New connectors / new modalities.
- API additions beyond what each fix requires.
- Performance benchmarks for the µs claims in `model-registry.md`.
- `classify_api_error` test corpus.
- Behavior changes to the existing `Pipeline.preflight()` setter — only
  doc rewrites.

---

## Estimated work

| Phase | Items | Effort |
|---|---|---|
| Phase 1 — core | 6 fixes | ~1 day |
| Phase 2 — connectors + conformance | 4 fixes (across 7 files) | ~½ day |
| Phase 3 — docs | 7 fixes | ~½ day |
| **Total** | **17 fixes** | **~2 days** |

Tests are the bulk of the time: ~50% of effort. That's the right ratio
for a production-readiness PR — every fix gets a regression pin.
