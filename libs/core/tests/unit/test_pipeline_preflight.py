"""Tests for Pipeline model-preflight phase.

Coverage:
- ``preflight=True`` (default): raises on NOT_FOUND, WARNs on
  OK_PROVISIONAL / UNKNOWN_PERMISSIVE, silent on OK_AUTHORITATIVE.
- ``preflight=False`` skips the path entirely (RT-11c).
- WARN dedup via ``_warned_preflight`` (one per provider/slug).
- Suggestions surface in NOT_FOUND error messages.
- ``ThreadPoolExecutor`` parallelism — preflight runs validate_model
  on every step, sync codebase, no asyncio.run_until_complete (RT-2).
- Issue #56: ``arun()`` must not block the event loop during preflight.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import pickle
import queue
import re
import threading
import time

import pytest
from genblaze_core import Pipeline
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import Modality
from genblaze_core.providers.base import BaseProvider, ProviderCapabilities
from genblaze_core.providers.discovery import (
    DiscoveryResult,
    _DiscoveryCache,
)
from genblaze_core.providers.family import DiscoverySupport, ModelFamily
from genblaze_core.providers.model_registry import ModelRegistry
from genblaze_core.providers.spec import ModelSpec


def _make_native_provider(slugs: set[str]) -> BaseProvider:
    """A NATIVE provider whose discovery cache returns ``slugs``."""

    class _NativeProvider(BaseProvider):
        name = "native-test"
        discovery_support = DiscoverySupport.NATIVE

        @classmethod
        def create_registry(cls) -> ModelRegistry:
            cache = _DiscoveryCache(lambda: DiscoveryResult.ok(slugs))
            return ModelRegistry(discovery_cache=cache)

        def discover_models(
            self,
            *,
            max_age_seconds: float | None = ...,  # type: ignore[assignment]
        ) -> DiscoveryResult:
            cache = self._models._discovery_cache
            assert cache is not None
            if max_age_seconds is ...:  # type: ignore[comparison-overlap]
                return cache.get()
            return cache.get(max_age_seconds=max_age_seconds)

        def get_capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(supported_modalities=[Modality.IMAGE], models=[])

        def submit(self, step, config=None):  # type: ignore[no-untyped-def]
            return "pid"

        def poll(self, prediction_id, config=None):  # type: ignore[no-untyped-def]
            return True

        def fetch_output(self, prediction_id, step):  # type: ignore[no-untyped-def]
            return step

    # Ensure each test gets a fresh class-level cache.
    return _NativeProvider()


def _make_family_provider(family: ModelFamily) -> BaseProvider:
    """A NONE-discovery provider with one family."""

    class _FamilyProvider(BaseProvider):
        name = "family-test"
        discovery_support = DiscoverySupport.NONE

        @classmethod
        def create_registry(cls) -> ModelRegistry:
            return ModelRegistry(provider_families=[family])

        def get_capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(supported_modalities=[Modality.IMAGE], models=[])

        def submit(self, step, config=None):  # type: ignore[no-untyped-def]
            return "pid"

        def poll(self, prediction_id, config=None):  # type: ignore[no-untyped-def]
            return True

        def fetch_output(self, prediction_id, step):  # type: ignore[no-untyped-def]
            return step

    return _FamilyProvider()


class TestNotFoundRaises:
    def test_native_missing_slug_raises(self) -> None:
        p = _make_native_provider({"live-slug-1", "live-slug-2"})
        pipe = Pipeline("t").step(p, model="dead-slug", modality=Modality.IMAGE, prompt="hi")
        with pytest.raises(ProviderError) as exc:
            pipe._validate_steps()
        assert "not found" in str(exc.value).lower()
        assert "dead-slug" in str(exc.value)

    def test_error_message_includes_suggestions(self) -> None:
        p = _make_native_provider({"nvidia/magpie-tts-multilingual"})
        pipe = Pipeline("t").step(p, model="nvidia/riva-tts", modality=Modality.IMAGE, prompt="hi")
        with pytest.raises(ProviderError) as exc:
            pipe._validate_steps()
        assert "nvidia/magpie-tts-multilingual" in str(exc.value)

    def test_error_message_includes_refresh_recovery_hint(self) -> None:
        """The NOT_FOUND error must point users at the refresh=True
        escape hatch — without it, a cached-DEAD verdict that no longer
        reflects upstream reality strands the user with no recovery
        path beyond reading the source."""
        p = _make_native_provider({"live"})
        pipe = Pipeline("t").step(p, model="dead-slug", modality=Modality.IMAGE, prompt="hi")
        with pytest.raises(ProviderError) as exc:
            pipe._validate_steps()
        msg = str(exc.value)
        assert "refresh=True" in msg, f"NOT_FOUND error must mention the refresh recovery: {msg}"

    def test_error_message_propagates_validation_detail(self) -> None:
        """When validate_model returns a NOT_FOUND with a ``detail``
        (e.g., from a stale-cache fallback path), that detail must
        surface in the user's exception so they can correlate during
        incidents."""
        # Construct a provider whose registry returns NOT_FOUND with
        # a custom detail. We pass a stub directly via ``models=`` and
        # mock the validate path.
        from genblaze_core.providers.validation import (
            ValidationResult,
            ValidationSource,
        )

        p = _make_native_provider({"live"})

        def _validate_with_detail(slug: str, *, refresh: bool = False) -> ValidationResult:
            return ValidationResult.not_found(
                ValidationSource.DISCOVERY,
                detail="discovery fetch failed; result is from stale cache",
            )

        p.validate_model = _validate_with_detail  # type: ignore[method-assign]
        pipe = Pipeline("t").step(p, model="any", modality=Modality.IMAGE, prompt="hi")
        with pytest.raises(ProviderError) as exc:
            pipe._validate_steps()
        assert "stale cache" in str(exc.value)


class TestOkAuthoritativeSilent:
    def test_native_present_slug_silent(self, caplog) -> None:
        p = _make_native_provider({"live-slug"})
        pipe = Pipeline("t").step(p, model="live-slug", modality=Modality.IMAGE, prompt="hi")
        with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
            pipe._validate_steps()
        # No preflight WARNs emitted for OK_AUTHORITATIVE.
        preflight_warns = [r for r in caplog.records if "preflight." in r.getMessage()]
        assert preflight_warns == []


class TestOkProvisionalWarns:
    def test_family_match_no_probe_warns_once(self, caplog) -> None:
        fam = ModelFamily(
            name="fake-fam",
            pattern=re.compile(r"^fake/"),
            spec_template=ModelSpec(model_id="*", modality=Modality.IMAGE),
            description="fake",
        )
        p = _make_family_provider(fam)
        pipe = (
            Pipeline("t")
            .step(p, model="fake/a", modality=Modality.IMAGE, prompt="hi")
            .step(p, model="fake/a", modality=Modality.IMAGE, prompt="hi")
        )
        with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
            pipe._validate_steps()

        # Two steps, same slug — one WARN expected (dedup).
        provisional_warns = [
            r for r in caplog.records if "preflight.provisional" in r.getMessage()
        ]
        assert len(provisional_warns) == 1


class TestUnknownPermissiveWarns:
    def test_no_match_no_discovery_warns_once(self, caplog) -> None:
        # NONE provider, no families, no user spec — UNKNOWN_PERMISSIVE.
        class _NoCatalogProvider(BaseProvider):
            name = "no-catalog"
            discovery_support = DiscoverySupport.NONE

            def get_capabilities(self) -> ProviderCapabilities:
                return ProviderCapabilities(supported_modalities=[Modality.IMAGE], models=[])

            def submit(self, step, config=None):  # type: ignore[no-untyped-def]
                return "pid"

            def poll(self, prediction_id, config=None):  # type: ignore[no-untyped-def]
                return True

            def fetch_output(self, prediction_id, step):  # type: ignore[no-untyped-def]
                return step

        p = _NoCatalogProvider()
        pipe = Pipeline("t").step(p, model="random-slug", modality=Modality.IMAGE, prompt="hi")
        with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
            pipe._validate_steps()

        unknown_warns = [r for r in caplog.records if "preflight.unknown" in r.getMessage()]
        assert len(unknown_warns) == 1


class TestPreflightOptOut:
    """RT-11c: preflight=False must skip the validation path entirely."""

    def test_preflight_false_skips_not_found(self) -> None:
        p = _make_native_provider({"live"})
        pipe = Pipeline("t", preflight=False).step(
            p, model="not-live", modality=Modality.IMAGE, prompt="hi"
        )
        # Should NOT raise — preflight is off.
        pipe._validate_steps()

    def test_preflight_false_skips_warns(self, caplog) -> None:
        p = _make_native_provider({"live"})
        pipe = Pipeline("t", preflight=False).step(
            p, model="not-live", modality=Modality.IMAGE, prompt="hi"
        )
        with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
            pipe._validate_steps()
        preflight_records = [r for r in caplog.records if "preflight." in r.getMessage()]
        assert preflight_records == []

    def test_preflight_method_toggles(self) -> None:
        p = _make_native_provider({"live"})
        pipe = (
            Pipeline("t")
            .preflight(False)
            .step(p, model="not-live", modality=Modality.IMAGE, prompt="hi")
        )
        # toggle method propagates: no raise.
        pipe._validate_steps()


class TestValidatorExceptionHandling:
    def test_validator_exception_falls_through(self, caplog) -> None:
        # A provider whose validate_model raises must not break preflight.
        class _BrokenProvider(BaseProvider):
            name = "broken"
            discovery_support = DiscoverySupport.NONE

            def validate_model(self, model_id: str, *, refresh: bool = False):  # type: ignore[no-untyped-def,override]
                raise RuntimeError("validate exploded")

            def get_capabilities(self) -> ProviderCapabilities:
                return ProviderCapabilities(supported_modalities=[Modality.IMAGE], models=[])

            def submit(self, step, config=None):  # type: ignore[no-untyped-def]
                return "pid"

            def poll(self, prediction_id, config=None):  # type: ignore[no-untyped-def]
                return True

            def fetch_output(self, prediction_id, step):  # type: ignore[no-untyped-def]
                return step

        p = _BrokenProvider()
        pipe = Pipeline("t").step(p, model="anything", modality=Modality.IMAGE, prompt="hi")
        # Must NOT raise — broken validator degrades to UNKNOWN_PERMISSIVE.
        with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
            pipe._validate_steps()

        # The broken validator surfaces as UNKNOWN_PERMISSIVE → preflight.unknown.
        unknown_warns = [r for r in caplog.records if "preflight.unknown" in r.getMessage()]
        assert len(unknown_warns) == 1


class TestArunDoesNotBlockEventLoop:
    """Issue #56: ``arun()`` preflight must not stall the event loop.

    Strategy: a provider whose ``validate_model`` does blocking I/O
    (simulated with ``time.sleep``). A concurrent heartbeat coroutine
    increments a counter every 10 ms. If preflight blocks the event
    loop the counter never ticks during the preflight window.
    After the fix (offloading via ``asyncio.to_thread``), the counter
    accumulates multiple ticks while preflight runs.
    """

    @pytest.mark.asyncio
    async def test_event_loop_not_blocked_during_preflight(self) -> None:
        PREFLIGHT_SLEEP = 0.15  # simulated blocking network call

        class _SlowProvider(BaseProvider):
            name = "slow-preflight"
            discovery_support = DiscoverySupport.NONE

            def validate_model(self, model_id: str, *, refresh: bool = False):  # type: ignore[no-untyped-def,override]
                # Simulate a blocking discovery fetch (the root cause of #56).
                time.sleep(PREFLIGHT_SLEEP)
                from genblaze_core.providers.validation import (
                    ValidationResult,
                    ValidationSource,
                )

                return ValidationResult.ok_authoritative(ValidationSource.DISCOVERY)

            def get_capabilities(self) -> ProviderCapabilities:
                return ProviderCapabilities(supported_modalities=[Modality.IMAGE], models=[])

            def submit(self, step, config=None):  # type: ignore[no-untyped-def]
                return "pid"

            def poll(self, prediction_id, config=None):  # type: ignore[no-untyped-def]
                return True

            def fetch_output(self, prediction_id, step):  # type: ignore[no-untyped-def]
                return step

        # Heartbeat that ticks every 10 ms while the event loop is free.
        tick_count = 0
        running = True

        async def heartbeat() -> None:
            nonlocal tick_count
            while running:
                await asyncio.sleep(0.01)
                tick_count += 1

        p = _SlowProvider()
        pipe = Pipeline("t", preflight=True).step(
            p, model="any", modality=Modality.IMAGE, prompt="hi"
        )

        hb_task = asyncio.create_task(heartbeat())
        try:
            # Call arun() — the actual public call site — to verify the event loop
            # is not blocked at the preflight call site, not just on the helper.
            await pipe.arun(raise_on_failure=False)
        finally:
            running = False
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass

        # With a 150 ms preflight sleep and 10 ms heartbeat interval we expect
        # ~14 ticks; assert >= 3 for generous CI-jitter margin. A blocked loop
        # yields 0-1 (the heartbeat's first sleep may fire before preflight
        # begins blocking).
        assert tick_count >= 3, (
            f"Event loop was blocked during preflight: heartbeat ticked only "
            f"{tick_count} time(s) during a {PREFLIGHT_SLEEP * 1000:.0f} ms "
            f"preflight window (expected >= 3)."
        )


class TestWarnOnceConcurrency:
    """Issue #56: ``_warn_once`` must dedup atomically across threads.

    Preflight now runs off the event loop via ``asyncio.to_thread`` and
    ``abatch_run`` clones share the ``_warned_preflight`` set (shallow
    ``copy.copy``), so multiple worker threads can call ``_warn_once`` on the
    same key concurrently. Exactly one caller must win the claim, else a
    duplicate WARN leaks. This locks in the contract so the guarding lock
    cannot be silently dropped in a future refactor.
    """

    def test_warn_once_claims_key_exactly_once_under_contention(self) -> None:
        pipe = Pipeline("warn-once")
        key = ("prov", "model-x")
        n_threads = 32
        # Barrier maximizes the check-then-add overlap window across threads.
        barrier = threading.Barrier(n_threads)
        results: list[bool] = []
        results_lock = threading.Lock()

        def claim() -> None:
            barrier.wait()
            won = pipe._warn_once(key)
            with results_lock:
                results.append(won)

        threads = [threading.Thread(target=claim) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1, (
            f"_warn_once must grant the claim to exactly one thread, "
            f"got {sum(results)} winners out of {n_threads}."
        )
        assert key in pipe._warned_preflight


class TestPipelineCopySemantics:
    """Issue #56: the preflight lock must not break copy/pickle.

    The threading.Lock added for the WARN-dedup is neither copyable nor
    picklable. Three protocols must stay correct: shallow copy.copy (used by
    batch_run/abatch_run) shares the lock+set so a batch dedups as a unit,
    while deepcopy and pickle each yield a fully independent pipeline with a
    freshly built lock.
    """

    def test_deepcopy_succeeds_and_isolates_lock(self) -> None:
        p = Pipeline("orig")
        p._warned_preflight.add(("seed", "model"))

        clone = copy.deepcopy(p)

        # deepcopy => independent lock and dedup set (no shared mutation).
        assert clone._warned_preflight_lock is not p._warned_preflight_lock
        assert clone._warned_preflight is not p._warned_preflight
        p._warned_preflight.add(("prov", "model"))
        assert ("prov", "model") not in clone._warned_preflight

    def test_pickle_roundtrip_succeeds_and_rebuilds_lock(self) -> None:
        p = Pipeline("orig")
        p._warned_preflight.add(("seed", "model"))

        clone = pickle.loads(pickle.dumps(p))  # noqa: S301 — round-tripping our own pipeline in a test

        # pickle => independent pipeline with a usable, freshly built lock.
        assert isinstance(clone, Pipeline)
        assert clone._warned_preflight == {("seed", "model")}
        assert clone._warned_preflight is not p._warned_preflight
        # The rebuilt lock must be a real, acquirable lock.
        assert clone._warn_once(("new", "model")) is True

    def test_shallow_copy_shares_lock_for_batch_clones(self) -> None:
        p = Pipeline("orig")

        clone = copy.copy(p)

        # abatch_run relies on clones sharing the lock+set so _warn_once dedups
        # across the whole batch — this must NOT change.
        assert clone._warned_preflight_lock is p._warned_preflight_lock
        assert clone._warned_preflight is p._warned_preflight


class TestPipelineEmitterSlotCopySemantics:
    """Issue #151: the per-instance stream emitter slot must never be shared.

    Unlike the WARN-dedup lock/set, ALL THREE copy protocols give the clone a
    brand-new ``EmitterSlot`` — including shallow ``copy.copy`` (used by
    batch_run/abatch_run). A shallow-shared emitter slot would let a
    batch_run() clone read the spawning pipeline's active stream emitter when
    both run in the same thread/task Context, reintroducing #151's leak in a
    narrower shape. See ``test_batch_run_clone_inside_outer_stream_does_not_leak``
    in test_streaming.py for the corresponding behavioral regression test.
    """

    def test_deepcopy_isolates_emitter_slot(self) -> None:
        p = Pipeline("orig")
        clone = copy.deepcopy(p)
        assert clone._emitter_slot is not p._emitter_slot

    def test_pickle_roundtrip_rebuilds_emitter_slot(self) -> None:
        p = Pipeline("orig")
        clone = pickle.loads(pickle.dumps(p))  # noqa: S301 — round-tripping our own pipeline in a test
        assert isinstance(clone, Pipeline)
        assert clone._emitter_slot is not p._emitter_slot
        # The rebuilt slot must be a real, usable EmitterSlot (get() works).
        assert clone._emitter_slot.get() is None

    def test_shallow_copy_does_not_share_emitter_slot(self) -> None:
        """Unlike the WARN-dedup lock, batch clones must NOT share the emitter slot."""
        p = Pipeline("orig")
        clone = copy.copy(p)
        assert clone._emitter_slot is not p._emitter_slot

    def test_shallow_copy_clone_does_not_observe_original_emitter(self) -> None:
        """A clone's ``_event_emitter`` must stay None even when the original
        has an emitter installed in the SAME thread/task Context — the exact
        scenario a nested batch_run() invoked from inside stream() hits.
        """
        from genblaze_core.pipeline.streaming import QueueEmitter

        p = Pipeline("orig")
        p.attach_emitter(QueueEmitter(q=queue.Queue()))
        try:
            clone = copy.copy(p)
            assert clone._event_emitter is None
        finally:
            p.attach_emitter(None)
