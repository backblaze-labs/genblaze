<!-- branch: issue/261-dataclass-provider-init-skip -->
# Issue 261: @dataclass on a provider subclass silently skips BaseProvider.__init__

## Problem

GitHub issue: https://github.com/backblaze-labs/genblaze/issues/261

Decorating a `SyncProvider` / `BaseProvider` subclass with `@dataclass` produces
a half-initialized object. `@dataclass` generates an `__init__` that replaces
the inherited one, so `BaseProvider.__init__`
(`libs/core/genblaze_core/providers/base.py:406-497`) never runs and none of the
instance attributes it sets exist. Construction succeeds silently; the failure
surfaces much later from inside `providers/base.py`, naming private attributes
the user never wrote.

The reported double-fault is real and reproduces on `main` @ `46bbcc6`:

1. `invoke()` (`:1766`) calls `_attempt_once` (`:1810`), which calls
   `_cleanup_poll_cache()` (`:1536`) → reads `self._poll_cache_max_age`
   (`:576`, set at `:440`) → **first `AttributeError`**.
2. `invoke()`'s `except Exception` handler (`:1817`) then evaluates
   `error_code in self.retry_policy.retryable_codes` (`:1828`) → the
   `retry_policy` property (`:500`) reads `self._retry_policy_override`
   (`:515`, set at `:452`) → **second `AttributeError`, raised from inside the
   handler**.

Because the second exception escapes the handler, the message the user sees
names `_retry_policy_override` — not even the first thing that broke. `ainvoke()`
has the identical shape (`:2012` / `:2030`). Nothing in either traceback mentions
`@dataclass`, `__init__`, or `super().__init__()`.

`__init_subclass__` already exists at `:382` but only resets `_models_cache`; it
does not check `__init__` at all.

### Why the issue's preferred fix cannot work as written

The issue proposes detecting the case in `BaseProvider.__init_subclass__` via
`dataclasses.is_dataclass(cls)`. **This does not work.** `__init_subclass__`
runs during type creation — i.e. when the `class` statement's body finishes —
whereas `@dataclass` is a decorator applied to the *already-created* class
object afterwards. Verified:

```python
class B:
    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        print(dataclasses.is_dataclass(cls), "__init__" in cls.__dict__)

@dataclasses.dataclass
class C(B):
    x: int = 1
# during __init_subclass__: False False
# after the decorator:      True  True
```

Both signals the guard would key on are absent at `__init_subclass__` time, so
the check would never fire. Class-definition-time detection of the decorator
case is not achievable; the earliest reliable point is **construction**.

## Fix

Fail at construction with an error that names the cause, and stop `invoke()`'s
error path from masking the original exception.

Production — `libs/core/genblaze_core/providers/base.py`:

1. Add a class-level sentinel `_base_init_done: bool = False` on `BaseProvider`
   (class attribute, so reads never raise `AttributeError`), and set
   `self._base_init_done = True` as the **last** statement of
   `BaseProvider.__init__` — last, so a constructor that raises partway is also
   caught.
2. Add `class _ProviderMeta(ABCMeta)` whose `__call__` constructs the instance
   via `super().__call__(...)`, then raises `TypeError` if
   `getattr(obj, "_base_init_done", False)` is false. `BaseProvider` declares
   `metaclass=_ProviderMeta`. `Runnable` is `ABC, Generic[In, Out]`
   (`runnable/base.py:15`), so its metaclass is already `ABCMeta` and
   subclassing it is conflict-free.
3. Tailor the message by inspecting the class *at instantiation time*, where
   `dataclasses.is_dataclass(cls)` is finally accurate:
   - dataclass case → providers cannot be decorated with `@dataclass`, because
     the generated `__init__` replaces `BaseProvider.__init__`; write a plain
     `__init__` that calls `super().__init__()`.
   - any other case → `<Class>.__init__` never called `super().__init__()`.

   Both messages name the offending class and the concrete fix.
4. Defense-in-depth in `invoke()` (`:1828`) and `ainvoke()` (`:2012`): resolve
   the retry policy inside the exception handler defensively, and if resolving
   it raises, re-raise the **original** `exc` instead of the handler's own
   failure. The guard makes this unreachable via `@dataclass`, but it removes a
   whole class of "the error you see is not the error that happened" for any
   other broken-instance route (e.g. `object.__new__`, which bypasses the
   metaclass).

Scope note: the issue's "weaker alternative" (promote the lazy attributes to
class-level defaults) is deliberately **not** taken. It would let a
half-initialized provider run with silently-wrong defaults — shared mutable
`_poll_cache` dicts across instances, no `_poll_cache_lock` — which is worse
than a clear failure.

Tests — new `libs/core/tests/unit/test_provider_init_guard.py`:

- `@dataclass` provider raises `TypeError` at construction; message mentions
  `@dataclass` and `super().__init__()`.
- Hand-written `__init__` without `super().__init__()` raises `TypeError`
  naming `super().__init__()`.
- A correct provider constructs and completes `invoke()` unchanged.
- Multi-level subclass (`SyncProvider` → mid → leaf) still constructs.
- The failure is a `TypeError` at construction — assert the old
  `AttributeError: '_retry_policy_override'` symptom no longer occurs.
- Handler hardening: a provider built via `object.__new__` (bypassing the
  metaclass) surfaces the *original* `AttributeError`, not the
  `retry_policy` one — sync and async.

Docs / release:

- `docs/guides/new-provider.md` — add "never decorate a provider with
  `@dataclass`" alongside the existing `super().__init__()` guidance at `:131-142`
  and `:531`, and a checklist line at `:692`.
- `CHANGELOG.md` `[Unreleased]` → `### genblaze-core` **Fixed** bullet (#261).
- No version bump (versions move per release wave, per RELEASING.md).

## Risk

Low, and measured rather than assumed.

- **Does the guard break any existing provider?** No. An AST scan across all of
  `libs/` (excluding `.venv`/`node_modules`) for classes with a `Provider` base
  that define `__init__` without `super().__init__()` returns **zero hits** —
  production connectors and test doubles alike. There are also **no existing
  `@dataclass` providers** (the four `@dataclass` uses in connectors are on
  internal spec/config types, e.g. `openai/dalle.py:95 _ImageModelSpec`, not
  provider subclasses).
- **Metaclass conflict?** None possible in-repo: `grep metaclass=` across `libs/`
  returns no other metaclass, and `_ProviderMeta` derives from the `ABCMeta`
  that `BaseProvider` already has via `Runnable(ABC, Generic[...])`. A
  third-party subclass combining `BaseProvider` with a non-`ABCMeta` metaclass
  would need `_ProviderMeta` in its MRO — an acceptable and documented cost of
  enforcing the invariant.
- **Runtime cost:** one `getattr` per provider instantiation. Nothing on the
  per-step hot path.
- **Test doubles built via `Mock(spec=...)` or `object.__new__`** bypass
  `type.__call__` entirely and are unaffected by the guard — which is why the
  handler hardening in step 4 is kept as a separate, independent safety net.
- **Behavior change for third parties:** a downstream provider that *is* a
  dataclass goes from "constructs, then fails confusingly at invoke" to "fails
  loudly at construction". That is the intended fix, and it converts a silent
  half-broken object into an actionable error, but it is a visible change and is
  called out in the CHANGELOG.
- **Sentinel spoofing (accepted residual risk):** the guard's "did
  `BaseProvider.__init__` run" signal is `_base_init_token is _BASE_INIT_TOKEN`,
  an identity check against a module-private sentinel — not an unbreakable
  boundary. A provider author who deliberately imports `_BASE_INIT_TOKEN` and
  assigns it in a broken `__init__` still bypasses the guard. Accepted: this
  requires a deliberate act against a leading-underscore, non-exported symbol
  by code already running in the same trust domain (provider authors are not
  an adversarial boundary) — not something a `@dataclass` decorator or an
  accidental attribute collision can trigger. Confirmed by three independent
  reviews (2 Codex, 1 Claude) during `/pre-pr-check`.
- **Two accepted diagnostic-quality limitations, both contrived nested-`@dataclass`
  topologies with zero occurrence in this repo, found by Codex's adversarial
  pass and reproduced directly against the live code:**
  1. A `@dataclass` provider whose `__post_init__` itself reads missing
     `BaseProvider` state raises a raw `AttributeError` (naming the actual
     missing attribute) instead of the guard's `TypeError`, because the guard's
     check runs only *after* `__init__` returns — if `__init__` itself raises,
     the guard's check never executes. Construction still fails immediately
     (the core anti-regression goal holds); only the exception type and
     wording differ from the common case. Not fixed: a reliable fix requires
     distinguishing "the exception came from the dataclass-generated
     `__init__`" from "the exception came from a legitimate custom `__init__`"
     (e.g. the valid `@dataclass(init=False)` + hand-written `__init__` pattern
     that calls `super().__init__()`), which isn't reliably inspectable without
     risking a false positive on that legal pattern.
  2. (Found and **fixed** — kept here for context since round 1 initially
     deferred it before testing it.) `class Child(BrokenDataclassProvider):
     pass` — no new `__init__` — got a message asserting `Child.__init__`
     never called `super().__init__()`, which is false (`Child` has no
     `__init__` of its own). Verified worse than just imprecise: even adding
     `Child.__init__` that calls `super().__init__()` (the message's own
     suggested fix) still fails, because that call resolves to the
     dataclass-generated `__init__` on the broken ancestor, which never
     forwards to `BaseProvider.__init__`. Fixed by rewording the fallback
     message to describe the whole `__init__` chain rather than asserting a
     specific class is the culprit; pinned by
     `test_message_does_not_blame_a_specific_init_that_is_not_the_real_cause`.

## Files Modified

| File | Change |
|---|---|
| `libs/core/genblaze_core/providers/base.py` | modified |
| `libs/core/tests/unit/test_provider_init_guard.py` | created |
| `docs/guides/new-provider.md` | modified |
| `CHANGELOG.md` | modified |
| `docs/exec-plans/completed/issue-261-dataclass-provider-init-skip.md` | created |

No connector source touched. No version bump (versions move per release wave,
per RELEASING.md).

## Verification

Actual results, this branch:

1. Exact repro from the issue body run as a script: `MyProv()` raises `TypeError`
   naming `@dataclass`, and the old `_retry_policy_override` `AttributeError` is
   gone.
2. `cd libs/core && pytest tests/ -q` — **1805 passed, 25 skipped** (the 2
   failures seen mid-development, `test_url_policy_reexported_from_s3_for_back_compat`
   and `test_spool_threshold_matches_multipart_threshold`, are pre-existing —
   reproduced identically against unmodified `main` in an isolated `libs/core`
   env; both are `ModuleNotFoundError: genblaze_s3` from running `libs/core`
   without the sibling package installed, not caused by this change. They do
   not occur under `make test`, which installs every package into one env.)
3. `cd libs/core && pytest tests/unit/test_provider_init_guard.py -v` —
   **12 passed** (new file).
4. Full-repo `make test` (all 18 packages, single shared `.venv` with every
   connector installed editable) — **exit 0**. `genblaze-core` 2155 passed, 3
   skipped; every connector, `cli`, `libs/meta`, and `tools/tests` green.
5. `ruff check` / `ruff format --check` on touched files — clean.
6. `mypy libs/core/genblaze_core/ --ignore-missing-imports` — same 2
   pre-existing errors in `_utils.py` as on unmodified `main` (confirmed via
   `git stash`); none in the new metaclass or touched code.
7. `/pre-pr-check`, full 6-check sequence (3 Claude + 3 Codex), run twice:
   - **Round 1** found 2 real MEDIUM findings, both fixed: (a) the `invoke()`/
     `ainvoke()` exception-handler hardening was over-broad and could mask a
     genuine bug in a correctly-constructed provider's `retry_policy`
     override, leaking an unsanitized exception; (b) the construction guard's
     signal was a plain `_base_init_done: bool`, spoofable by a same-named
     subclass/dataclass-field attribute — replaced with an identity-checked
     module-private sentinel (`_base_init_token` / `_BASE_INIT_TOKEN`).
   - **Round 2** (re-run after both fixes): 5 of 6 checks clean; Codex's
     adversarial pass (check 6) found 2 further findings in contrived nested
     `@dataclass` topologies with zero real-world occurrence in this repo —
     see the two accepted limitations in Risk, below. One of the two
     (misleading remediation message for a subclass of an already-broken
     `@dataclass` ancestor) was fixed anyway, since the fix was cheap and
     removed a message that was not just imprecise but demonstrably wrong —
     the message's own suggested fix, verified against the live code, did not
     resolve the failure. A 12th regression test pins this.
   - All findings, classifications, and fixes are recorded in this repo's
     pre-pr-check run log (`~/.claude/pre-pr-check-log.md`); the working
     `.pre-pr-check/` folder is deleted on a clean run, per that skill's
     process.
