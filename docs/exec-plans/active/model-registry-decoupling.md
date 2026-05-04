<!-- last_verified: 2026-05-04 -->
# Model registry decoupling

**Status:** draft V2 (post-red-team) · **Owner:** core · **Target release:**
`genblaze-core 0.3.0` · **Shape:** breaking — soft-launch, no backcompat shims
· **Feedback refs:** F-2026-05-04-01 (NVIDIA `riva-tts` 404) and the broader
catalog-rot class it represents (GMI April reconciliation, etc.).

> **Red-team status:** V1 was reviewed by an independent reviewer and produced
> 11 substantive issues, 3 classified "must fix before implementation
> starts." All are addressed inline in V2 — see "Red-team resolutions"
> appendix at the end.

## Problem

The SDK ships editorial content — hardcoded upstream model slugs — inside
library releases. Slug churn upstream (NVIDIA renames Riva → Magpie, GMI rolls
TTS variants, OpenAI deprecates DALL-E 2) creates rot whose half-life is
shorter than our release cadence. Today's symptoms:

- `genblaze_nvidia/models/audio.py` ships `nvidia/riva-tts` which now 404s
  from NIM. Permissive fallback masks it; the user sees a mid-pipeline
  `ProviderError(MODEL_ERROR, 404)`.
- GMI's April reconciliation found every audio default 404'd against the
  live `/requests` endpoint. The connector now flags `extras["suspected_dead"]`
  on entire model families as a stop-gap.
- Pricing tables (~50 references across 11 generative connectors) inherit
  the same rot one layer down: rates change without the SDK noticing.

These are not connector bugs. They're the consequence of conflating two
unrelated concerns inside one tuple:

1. **Slug list** — high rot, owned by upstream, churns between our releases.
2. **Param-shape rules** — low rot, real engineering value (SDXL needs
   `text_prompts` wrapping, FLUX takes `aspect_ratio`, GMI wraps in
   `{"payload": ...}`, NVIDIA video2world routes the video slot).

Slugs change; param contracts within a family don't. Today they live in the
same data structure, so curating one forces curating the other.

## Goal

Stop shipping slug lists. Keep param-shape rules, keyed by model family
(regex pattern), so the SDK's authority surface matches what it can
actually own truthfully. Slugs become user inputs and (where the upstream
exposes a catalog) discovery results — never SDK constants.

## Non-goals

- Network-on-import. Registry construction stays in-memory and synchronous.
- Pricing infrastructure expansion. We **phase out** SDK-shipped pricing.
  No companion `genblaze-pricing` package, no pattern-keyed pricing
  inheritance, no new pricing strategies. `register_pricing()` stays as the
  user-side contract for cost tracking. A one-shot cookbook
  (`docs/reference/pricing-recipes.md`) preserves the last-known prices
  as copy-paste recipes; it is **not** maintained going forward.
- Backcompat shims. genblaze is in soft launch; we make the clean break in
  0.3.0. Deprecation cycles are reserved for post-1.0.
- Probe-CI for connectors without discovery. We classify support honestly.

## Architecture

Five principles, refined post-red-team:

1. **The SDK owns shapes, not slugs.** Param contracts live as
   `ModelFamily` rules keyed by regex pattern. Slugs do not appear in
   `models/*.py` as defaults.
2. **Discovery is the upstream's job, with honest classification.**
   Connectors declare `DiscoverySupport ∈ {NATIVE, PARTIAL, NONE}`. The
   classification drives validation outcomes — a `PARTIAL` provider can
   never return `OK_AUTHORITATIVE` from family match alone (RT-1).
3. **Validation surfaces drift before the wire — but only as honestly as
   the provider can support.** Three outcomes: `OK_AUTHORITATIVE` (slug
   confirmed live by discovery or provider-supplied probe), `OK_PROVISIONAL`
   (matched a family pattern but liveness unverifiable), `NOT_FOUND` (slug
   absent from authoritative catalog). Pipeline preflight gates each
   independently.
4. **Family liveness probing is the PARTIAL escape hatch.** Families on
   `PARTIAL` providers may declare an optional `probe: FamilyProbe`
   callable that performs a cheap upstream liveness check (HEAD,
   empty-payload POST returning 400≠404). Probe results cache per-slug
   per-process, with TTL.
5. **Validation primitives collapse, not proliferate.** `validate_model()`
   subsumes `probe_model()`. The legacy `probe_model()` becomes a thin
   adapter that delegates to `validate_model(refresh=True)` and returns
   a coerced `ProbeResult` for the existing `tools/probe_models.py`
   consumers. Single source of truth for "is this slug usable" (RT-8).

### New core types

```
libs/core/genblaze_core/providers/
├── family.py          (NEW)  ModelFamily, DiscoverySupport, FamilyProbe
├── discovery.py       (NEW)  DiscoveryResult, DiscoveryStatus, _DiscoveryCache
├── validation.py      (NEW)  ValidationResult, ValidationSource, ValidationOutcome
└── pattern_safety.py  (NEW)  pattern-safety guard (RT-9)
```

#### `family.py`

```python
class FamilyProbe(Protocol):
    """Cheap upstream liveness check for a family-matched slug.

    Returns one of:
      - LiveProbeResult.LIVE  : upstream confirmed slug is callable
      - LiveProbeResult.DEAD  : upstream confirmed slug is missing
      - LiveProbeResult.UNKNOWN : auth/network/other — caller decides

    Implementations must be cheap (one round-trip, no token spend) and
    polite (do not create persistent records in the upstream's audit log
    if avoidable).
    """
    def __call__(self, slug: str, *, http: httpx.Client) -> LiveProbeResult: ...


@dataclass(frozen=True, slots=True)
class ModelFamily:
    """Pattern-keyed param-shape rule.

    A family is the SDK's unit of authority over model behavior. It claims:
    "any slug matching this pattern uses this spec_template." Slugs are
    never stored; only the pattern, optional liveness probe, and editorial
    example_slugs (used for docs and nearest-neighbor suggestions).
    """
    name: str                          # "nvidia-cosmos-video2world", "sdxl"
    pattern: re.Pattern                # validated for safety at construction
    spec_template: ModelSpec           # param shape, transformers, schemas
    description: str                   # one line for docs / error messages
    example_slugs: tuple[str, ...] = ()
    probe: FamilyProbe | None = None    # optional liveness check (RT-1)
    discovery_required: bool = False    # if True, fallback alone is insufficient

    def __post_init__(self) -> None:
        # RT-9: reject patterns prone to catastrophic backtracking at
        # construction time. Run via pattern_safety.assert_safe.
        from genblaze_core.providers.pattern_safety import assert_safe
        assert_safe(self.pattern)


class DiscoverySupport(StrEnum):
    """Per-provider declaration of upstream catalog API support."""
    NATIVE = "native"
        # GET /v1/models or equivalent — authoritative for the provider's
        # full surface. NATIVE-matched slugs from family resolution
        # are upgraded to OK_AUTHORITATIVE iff the slug is in the live cache.
    PARTIAL = "partial"
        # Catalog exists but doesn't enumerate every endpoint (e.g.,
        # NVIDIA chat /v1/models doesn't list /genai/* endpoints).
        # PARTIAL providers cannot return OK_AUTHORITATIVE from family
        # match alone — they require a family-level probe (FamilyProbe)
        # OR fall back to OK_PROVISIONAL with a strong WARN.
    NONE = "none"
        # No catalog API. Family match returns OK_PROVISIONAL.
        # User owns slug freshness; the SDK is honest that it cannot verify.


class LiveProbeResult(StrEnum):
    LIVE = "live"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FamilyMatch:
    """The result of resolving a slug against a registry's families.

    `spec` is the family's `spec_template` with `model_id` substituted to
    the matched slug. Returned from `ModelRegistry.match_family()`.
    """
    family: ModelFamily
    spec: ModelSpec
```

#### `discovery.py`

```python
class DiscoveryStatus(StrEnum):
    OK = "ok"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    status: DiscoveryStatus
    slugs: frozenset[str] = frozenset()
    fetched_at: float | None = None    # monotonic
    source_url: str | None = None
    detail: str | None = None


_DEFAULT_TTL_SECONDS = 3600.0  # 1 hour — RT-6: long-running daemons


class _DiscoveryCache:
    """Per-provider, single-flight, thread-safe cache.

    Concurrency model:
      - threading.RLock around cache state
      - Single-flight: at most one in-flight fetch per provider via
        threading.Event. Concurrent callers block on the same fetch.
      - Default TTL of 3600s; callers may override per-call.
      - Discovery retries use the existing RetryPolicy machinery
        (exponential backoff + jitter, capped at 3 retries) — RT-11a.
      - On rate-limited (429) failures, returns FAILED and respects
        Retry-After headers; subsequent fetches throttled until window
        clears.

    The fetcher is a Callable[[], DiscoveryResult] passed at construction.
    Per RT-11b, the conformance test for NATIVE providers asserts the
    fetcher returns DiscoveryResult(status=OK) against staging credentials
    — catches connectors that fail to forward auth.
    """
    def __init__(
        self,
        fetcher: Callable[[], DiscoveryResult],
        *,
        default_max_age_seconds: float | None = _DEFAULT_TTL_SECONDS,
        retry_policy: RetryPolicy | None = None,
    ) -> None: ...

    def get(self, *, max_age_seconds: float | None = None) -> DiscoveryResult: ...
    def invalidate(self) -> None: ...
```

#### `validation.py`

```python
class ValidationSource(StrEnum):
    USER = "user"
    FAMILY = "family"
    DISCOVERY = "discovery"
    PROBE = "probe"          # family.probe() returned LIVE
    FALLBACK = "fallback"


class ValidationOutcome(StrEnum):
    OK_AUTHORITATIVE = "ok_authoritative"
        # SDK has positive confirmation: user-registered, OR
        # FAMILY-matched ∧ NATIVE-discovery-confirmed, OR
        # FAMILY-matched ∧ family.probe() returned LIVE.
    OK_PROVISIONAL = "ok_provisional"
        # FAMILY-matched but liveness unverifiable
        # (PARTIAL/NONE provider, no probe, no discovery hit).
    UNKNOWN_PERMISSIVE = "unknown_permissive"
        # No family match; permissive fallback applies.
        # User passes through to upstream; we cannot pre-flight.
    NOT_FOUND = "not_found"
        # NATIVE discovery says absent, OR family.probe() returned DEAD.


@dataclass(frozen=True, slots=True)
class ValidationResult:
    outcome: ValidationOutcome
    source: ValidationSource
    family_name: str | None = None
    detail: str | None = None
    suggested_slugs: tuple[str, ...] = ()  # nearest-neighbor for NOT_FOUND
```

#### `pattern_safety.py` (RT-9)

```python
"""Reject regex patterns prone to catastrophic backtracking.

Strategy: prefer google-re2 if installed (linear-time guarantee). Fall back
to a static AST-based check that rejects unbounded quantifiers nested
inside groups (e.g., `(a+)+`, `(a|a)*`, `(.+)+`). Patterns failing the
check raise ValueError at ModelFamily construction time — fast-failure
during connector import, before any user code runs.
"""
import re
from typing import Final

_UNSAFE_PATTERNS: Final = re.compile(
    r"\([^)]*[+*][^)]*\)\s*[+*]"  # nested unbounded quantifiers
)

def assert_safe(pattern: re.Pattern) -> None:
    if _UNSAFE_PATTERNS.search(pattern.pattern):
        raise ValueError(
            f"Pattern {pattern.pattern!r} has nested unbounded quantifiers "
            f"and is rejected for risk of catastrophic backtracking. "
            f"Rewrite the pattern or install google-re2 for linear-time matching."
        )
```

### Modified core types

#### `model_registry.py`

`ModelRegistry` becomes:

- Constructor: `ModelRegistry(provider_families=(), fallback=FALLBACK_SPEC, *, strict_params=False, discovery_cache=None)`. The `defaults` mapping is **removed** in PR #13 (see RT-4 for migration shim shape).
- Two family layers (RT-3):
  - `_provider_families: tuple[ModelFamily, ...]` — frozen post-construction; from connector.
  - `_user_families: list[ModelFamily]` — RLock-guarded; user-registered, prepended (precedence).
- New methods:
  - `match_family(model_id) -> FamilyMatch | None` — checks `_user_families` first, then `_provider_families`. First-match-wins within each layer. Lock-free read of frozen tuple, RLock-guarded read of user list (snapshot copy).
  - `validate(model_id) -> ValidationResult` — non-network. Checks user registry → user families → provider families → discovery cache (no fetch) → fallback. Returns the strongest outcome each source can substantiate (RT-1).
  - `validate_with_discovery(model_id, *, refresh=False) -> ValidationResult` — issues discovery fetch and/or family probe as needed.
  - `register_family(family) -> None` — prepends to `_user_families` under RLock.
  - `families: tuple[ModelFamily, ...]` — public introspection (returns `tuple(_user_families) + _provider_families`).
  - `known()` — returns user-registered slugs ∪ examples from families ∪ last-discovered slugs. **Documentation-grade hint, not a contract.**
  - `__contains__(model_id)` — coherent with `validate()`: returns `True` iff `validate(model_id).outcome ∈ {OK_AUTHORITATIVE, OK_PROVISIONAL}`. `NOT_FOUND` and `UNKNOWN_PERMISSIVE` both return `False` (RT-11d, refined in final review).
- `get(model_id)` precedence: user spec → user family → provider family (returns `spec_template` with `model_id` substituted) → discovery cache → fallback. Same `DeprecationWarning` mechanic for `deprecated_aliases` is preserved.
- **Family count cap (RT-9)**: `MAX_PROVIDER_FAMILIES = 32`. Constructor raises `ValueError` if the connector ships more. Forces connectors to keep the surface tight.

#### `base.py` (`BaseProvider`)

```python
class BaseProvider:
    discovery_support: ClassVar[DiscoverySupport]   # required class attribute

    def discover_models(self, *, max_age_seconds: float | None = None) -> DiscoveryResult:
        """Default: DiscoveryResult(status=UNSUPPORTED). Override on connectors
        with /v1/models or equivalent. The fetcher must forward auth (RT-11b)."""

    def validate_model(self, model_id: str, *, refresh: bool = False) -> ValidationResult:
        """Public entrypoint. Delegates to registry.

        Behavior matrix:
        | DiscoverySupport | family match? | family.probe? | refresh? | Outcome |
        |---|---|---|---|---|
        | any              | user-registered  | n/a  | n/a   | OK_AUTHORITATIVE |
        | NATIVE           | yes              | n/a  | yes   | OK_AUTHORITATIVE if in catalog else NOT_FOUND |
        | NATIVE           | yes              | n/a  | no    | OK_AUTHORITATIVE if cached else issue fetch |
        | NATIVE           | no               | n/a  | yes   | NOT_FOUND if catalog fresh else UNKNOWN_PERMISSIVE |
        | PARTIAL          | yes + probe()    | LIVE | n/a   | OK_AUTHORITATIVE |
        | PARTIAL          | yes + probe()    | DEAD | n/a   | NOT_FOUND |
        | PARTIAL          | yes (no probe)   | n/a  | n/a   | OK_PROVISIONAL |
        | PARTIAL          | no               | n/a  | n/a   | UNKNOWN_PERMISSIVE |
        | NONE             | yes              | LIVE | n/a   | OK_AUTHORITATIVE |
        | NONE             | yes (no probe)   | n/a  | n/a   | OK_PROVISIONAL |
        | NONE             | no               | n/a  | n/a   | UNKNOWN_PERMISSIVE |

        OK_PROVISIONAL is the honest outcome when the SDK matches a family
        pattern but cannot confirm liveness. Pipeline preflight emits a WARN
        for OK_PROVISIONAL; raises for NOT_FOUND.
        """

    @deprecated("Use validate_model(refresh=True). Removed in 0.4.0.")
    def probe_model(self, model_id: str) -> ProbeResult:
        """Adapter: delegates to validate_model(refresh=True) and coerces
        the ValidationResult to a ProbeResult for legacy probe-CI consumers.
        Kept for one minor only (RT-8)."""
```

`ProbeResult` is preserved through 0.3.0 as a coerced view of
`ValidationResult` so external probe-CI consumers don't break in-flight.
Slated for removal in 0.4.0.

#### `pipeline/pipeline.py`

Extend `_validate_steps()` (existing, line 439) with a model preflight phase:

```python
def _validate_steps(self) -> None:
    self._validate_capabilities()  # existing
    if self._preflight:
        self._validate_models()    # new

def _validate_models(self) -> None:
    """Preflight: validate every step's model in parallel.

    RT-2: uses concurrent.futures.ThreadPoolExecutor (not asyncio.gather)
    to stay compatible with the sync codebase, FastAPI, Jupyter, and
    threaded daemons. AsyncPipeline path uses asyncio.gather natively.
    """
    with ThreadPoolExecutor(max_workers=min(8, len(self._steps))) as ex:
        futures = {
            ex.submit(ps.provider.validate_model, ps.model): (i, ps)
            for i, ps in enumerate(self._steps)
        }
        for fut, (i, ps) in futures.items():
            result = fut.result(timeout=self._preflight_timeout_s)
            self._handle_validation(i, ps, result)

def _handle_validation(self, i: int, ps: _PipelineStep, r: ValidationResult) -> None:
    if r.outcome == ValidationOutcome.NOT_FOUND:
        suggestions = (
            f" Did you mean: {', '.join(r.suggested_slugs[:3])}?"
            if r.suggested_slugs else ""
        )
        raise ProviderError(
            f"Step {i} ({ps.provider.name}): model {ps.model!r} not found "
            f"in upstream catalog.{suggestions} "
            f"See docs/migration/registry-decoupling.md.",
            error_code=ProviderErrorCode.MODEL_ERROR,
        )
    if r.outcome == ValidationOutcome.OK_PROVISIONAL:
        _warn_once_per_process(
            f"genblaze.preflight.provisional",
            f"Step {i} ({ps.provider.name}): model {ps.model!r} matched "
            f"family {r.family_name!r} but liveness is unverifiable on "
            f"DiscoverySupport.{ps.provider.discovery_support}. "
            f"Failures will surface mid-pipeline.",
        )
    if r.outcome == ValidationOutcome.UNKNOWN_PERMISSIVE:
        _warn_once_per_process(
            f"genblaze.preflight.unknown",
            f"Step {i} ({ps.provider.name}): model {ps.model!r} did not "
            f"match any family; permissive fallback applies.",
        )
    # OK_AUTHORITATIVE: silent.
```

`Pipeline.preflight(False)` opts out for hot paths. **Test coverage: both
paths covered explicitly** (RT-11c).

#### `_errors.py` (every connector)

Each connector's error mapper enriches 404 responses with an actionable
message. Pattern:

```python
def _enriched_model_error(model_id: str, provider_name: str) -> str:
    return (
        f"{provider_name} returned 404 for model {model_id!r}.\n"
        f" - This slug may be retired upstream. Run "
        f"provider.validate_model({model_id!r}, refresh=True) for a "
        f"deterministic check, or provider.discover_models() to refresh.\n"
        f" - Migration: docs/migration/registry-decoupling.md"
    )
```

### Pricing phase-out (per non-goal, refined per RT-7)

This plan removes pricing tables as a side effect of the family migration.
**No new pricing infrastructure is added.** Per RT-7, the impact on
enterprise cost-tracking users is **non-trivial** and acknowledged
honestly:

1. **Day 0** — every connector ships pricing on its existing slug specs.
   `compute_cost()` works.
2. **Per-connector migration PR** — defaults → families. Pricing tables
   (`_RUNWAY_PRICING`, `_PRICE_PER_CHAR`, `_SFX_DURATION_BUCKETS`,
   `_IMAGEN_PER_IMAGE_RATES`, `_RATES`, `_VIDEO_PRICING`, `_COST_PER_SEC`,
   `_PRICE_PER_SEC`, `_IMAGE_PRICE`, `_LMNT_PRICE_PER_CHAR`) are deleted.
   New `spec_template`s ship `pricing=None`.
3. **Same-PR cookbook update** — every migration PR appends a section to
   `docs/reference/pricing-recipes.md` with the deleted constants in
   `register_pricing()` form. Users who relied on `compute_cost()` can
   copy-paste the recipe. The cookbook is timestamped, marked
   "last verified YYYY-MM-DD", and explicitly disclaimed: **"prices in
   this document are not maintained; verify with the upstream before
   relying on them."**
4. **End state** — zero hardcoded prices in the SDK code. `compute_cost()`
   returns `None` for any family-resolved model without user-registered
   pricing. Cost-tracking is a user opt-in via `register_pricing()`.
5. **Helpers retained** in `genblaze_core.providers.pricing`: `per_unit`,
   `by_param`, `bucketed_by_duration`, `per_input_chars`,
   `per_input_tokens`, `per_response_metric`, `bucketed_by_duration`,
   `by_model_and_param`. User-facing primitives. No removal, no addition.
6. **CHANGELOG** documents the pricing breaking change with a one-pipeline
   recipe in the 0.3.0 entry. `Pipeline.estimated_cost()` returning `None`
   is called out explicitly.
7. **Risks table updated**: pricing impact is reclassified to **Medium
   (enterprise users with cost-estimation flows are degraded until they
   migrate to register_pricing())** — see Risk #7.

### `extras["suspected_dead"]` (per RT-10)

The GMI `suspected_dead` flag is **not** removed at migration time — its
signal is preserved through the family layer:

- For families on PARTIAL providers, an optional
  `unstable_examples: tuple[str, ...]` field lists slugs known or
  suspected dead.
- `validate_model()` for any slug in `unstable_examples` returns
  `OK_PROVISIONAL` with `detail="known_unstable; verify with discover_models()"`.
- The flag is removed only after the family's `probe` callable is
  implemented and passing in CI for the relevant slugs — i.e., we replace
  the flag with a real liveness check, not just delete the signal.

### Migration shim (RT-4)

PR #1 introduces `ModelRegistry` v2 with **both** `provider_families=()` and
the legacy `defaults={}` parameters supported. Lookup precedence during the
migration window:

1. user spec
2. user family
3. provider family
4. **legacy `defaults` dict** (removed in PR #13)
5. discovery cache
6. fallback

This is a **code-path** shim, not a regex-pattern shim. It cannot mask
NOT_FOUND outcomes for unmigrated connectors because the legacy dict
returns the same spec it does today. PR #13 deletes the `defaults=`
constructor parameter and the dict lookup path; conformance test then
asserts no `defaults=` calls remain in `libs/connectors/`.

## Connector classification

Per the audit performed for this plan:

| Connector | Catalog today | Pricing today | DiscoverySupport | Family probe? |
|---|---|---|---|---|
| `nvidia` (audio/video/image) | slug tuples | none in code | `PARTIAL` (chat-only `/v1/models`) | **yes** — empty-payload POST trick from `probe.py` |
| `nvidia` (chat) | enumerated | none | `NATIVE` (`integrate.api.nvidia.com/v1/models`) | n/a |
| `gmicloud` | catalog with `suspected_dead` | per-slug `per_unit` | `PARTIAL` | yes — empty-payload POST |
| `decart` | `_IMAGE_MODELS`, `_VIDEO_MODELS` | `per_unit`, `by_param` | `NONE` | no |
| `elevenlabs` | enumerated voices/sfx | `per_input_chars`, `bucketed` | `NATIVE` | n/a |
| `google` | enumerated | `per_unit`, custom | `PARTIAL` | yes |
| `lmnt` | already empty + fallback | `per_input_chars` | `NONE` | no |
| `luma` | enumerated | `None` | `PARTIAL` | yes |
| `openai` | enumerated | `per_input_chars`, `None` | `NATIVE` | n/a |
| `replicate` | already empty + fallback | `per_response_metric` | `NATIVE` | n/a |
| `runway` | enumerated | `by_model_and_param` | `PARTIAL` | yes |
| `stability-audio` | single-model dict | `per_second` | `NONE` | no |
| `s3`, `langsmith` | n/a | n/a | n/a — exempt | n/a |

LMNT and Replicate already use the empty-catalog + fallback pattern; they
are **proof points** that this architecture works in production today
(LMNT) and at scale (Replicate handles thousands of upstream slugs via
`FALLBACK_SPEC` alone). They become the conformance test fixtures.

## Performance targets

Hard gates enforced by `tests/perf/test_registry_perf.py`:

| Operation | P99 budget | Method |
|---|---|---|
| Family pattern resolution (1 family) | < 5 µs | Compiled regex, single match |
| Family resolution (32 families, miss) | < 50 µs | Linear scan, all-miss case |
| Family resolution (adversarial input, 32 families) | **< 100 µs** | Pattern-safety guard rejects unsafe patterns at construction (RT-9); `re2` if available |
| Registry construction | < 10 ms | No I/O, no network |
| `validate_model()` (no fetch) | < 50 µs | Dict + family scan + cache read |
| Pipeline preflight (3 steps, mixed providers, NATIVE cached) | < 50 ms | ThreadPoolExecutor; single-flight discovery cache |
| Pipeline preflight (3 steps, cold) | < 800 ms | First fetch per provider via single-flight |
| Discovery fetch | < 500 ms typical | RetryPolicy w/ backoff on transient errors |
| Memory per registry | < 100 KB | Specs + compiled patterns + discovery cache |

Concurrency model:

- `_provider_families` is immutable post-construction (frozen `tuple`);
  resolution is lock-free.
- `_user_families` is RLock-guarded (RT-3); resolution snapshots the list
  under the lock and scans the snapshot.
- Discovery cache uses `threading.RLock` + single-flight `Event`. Concurrent
  callers from N threads share one fetch. RetryPolicy + Retry-After header
  respect for 429 responses (RT-11a).
- `register()` / `register_pricing()` / `register_family()` retain
  RLock-guarded writes.

Family count cap (`MAX_PROVIDER_FAMILIES = 32`) prevents future connector
authors from undermining the perf bound (RT-9).

## Observability

Structured log surface:

| Logger | Level | Event | Cardinality |
|---|---|---|---|
| `genblaze.registry.lookup` | DEBUG | per-resolution; fields: `model_id`, `source`, `family_name` | per call |
| `genblaze.registry.fallback` | INFO | dedup'd one-time per `(provider, slug)` | bounded |
| `genblaze.discovery.fetch` | INFO | per-discovery; fields: `provider`, `slug_count`, `latency_ms`, `source_url` | per fetch |
| `genblaze.discovery.failed` | WARN | per-failure; fields: `provider`, `reason`, `latency_ms`, `retry_count` | per failure |
| `genblaze.discovery.rate_limited` | WARN | when 429 hits the retry budget | bounded |
| `genblaze.preflight.provisional` | WARN | dedup'd one-time per `(provider, slug)` for `OK_PROVISIONAL` | bounded |
| `genblaze.preflight.unknown_slug` | WARN | dedup'd one-time per `(provider, slug)` for `UNKNOWN_PERMISSIVE` | bounded |
| `genblaze.preflight.not_found` | ERROR | per failed preflight (raises) | per pipeline |
| `genblaze.family.probe` | DEBUG | per probe; fields: `provider`, `family_name`, `slug`, `result` | per probe |

OpenTelemetry: `tracer.span("registry.validate")`, `tracer.span("discovery.fetch")`,
`tracer.span("family.probe")` integrate with existing `NoOpTracer` /
`LoggingTracer` abstractions.

Metrics:
- `registry.lookup_total{source}` (counter)
- `registry.lookup_latency_seconds{source}` (histogram)
- `discovery.fetch_total{provider, status}` (counter)
- `discovery.fetch_latency_seconds{provider}` (histogram)
- `family.probe_total{provider, family, result}` (counter)
- `preflight.outcome_total{outcome}` (counter)

## Test strategy

### Unit (`libs/core/tests/unit/`)

- `test_model_family.py` — pattern matching, spec_template substitution,
  example_slug independence, regex compilation at module load,
  pattern-safety rejection.
- `test_pattern_safety.py` (RT-9) — assert known catastrophic-backtracking
  patterns raise; assert `re2` integration when available.
- `test_model_registry_v2.py` — precedence ordering across all six layers,
  user-family-prepend semantics (RT-3), `__contains__` coherence with
  `validate()` (RT-11d), single-flight discovery stress (50 concurrent
  threads → 1 fetch issued), TTL expiry (RT-6).
- `test_validation_outcomes.py` — every `(DiscoverySupport, family-match,
  probe, cache state, user registration, refresh)` combination produces
  the documented `ValidationResult`. Goldens for the full matrix
  (~3×3×3×2 = 54 cases).
- `test_pipeline_preflight.py` — `preflight=True` raises on `NOT_FOUND`,
  emits WARN on `OK_PROVISIONAL` and `UNKNOWN_PERMISSIVE`, silent on
  `OK_AUTHORITATIVE`. **`preflight=False` skips the path entirely
  (RT-11c).**
- `test_pricing_phaseout.py` — lint-style guard: no `pricing=` argument
  in any `ModelFamily` `spec_template` literal, no `_PRICE_*` /
  `_*_PRICING` / `_RATES` constants in `libs/connectors/*/genblaze_*/`
  source files post-migration.

### Conformance (`libs/core/tests/conformance/test_provider_contract.py`)

Extend the existing parametric harness:

- Every entry-point provider declares `discovery_support: DiscoverySupport`.
- Every family in every provider has ≥ 1 `example_slug`, and every
  example matches its family's pattern (catches reviewer typos).
- For `discovery_support == NATIVE` providers with `GENBLAZE_PROBE_*_API_KEY`
  set: `discover_models()` returns `DiscoveryResult(status=OK)` with a
  non-empty slug set (RT-11b — auth forwarding).
- `validate_model("__definitely_not_a_real_slug_12345__")` returns:
  `UNKNOWN_PERMISSIVE` for `NONE`, `NOT_FOUND` for `NATIVE` (with creds),
  `UNKNOWN_PERMISSIVE` for `PARTIAL` without probe.
- No `models/*.py` file contains `pricing=` after migration (lint guard).
- For `discovery_support == NATIVE` providers, every `example_slug`
  matches a slug in the live discovery catalog **OR** is documented in
  `unstable_examples` (RT-5 — closes the example-slug rot vector for
  NATIVE providers).

### Performance (`libs/core/tests/perf/test_registry_perf.py` — NEW)

`pytest-benchmark` harness. Hard gates from the Performance section.
Includes adversarial-input benchmark: a corpus of slugs designed to
exercise pattern matching against every family. P99 must hold under
fuzz inputs (RT-9).

### Probe-CI (`tools/probe_models.py` — modified)

Two modes:
- `--mode pattern-coverage` (default): for each `NATIVE` provider,
  call `discover_models()`, assert every returned slug matches some
  family pattern OR is registered explicitly. Slugs without coverage
  reported as `family_drift` entries — **hard CI gate** (RT-5: `family_drift`
  for NATIVE providers breaks the build, not a soft signal).
- `--mode example-liveness` (NATIVE only): assert every `example_slug`
  is currently in upstream's discovery catalog. Hard CI gate. Closes
  the example-slug rot vector identified in RT-5.

The legacy "probe each `known()` slug" mode is removed — its premise
(curated `known()` list) no longer holds.

### Golden tests (`libs/core/tests/golden/test_resolution.py` — NEW)

Snapshot pattern resolution for a corpus of representative slugs across
all providers. Reviewer must include snapshot diff in any family pattern
edit. Catches accidental pattern widening or narrowing.

### Fuzz (`libs/core/tests/unit/test_family_fuzz.py` — NEW)

Hypothesis-based:
- generate random strings (length 0 to 100k, full Unicode);
- assert pattern resolution is deterministic, total (no exceptions);
- assert P99 < 100 µs on adversarial inputs (RT-9).

## File-by-file changes

### Core (PR #1)

```
libs/core/genblaze_core/providers/
├── family.py                    NEW
├── discovery.py                 NEW
├── validation.py                NEW
├── pattern_safety.py            NEW
├── model_registry.py            EDIT  (add families, validate*, transitional defaults= shim)
├── base.py                      EDIT  (discovery_support, discover_models, validate_model,
│                                       deprecated probe_model adapter)
├── __init__.py                  EDIT  (export new types; keep ProbeResult for 0.3.0)
├── spec.py                      EDIT  (no pricing default change; user-registered going forward)
└── pricing.py                   no change

libs/core/genblaze_core/pipeline/pipeline.py
                                 EDIT  (preflight=True default, _validate_models() via
                                        ThreadPoolExecutor — RT-2; preflight(bool) toggle)

libs/core/tests/unit/test_model_family.py            NEW
libs/core/tests/unit/test_pattern_safety.py          NEW
libs/core/tests/unit/test_model_registry_v2.py       NEW
libs/core/tests/unit/test_validation_outcomes.py     NEW
libs/core/tests/unit/test_pipeline_preflight.py      NEW
libs/core/tests/unit/test_pricing_phaseout.py        NEW
libs/core/tests/unit/test_family_fuzz.py             NEW
libs/core/tests/perf/test_registry_perf.py           NEW
libs/core/tests/golden/test_resolution.py            NEW
libs/core/tests/conformance/test_provider_contract.py EDIT
```

### Per-connector (one PR per connector)

```
libs/connectors/<name>/genblaze_<name>/
├── models/<modality>.py         REWRITE  defaults={...} → provider_families=(...,)
├── _errors.py                   EDIT     enrich 404 messages
├── provider.py                  EDIT     declare discovery_support, override
│                                          discover_models() if NATIVE/PARTIAL
└── tests/
    └── test_family_resolution.py NEW    snapshot family matches + pricing-recipe assertions
```

### Tooling

```
tools/probe_models.py            EDIT  --mode pattern-coverage (default) + --mode example-liveness
tools/gen_model_matrix.py        EDIT  read families; drop Price column
docs/migration/registry-decoupling.md   NEW   user-facing migration guide
docs/reference/model-matrix.md   regenerated by gen_model_matrix.py
docs/reference/pricing-recipes.md       NEW   per-connector register_pricing() recipes;
                                              "not maintained" disclaimer
README.md                        EDIT  family-based examples; remove slug-pinning examples
ARCHITECTURE.md                  EDIT  registry section
AGENTS.md                        EDIT  scaffold-provider workflow updates
.claude/skills/scaffold-provider EDIT  templates use provider_families=, not defaults=
```

## Rollout sequencing

Soft launch — single 0.3.0 minor cuts the entire change. PRs are sequenced
to keep CI green at each merge. **Order matters**: core first, then proof-
points (LMNT, Replicate), then the active fire (NVIDIA), then the rest in
dependency-light order.

| # | PR | Contents | Gate |
|---|---|---|---|
| 1 | `core: ModelFamily + ValidationResult + DiscoverySupport` | All new types, registry v2, pipeline preflight, conformance/perf/golden test scaffolding. Transitional `defaults=` parameter retained on `ModelRegistry` (code-path shim, RT-4). | `make test` passes; perf gates green |
| 2 | `lmnt: declare DiscoverySupport.NONE` | Already empty-catalog. Conformance test as proof-point. | LMNT package tests pass |
| 3 | `replicate: declare DiscoverySupport.NATIVE` | Already empty-catalog; wire `/v1/models` discovery. | Replicate tests + probe pattern-coverage |
| 4 | `nvidia: families for audio/video/image; chat discovery; family.probe for PARTIAL` | The active fire. NVIDIA chat = NATIVE; generative = PARTIAL **with `family.probe`** (empty-payload POST trick). riva-tts slug deleted; magpie-tts via family pattern. | NVIDIA tests; CHANGELOG entry |
| 5 | `gmicloud: families + family.probe + suspected_dead → unstable_examples` | RT-10: preserve dead-slug signal through family layer until probe-confirmed. | GMI tests + probe pattern-coverage |
| 6-7 | `runway`, `decart` | Mid-complexity catalogs with pricing. | Package tests + probe pattern-coverage |
| 8-10 | `elevenlabs`, `openai`, `google` | NATIVE discovery; pricing tables removed; recipes published. | Package tests + probe + cookbook published |
| 11-12 | `luma`, `stability-audio` | Smallest catalogs. | Package tests pass |
| 13 | `core: remove transitional defaults= shim; finalize` | Delete the legacy dict path from registry. Conformance test asserts no `defaults=` in any connector. | All conformance tests pass; lint guard green |
| 14 | `docs: migration guide + pricing recipes + README rewrites` | User-facing surface. | `verify-docs` skill green |
| 15 | `samples: migrate downstream reference apps` | Update `backblaze-labs/genblaze-gmicloud-pipeline` and `backblaze-labs/nvidia-nemotron-genblaze-b2`: delete `_defaults` mutations, replace per-model registry workarounds with family-pattern equivalents (most workarounds dissolve), add `Pipeline(preflight=False)` opt-out only where UX requires. **Lives in those repos, not this one** — this row tracks coordination. | Sample apps' CI green against `genblaze-core 0.3.0` |

Total: **14 PRs in this repo + 1 coordination PR** for downstream samples.
Estimated duration: 3-4 weeks at one engineer's pace; 2 weeks if
parallelized across connectors after PR #1.

## Risks & mitigations (post-red-team)

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Pattern misses a new family member with surprising slug | Medium | Low | Permissive fallback + error enrichment + probe-CI `family_drift` hard gate for NATIVE |
| Discovery API changes upstream | Low-Medium | Low | `DiscoveryResult.FAILED` falls through to permissive; one WARN; no crash; RetryPolicy w/ backoff |
| Misconfigured pattern matches wrong slug | Low | Medium | Golden tests; reviewer must include snapshot diff |
| Catastrophic-backtracking regex DoS | Low | High | `pattern_safety.assert_safe` at `ModelFamily` construction; `re2` if available; fuzz tests with P99 bound (RT-9) |
| **Pricing removal degrades enterprise cost-estimation** | **High (expected)** | **Medium** (was Low) | Per-connector pricing-recipes cookbook published in same PR as the removal; CHANGELOG documents `Pipeline.estimated_cost()` returning `None`; user opt-in via `register_pricing()`. Honest documentation, not silent degradation (RT-7) |
| Pipeline preflight adds startup latency | Medium | Low | Single-flight discovery + ThreadPoolExecutor; `preflight=False` opt-out; perf gate enforces < 50 ms warm / < 800 ms cold |
| `validate_model` confused with `probe_model` | Low | Low | RT-8: `probe_model` deprecated and delegates to `validate_model`; removed in 0.4.0 |
| `discover_models()` requires auth per-provider, blocking CI | Medium | Low | `GENBLAZE_PROBE_*_API_KEY` env-var pattern; conformance test asserts auth forwarding (RT-11b) |
| Soft-launch users still pinning to deleted slugs | High | Low | 404 with enriched error message; migration guide; CHANGELOG entry |
| **PARTIAL providers' family-only OK is false precision** (was Issue 1) | High (resolved) | Resolved | RT-1: `OK_PROVISIONAL` outcome distinct from `OK_AUTHORITATIVE`; family.probe escape hatch; preflight emits WARN |
| **`asyncio.gather` in sync codebase** (was Issue 2) | High (resolved) | Resolved | RT-2: ThreadPoolExecutor on sync path; `asyncio.gather` only in `arun()` |
| **Migration shim masks bugs** (was Issue 4) | Medium (resolved) | Resolved | RT-4: code-path shim, not regex shim |
| Long-running daemon staleness | Medium | Medium | RT-6: default TTL 3600s; `_DEFAULT_TTL_SECONDS = 3600.0` constant |
| Stale `example_slugs` ship as CI-enforced docs | Medium | Low | RT-5: `--mode example-liveness` hard gate for NATIVE; PARTIAL examples documented as advisory |
| `__contains__` / `known()` / `has()` incoherence | Low | Low | RT-11d: `__contains__` mirrors `validate().outcome != UNKNOWN_PERMISSIVE` |
| `unstable_examples` deprecation forgotten | Medium | Low | Conformance test flags families with `unstable_examples` older than 6 months without a probe; nudges maintainers to either probe-confirm or delete |
| Downstream sample apps break on 0.3.0 | High (expected) | Medium | PR #15 coordinates `backblaze-labs/genblaze-gmicloud-pipeline` + `nvidia-nemotron-genblaze-b2` migration; net code-shrink (per-app workarounds dissolve into family patterns); `Pipeline(preflight=False)` is the conservative opt-out |

## Open design decisions (locked)

1. **Family ordering matters.** First-match-wins. Connectors must order
   families from most-specific to least-specific. Conformance test detects
   shadowed families via subset analysis.
2. **`example_slugs` semantics differ by `DiscoverySupport`.** For NATIVE
   providers, examples are CI-gated (must be live in upstream catalog).
   For PARTIAL/NONE, they're advisory and documented as such.
3. **Discovery TTL default = 3600s (1 hour).** RT-6: long-running daemons
   refresh on a sane cadence by default. `max_age_seconds=None` opt-in
   for CLI scripts that want one-fetch-per-process.
4. **Discovery is per-provider-instance.** Two `NvidiaProvider` instances
   with different API keys may see different catalogs; cache lives on the
   instance.
5. **Preflight default = ON.** Soft launch — loud failures.
6. **`_provider_families` is `tuple` (immutable post-construction);
   `_user_families` is RLock-guarded list with prepend semantics.** RT-3
   resolves the precedence question explicitly.
7. **No `families` inheritance across providers.** Each connector ships
   its own.
8. **Family count cap = 32 per provider.** RT-9: prevents perf-bound
   erosion as connectors grow.
9. **`probe_model()` deprecated in 0.3.0, removed in 0.4.0.** RT-8: single
   "is this slug usable" entrypoint going forward.
10. **`MAX_PROVIDER_FAMILIES = 32`** is enforced at registry construction
    via `ValueError`. Encourages connector authors to keep families tight.

## Acceptance criteria

This plan is complete when:

- [ ] Zero `_AUDIO_MODELS`, `_IMAGE_MODELS`, `_VIDEO_MODELS`, `_RATES`,
      `_PRICING`, `_PRICE_*`, `_*_BUCKETS`, `extras["suspected_dead"]`
      constants in any `libs/connectors/*/genblaze_*/` module.
- [ ] Every entry-point provider declares
      `discovery_support: DiscoverySupport`.
- [ ] Every family has ≥ 1 `example_slug`; each example matches its
      family pattern.
- [ ] `validate_model()` returns deterministic, documented results across
      the 54-case `(DiscoverySupport, family-match, probe, cache state,
      user registration, refresh)` matrix.
- [ ] `Pipeline.run()` raises `ProviderError(MODEL_ERROR)` at preflight
      for `NOT_FOUND`. **`Pipeline.preflight(False)` skips the path.**
- [ ] `compute_cost()` returns `None` for any family-resolved model with
      no user-registered pricing. Documented in CHANGELOG.
- [ ] `tools/probe_models.py --mode pattern-coverage` passes for every
      `NATIVE` provider against staging credentials. **Hard gate.**
- [ ] `tools/probe_models.py --mode example-liveness` passes for every
      `NATIVE` provider. **Hard gate.**
- [ ] Performance gates pass:
      - pattern resolution P99 < 5 µs (1 family) / < 50 µs (32 families) /
        < 100 µs (adversarial)
      - registry construction < 10 ms
      - preflight (3 steps, warm) < 50 ms; cold < 800 ms
- [ ] `pattern_safety.assert_safe` rejects all catastrophic-backtracking
      patterns in the test corpus.
- [ ] `docs/migration/registry-decoupling.md` published.
- [ ] `docs/reference/pricing-recipes.md` published with one section per
      migrated connector.
- [ ] `docs/reference/model-matrix.md` regenerated from families.
- [ ] CHANGELOG entry under 0.3.0 with migration recipes for the 5 most
      common callsite patterns + pricing migration.
- [ ] One example in `examples/` migrated end-to-end demonstrating
      `validate_model()` preflight.

## Resolves the reporter's complaint

Mapping back to F-2026-05-04-01 (NVIDIA `riva-tts` 404):

| Reporter ask | Plan resolution |
|---|---|
| Replace dead `nvidia/riva-tts` slug | Slug deleted entirely. NVIDIA audio family pattern covers Magpie/Riva/Fugatto-shaped slugs. **`family.probe` (empty-payload POST trick) provides liveness check on PARTIAL** — RT-1. |
| Validate the rest of `_AUDIO_MODELS` | n/a — list deleted. Family pattern + probe covers the surface; probe-CI catches drift. |
| Surface deprecation warning before pipeline runs | `Pipeline.run()` issues `family.probe` for every PARTIAL-provider step at preflight. `NOT_FOUND` raises before any generation begins. `OK_PROVISIONAL` (no probe configured) emits WARN. |
| CHANGELOG entry for slug change | One CHANGELOG entry under 0.3.0 documenting the catalog-decoupling, including riva-tts disposition. Going forward: no slug-rename CHANGELOG entries (no slugs pinned). |

## Out of scope (explicitly)

- Provider-side rate limiting and retry policy unification — owned by
  `retry-policy-unification.md` (we *use* `RetryPolicy` here, do not
  redefine it).
- Cost-ledger replay — owned by `pipeline-replay-and-cost-ledger-tranche.md`.
- Manifest signing — owned by `manifest-signing-and-redaction-tranche.md`.
- Re-introducing pricing infrastructure of any kind. Phase-out is one-way;
  `register_pricing()` is the user contract.

---

## Red-team resolutions appendix

V1 was reviewed by an independent reviewer; this V2 closes all 11 issues:

| RT# | Issue | V2 resolution |
|---|---|---|
| 1 | PARTIAL providers still produce mid-pipeline 404s — family-match returned `OK` with no liveness signal | New `OK_PROVISIONAL` outcome distinct from `OK_AUTHORITATIVE`; `FamilyProbe` callable on PARTIAL families; preflight raises on probe `DEAD` |
| 2 | `asyncio.gather` in sync codebase crashes FastAPI/Jupyter | `ThreadPoolExecutor` on sync `Pipeline.run()`; `asyncio.gather` only in `AsyncPipeline.arun()` |
| 3 | `register_family` precedence ambiguity, TOCTOU on tuple rebuild | Two layers: `_user_families` (RLock list, prepend) and `_provider_families` (frozen tuple). Lookup is user-first |
| 4 | PR #1 shim could mask all preflight bugs during 12-PR window | Shim is **code-path** (legacy `defaults=` dict path inside registry), not a regex pattern. Cannot mask `NOT_FOUND` for unmigrated connectors |
| 5 | `example_slugs` are CI-enforced (must match pattern) yet "non-binding" — rot vector preserved | NATIVE: `--mode example-liveness` hard CI gate against live catalog. PARTIAL/NONE: examples are advisory, documented as such |
| 6 | Discovery TTL `None` default → long-running daemons silently stale | Default TTL = 3600s (1 hour). `max_age_seconds=None` is opt-in for one-fetch-per-process |
| 7 | Pricing phase-out misclassified as "Low" impact for enterprise users | Reclassified to **Medium**. Same-PR pricing-recipes cookbook with copy-paste `register_pricing()` recipes; CHANGELOG explicit on `Pipeline.estimated_cost()` returning `None` |
| 8 | `validate_model` vs `probe_model` — three overlapping entry points | Collapsed: `probe_model` deprecated, delegates to `validate_model(refresh=True)` returning coerced `ProbeResult`. Removed in 0.4.0 |
| 9 | Performance gates unanchored to realistic conditions; regex DoS vector | `MAX_PROVIDER_FAMILIES=32` cap; `pattern_safety.assert_safe` at `ModelFamily` construction; `re2` if available; fuzz P99 < 100 µs on adversarial inputs |
| 10 | `extras["suspected_dead"]` removed without replacement signal for PARTIAL | New `unstable_examples: tuple[str, ...]` on `ModelFamily`; preserved through migration; removed only after `family.probe` is implemented and CI-passing |
| 11a | Discovery rate limit handling absent | RetryPolicy with exponential backoff + jitter, capped at 3 retries; Retry-After respected; rate-limited responses logged at WARN |
| 11b | Discovery auth forwarding contract undocumented | Conformance test asserts NATIVE providers' fetcher returns `OK` against staging credentials |
| 11c | `preflight=False` path untested | Explicit unit test in `test_pipeline_preflight.py` |
| 11d | `__contains__` / `known()` / `has()` incoherence | `__contains__` mirrors `validate().outcome != UNKNOWN_PERMISSIVE` |
