<!-- branch: issue/248-gmicloud-validate-model-unmatched-slugs -->
# Issue 248: validate_model() never probes slugs that match no family — a working
# model and a fabricated one both grade UNKNOWN_PERMISSIVE

## Problem

GitHub issue: https://github.com/backblaze-labs/genblaze/issues/248

The report has two claims. **The first is already fixed on main**;
`libs/connectors/gmicloud/genblaze_gmicloud/image.py:55` declares
`_entitlement_gated_slugs = frozenset({"seededit-3-0-i2i-250628"})` and
`_base.py:357` re-grades a probe-LIVE verdict on a gated slug down to
`OK_PROVISIONAL` (#193, shipped in genblaze-gmicloud 0.3.5 — the reporter
is on 0.3.3). No further work; answer on the issue.

The second claim is real and reproduces on main. `BaseProvider.validate_model()`
(`libs/core/genblaze_core/providers/base.py:670`) only consults a liveness probe
when a **family pattern matched**:

    # base.py:747
    match = self._models.match_family(model_id)
    if match is not None and match.family.probe is not None:
        probe_result = self._cached_probe(...)

GMICloud's image registry (`models/image.py:132`) ships three narrow families —
`bria-(genfill|eraser)`, `^(seededit-|reve-edit|reve-remix)`, `^gpt-image-2-edit$`
— plus a permissive `ModelSpec(model_id="*")` fallback. Everything else
(Seedream, Gemini-Flash, FLUX-Kontext, Reve create, Bria fibo) matches no
family. Video and audio registries have the same shape.

So for `gemini-3-pro-image-preview` — which serves all of the reporter's
production traffic:

1. `match_family()` → `None` (`model_registry.py:388`).
2. `discovery_support` is `PARTIAL` (`_base.py:146`), so the NATIVE-discovery
   branches are skipped.
3. `model_registry.py:458` → `unknown_permissive()`.
4. Back in `base.py:747`, `match is None`, so **the probe never fires**.

`totally-made-up-model-xyz` takes the identical path to the identical outcome.
The outcome carries no signal for exactly the slugs an account actually uses,
and the `preflight.unknown` WARN (`pipeline.py:963`) is noise the reporter
learned to ignore — which is when a real warning gets missed.

The probe that could answer is already written and already generic:
`empty_payload_request_probe` (`_probe.py:39`) reads 404 as DEAD, 400 as LIVE,
2xx as LIVE (with best-effort cancel), everything else as UNKNOWN. It takes only
`(slug, *, http)` — nothing family-specific. It is structurally unreachable for
fallback slugs.

Third note in the report is accurate but by design: `build_image_registry().get(
"reve-edit-v1")` returns the fallback `ModelSpec` for any string because `"*"`
matches everything. `.get()` is a param-shape lookup, not an existence check,
and nothing says so.

`list_available_models()` (the reporter's suggested fix) is not buildable:
`DiscoverySupport.PARTIAL` records that GMI's request-queue API exposes no
catalog endpoint. The console reads an internal surface. Answer that on the
issue rather than promising the API.

## Fix

Make the existing probe reachable on the fallback path, opt-in per registry.

Core — `providers/model_registry.py`:

1. New constructor kwarg `fallback_probe: FamilyProbe | None = None`, stored as
   `self._fallback_probe`. Semantics: "the probe to consult for slugs that
   matched no family" — the liveness counterpart to the existing `fallback`
   spec, which is the param-shape counterpart.
2. Carry it through `fork()` (`:223`) alongside `_fallback` /
   `_unstable_slugs`, so per-request multi-tenant forks don't silently lose
   the signal.
3. `validate()` (`:370`) is unchanged — it stays non-network by contract. The
   registry only *holds* the probe; the provider invokes it.

Core — `providers/base.py`:

4. In `validate_model()`, after the existing family-probe block, add the
   no-family-match branch for `PARTIAL` / `NONE`: when `match is None` and
   `self._models._fallback_probe is not None`, route it through the same
   `_cached_probe()` → `_invoke_family_probe()` path (same LRU + single-flight
   + TTL, so at most one probe per slug per TTL window):
   - `LIVE` → `ValidationResult.ok_authoritative(ValidationSource.PROBE)` with
     no `family_name` (there is none) and a `detail` naming it a fallback probe.
   - `DEAD` → `ValidationResult.not_found(ValidationSource.PROBE, detail=
     "upstream fallback probe returned DEAD")` — preflight then raises
     (`pipeline.py:927`) before any credit is spent.
   - `UNKNOWN` → fall through to today's `unknown_permissive`. No regression on
     auth failures, 429s, or 5xx.

   Two guards protect a `ValidationSource.USER` result from ever being
   probed under the wrong string (found in pre-pr-check review, not in the
   original design): (a) resolve `model_id` via `resolve_canonical()` first —
   if it differs, the input was an alias to a genuine user-registered spec
   (`match is None` rules out family resolution, so any non-identity
   canonicalization here can only come from the user/alias layer), and the
   branch returns `ok_authoritative(USER)` directly without probing; (b) the
   probe path itself only runs when the pre-probe `result.outcome` is
   `UNKNOWN_PERMISSIVE` — a `USER` exact match under `refresh=True` is
   already `OK_AUTHORITATIVE` at that point, so it falls straight through to
   `return result` instead of being handed to the probe.
5. Docstring update on `validate_model()` and on `ValidationOutcome` in
   `validation.py`: a positive outcome means the slug exists in upstream's
   catalog, **not** that this account is entitled to call it. That distinction
   is the reporter's actual ask and is what `_entitlement_gated_slugs` already
   encodes for the one slug GMI knows about.

Connector — `genblaze_gmicloud/models/{image,video,audio}.py`:

6. Pass `fallback_probe=empty_payload_request_probe` to each
   `build_*_registry()`'s `ModelRegistry(...)`. The transport hook
   (`_base.py:166`, forwards the provider's authed `httpx.Client`) already
   exists, so **`_base.py` needs no changes** — no line-level overlap with the
   open #283.
7. The #193 entitlement re-grade in `_base.py:357` keeps applying on top: it
   matches on `outcome is OK_AUTHORITATIVE and source is PROBE`, which the new
   fallback-LIVE result satisfies, so gated slugs still cannot over-claim.

Docs:

8. `ModelRegistry.get()` docstring — state explicitly it is not an existence
   check (`"*"` fallback matches any string) and point to `validate_model()`.
9. `libs/connectors/gmicloud/README.md` — short note that unmatched slugs are
   now probe-validated, and what each outcome does and does not prove.
10. CHANGELOG `[Unreleased]` → `### genblaze-core` **Added** (fallback_probe)
    and `### genblaze-gmicloud` **Fixed** (#248). No version bump (versions
    move per release wave, per RELEASING.md).

Tests:

- New `libs/core/tests/unit/test_fallback_probe.py`: `fallback_probe`
  stored on `ModelRegistry` and carried by `fork()`; on a stub `PARTIAL`
  provider, unmatched slug + LIVE → `OK_AUTHORITATIVE`/`PROBE`, no
  `family_name`; + DEAD → `NOT_FOUND`; + UNKNOWN → `UNKNOWN_PERMISSIVE`;
  probe fires **once** across repeated `validate_model()` calls (cache);
  `refresh=True` re-probes; family-matched slugs still take the family
  probe, not the fallback one (pins existing behavior); registries without
  a `fallback_probe` are unaffected. Also covers a pre-pr-check regression:
  the fallback probe must never override a USER-registered spec, directly
  or via an alias to one — verified with dedicated tests asserting the
  probe callable is never invoked for those cases.
- `libs/core/tests/unit/test_pipeline_preflight.py`: unmatched-slug DEAD now
  raises `ProviderError(MODEL_ERROR)` at preflight; UNKNOWN still WARNs and
  proceeds; the flip side (LIVE stays silent) is also covered.
- `libs/connectors/gmicloud/tests/test_catalog_decoupling.py`: with a
  mocked `httpx.Client` on `GMICloudAudioProvider`, an unmatched slug that
  404s grades `NOT_FOUND`/`PROBE` (previously `UNKNOWN_PERMISSIVE` — the
  exact regression from the issue) and one the fallback probe can't
  classify (500) stays `UNKNOWN_PERMISSIVE`; the orphan `unstable_slugs`
  case on `GMICloudVideoProvider` (`vidu-q1`) is re-pinned for both the
  LIVE (`OK_AUTHORITATIVE`, `known_unstable` detail preserved) and DEAD
  (`NOT_FOUND`) outcomes now that its registry carries a `fallback_probe`.
- Registries without a `fallback_probe` (every other connector) behave
  byte-for-byte as today — pinned by the existing suites.

## Risk

Medium-low. Core surface is additive (a new optional kwarg; no signature
changes, no default behavior change for connectors that don't opt in), but the
GMI behavior change is real and deliberate:

- **New round-trip.** Preflight on a fallback slug now POSTs
  `/requests` where it previously did nothing. `_probe.py`'s politeness note
  applies: that POST creates an audit-log record on the user's GMI account
  even when rejected. Bounded by the same single-flight LRU the family
  probes already use — for a definitive LIVE/DEAD verdict, once per slug
  per TTL window (default 1h). An inconclusive (`UNKNOWN`) verdict is
  deliberately *not* cached (pre-existing `_cached_probe` behavior, shared
  with every family probe, unchanged by this diff) so a slug the upstream
  can't classify (auth error, rate limit, 5xx) re-probes on every
  `validate_model()` call rather than being pinned to "inconclusive" for
  the rest of the window — callers who don't want any of this can construct
  a registry without `fallback_probe`.
- **Stricter preflight.** A genuinely dead slug now raises before the wire
  instead of failing mid-pipeline. That is the point of the issue, but it can
  turn a late failure into an early one for anyone relying on the permissive
  pass-through — including a slug GMI temporarily 404s during an incident.
  Mitigated: only 404 yields DEAD; 401/403/429/5xx stay UNKNOWN and permissive.
- **Entitlement is still not proven.** LIVE means "exists in the catalog". The
  reporter's `seededit` scar was an entitlement failure, and only the curated
  `_entitlement_gated_slugs` list catches that class. Documented, not claimed
  away.
- No overlap with any open PR: #283 touches `gmicloud/_base.py` (untouched
  here), #265 touches `models/audio.py` in the alias/param region, not the
  `ModelRegistry(...)` call. Merges independently off main.

## Files Modified

| File | Change |
|---|---|
| `libs/core/genblaze_core/providers/model_registry.py` | modified |
| `libs/core/genblaze_core/providers/base.py` | modified |
| `libs/core/genblaze_core/providers/validation.py` | modified (docs) |
| `libs/core/tests/unit/test_fallback_probe.py` | created |
| `libs/core/tests/unit/test_pipeline_preflight.py` | modified |
| `libs/connectors/gmicloud/genblaze_gmicloud/models/image.py` | modified |
| `libs/connectors/gmicloud/genblaze_gmicloud/models/video.py` | modified |
| `libs/connectors/gmicloud/genblaze_gmicloud/models/audio.py` | modified |
| `libs/connectors/gmicloud/tests/test_catalog_decoupling.py` | modified |
| `libs/connectors/gmicloud/README.md` | modified |
| `CHANGELOG.md` | modified |
| `docs/exec-plans/completed/issue-248-gmicloud-validate-model-unmatched-slugs.md` | created |

`genblaze_gmicloud/_base.py` deliberately untouched — the probe transport hook
and the #193 entitlement re-grade already sit at the right seam, which also
keeps this diff free of #283.

## Verification

1. Regression gate: the two GMI tests from the issue fail against pre-fix code
   (both slugs grade `UNKNOWN_PERMISSIVE`) and pass after.
2. `/test-package gmicloud` and `/test-package core` → green.
3. `make lint` clean on touched files.
4. Full-repo `make test` gate on the final commit → exit 0.
5. `/pre-pr-check` scoped to this diff against the acceptance criterion "a slug
   that 404s on submit grades NOT_FOUND at preflight, a slug that submits
   successfully grades OK_AUTHORITATIVE, and no probe fires more than once per
   slug per TTL window"; resolve blocking findings before opening the PR.
6. Reply on #248: claim 1 fixed in 0.3.5 (upgrade), claim 2 fixed here,
   `list_available_models()` not feasible while GMI ships no catalog endpoint.
