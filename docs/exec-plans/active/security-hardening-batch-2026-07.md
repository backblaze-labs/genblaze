<!-- last_verified: 2026-07-14 -->
# Security hardening batch — ffmpeg escaping/redaction, hash-depth guard, SSRF completeness, ReDoS guard

Tracks six independently-scoped security issues fixed together because they touch
overlapping modules (`libs/core/genblaze_core/providers/`, `_utils.py`,
`canonical/`). One commit per issue; this doc is the shared plan.

## Issues and fixes

### #17 — ffmpeg drawtext `%` expansion / control-sequence escaping
`_escape_drawtext` (`providers/transform.py`) escapes `\`, `:`, `'` but not `%`,
which ffmpeg's drawtext filter treats as a text-expansion trigger
(`%{expr:...}`, `%{pts}`, etc. — enabled by default). Fix: escape `%` → `%%`
(ffmpeg's own literal-percent escape), which also fully neutralizes `%{...}`
since the brace becomes plain text once expansion is disabled for that
occurrence. Literal `\n`/`\t` (two-char sequences typed by a caller) are
already neutralized by the existing backslash-doubling — added a regression
test to make that explicit instead of just asserting it in prose.

### #75 — presigned URL leaked in ffmpeg DEBUG log
`run_ffmpeg` (`providers/_ffmpeg_utils.py`) logs `" ".join(cmd)` verbatim at
DEBUG. A chained step's `https://` input can be a presigned object-storage URL
whose query string is a bearer credential until expiry. Fix: redact the query
string of any `http(s)` argument before logging (execution still uses the
untouched `cmd`).

### #81 — `RecursionError` on deeply nested `step.params`
`canonical/_normalize.py:normalize()` recurses with no depth cap; `step.params`
is free-form user input hashed into every manifest and cache key. Fix: thread
an optional `_depth` counter through `normalize()` and raise `ManifestError`
past a 100-level cap — well under Python's default recursion limit, so the
failure is a controlled, typed error instead of a stack overflow. No change to
sort order, float rounding, or any other normalization behavior (kwarg is
additive with a default, so all existing call sites are unaffected).

### #16 — SSRF denylist misses IPv4-mapped / NAT64 / unspecified forms
`resolve_ssrf` in `_utils.py` already normalizes `::ffff:0:0/96`-mapped IPv6
(fixed under #9/PR #112) but not RFC 6052 NAT64 (`64:ff9b::/96`, a different
prefix `ipaddress.IPv6Address.ipv4_mapped` doesn't recognize) or the
unspecified address `::/128`. Fix: extract the embedded IPv4 address from the
NAT64 well-known prefix the same way mapped addresses are unwrapped, add
`::/128` to `BLOCKED_NETWORKS`, and add a property-based backstop
(`is_private`/`is_loopback`/`is_link_local`/`is_reserved`/`is_unspecified`) so
ranges neither list enumerates are still caught. A Hypothesis property test
asserts the mapped/NAT64 representation of any generated IPv4 address has the
same blocked/allowed status as the plain address — the actual bypass class
this issue describes.

### #80 — ReDoS heuristic bypasses + re2 path never active + missing perf gate
Three separate root causes, all closed:

1. **Wrong import.** `pattern_safety.py` did `import google.re2 as _re2`, but
   the current `google-re2` PyPI distribution ships a top-level `re2` module
   (`import re2`), not a `google.re2` namespace package. The authoritative
   check has therefore never run for anyone, regardless of whether
   `google-re2` was installed — this is the real reason "re2 is never active"
   described in the issue, not just a missing dependency. Fixed the import.
2. **Heuristic bypasses.** Added `{n,}` (open-ended brace quantifier,
   semantically identical to `+`) to the unbounded-quantifier detection, added
   an adjacent-same-atom-quantifier check (`a+a+a+`), and replaced the
   nesting-blind `[^)]*` group scan with a small paren-depth-aware scanner so
   `([a-z]+(?:x)?)+`-shaped patterns (unbounded quantifier nested inside a
   sub-group, not directly inside the outer group's flat text) are caught.
3. **Missing perf gate.** Added `libs/core/tests/perf/test_registry_perf.py`
   backing the docstring's "P99 < 100µs on adversarial input" claim, using
   min-of-N timing (stable under CI scheduler noise; percentile-of-samples on
   a shared runner is not) as the practical implementation of that claim.

Additionally: added `google-re2` as a new `re2` optional extra (guarded
import, same pattern as the existing `otel`/`http` extras) and included it in
`dev`, so CI's `libs/core[dev]` install actually activates the authoritative
path going forward. Verified all 32 shipped connector family patterns compile
under re2 and none trip the strengthened heuristic (no false positives).

**Deferred:** a CI job that installs a deliberately-bad pattern and asserts
the *build* fails end-to-end (as opposed to the unit test suite exercising
`assert_safe` directly, which is in scope and included) — tracked in
tech-debt-tracker as a follow-up; the existing test suite already gates on
every commit via `assert_safe` running at `ModelFamily` construction time in
every connector's test collection, so the practical protection is in place
even without a dedicated CI job.

### #18 — `.gitignore` only covers `credentials.json`
Broadened to `credentials*` plus `*.credentials`, `*_rsa`, `*.p12`, `*.pfx`,
`*.keystore` per the issue's suggested fix. Preventative; no tracked secret
files existed before or after.

## Cross-batch note

A parallel batch is adding tests around `canonical_hash` normalization (#50).
The #81 fix here is additive (new optional kwarg, default preserves existing
behavior) and touches only `_normalize.py` — kept deliberately minimal to
avoid conflicts.
