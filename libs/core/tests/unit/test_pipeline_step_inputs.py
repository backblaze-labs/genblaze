"""Tests for ``Pipeline.step(external_inputs=...)`` — caller-held Asset injection.

Covers the public surface that lets a caller seed ``Step.inputs`` directly
(e.g., user-uploaded media for a multimodal chat step on position 0) without
going through ``input_from=`` or ``chain=True``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from genblaze_core.exceptions import GenblazeError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import RunStatus
from genblaze_core.models.step import Step
from genblaze_core.pipeline import Pipeline
from genblaze_core.providers.base import BaseProvider, ProviderCapabilities
from genblaze_core.runnable.config import RunnableConfig


class RecordingProvider(BaseProvider):
    """Records what landed in ``step.inputs`` so tests can assert identity."""

    name = "recording"

    def __init__(self, *, accepts_chain: bool = True) -> None:
        super().__init__()
        self._accepts_chain = accepts_chain
        self.received_inputs: list[list[Asset]] = []

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(accepts_chain_input=self._accepts_chain)

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        self.received_inputs.append(list(step.inputs))
        return "pred-rec"

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        return True

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        step.assets.append(Asset(url="https://example.com/out.png", media_type="image/png"))
        return step


def _asset(url: str, *, sha: str | None = "a" * 64) -> Asset:
    return Asset(url=url, media_type="image/png", sha256=sha)


# --- Threading external_inputs into step.inputs ---


def test_external_inputs_single_asset_threads_through() -> None:
    p = RecordingProvider()
    asset = _asset("https://upload.test/cat.png")

    Pipeline("t").step(p, model="m", prompt="describe", external_inputs=[asset]).run()

    assert len(p.received_inputs) == 1
    assert p.received_inputs[0] == [asset]


def test_external_inputs_multiple_assets_preserve_order() -> None:
    p = RecordingProvider()
    a1 = _asset("https://upload.test/1.png")
    a2 = _asset("https://upload.test/2.png")
    a3 = _asset("https://upload.test/3.png")

    Pipeline("t").step(p, model="m", prompt="multi", external_inputs=[a1, a2, a3]).run()

    assert p.received_inputs[0] == [a1, a2, a3]


def test_external_inputs_empty_list_is_noop() -> None:
    """Empty list is treated like omitting the kwarg — no inputs to the step."""
    p = RecordingProvider()
    Pipeline("t").step(p, model="m", prompt="p", external_inputs=[]).run()
    assert p.received_inputs[0] == []


# --- Mutual exclusion + reserved-name guards ---


def test_external_inputs_and_input_from_are_mutually_exclusive() -> None:
    p = RecordingProvider()
    pipe = Pipeline("t").step(p, model="m", prompt="seed")  # step 0
    with pytest.raises(GenblazeError, match="mutually exclusive"):
        pipe.step(
            p,
            model="m",
            prompt="combine",
            input_from=[0],
            external_inputs=[_asset("https://x.test/a.png")],
        )


def test_inputs_kwarg_is_reserved_with_helpful_message() -> None:
    """Bare ``inputs=`` would silently land in **params; we reject it loudly."""
    p = RecordingProvider()
    with pytest.raises(GenblazeError, match="external_inputs"):
        Pipeline("t").step(
            p,
            model="m",
            prompt="p",
            inputs=[_asset("https://x.test/a.png")],  # type: ignore[arg-type]
        )


def test_input_singular_kwarg_is_reserved() -> None:
    p = RecordingProvider()
    with pytest.raises(GenblazeError, match="external_inputs"):
        Pipeline("t").step(
            p,
            model="m",
            prompt="p",
            input=_asset("https://x.test/a.png"),  # type: ignore[arg-type]
        )


# --- Capability gate ---


def test_external_inputs_requires_accepts_chain_input_capability() -> None:
    """Provider that doesn't read step.inputs must reject external_inputs at validation."""
    p = RecordingProvider(accepts_chain=False)
    pipe = Pipeline("t").step(
        p, model="m", prompt="p", external_inputs=[_asset("https://x.test/a.png")]
    )
    with pytest.raises(GenblazeError, match="does not accept input"):
        pipe.run()


# --- Defensive copy ---


def test_external_inputs_defensive_copy() -> None:
    """Mutating the caller's list after step() must not bleed into the deferred step."""
    p = RecordingProvider()
    a1 = _asset("https://upload.test/1.png")
    a2 = _asset("https://upload.test/2.png")
    caller_list = [a1]

    pipe = Pipeline("t").step(p, model="m", prompt="p", external_inputs=caller_list)
    caller_list.append(a2)  # post-construction mutation

    pipe.run()
    assert p.received_inputs[0] == [a1]  # a2 never reaches the step


# --- Logging: sha256 warning ---


def test_external_inputs_warns_when_sha256_missing(caplog) -> None:
    p = RecordingProvider()
    asset_no_sha = Asset(url="https://upload.test/x.png", media_type="image/png")  # no sha256

    with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
        Pipeline("t").step(p, model="m", prompt="p", external_inputs=[asset_no_sha])

    msgs = [r.getMessage() for r in caplog.records]
    assert any("no sha256" in m and "canonical hash" in m for m in msgs), msgs


def test_external_inputs_no_warning_when_sha256_present(caplog) -> None:
    p = RecordingProvider()
    with caplog.at_level(logging.WARNING, logger="genblaze.pipeline"):
        Pipeline("t").step(
            p, model="m", prompt="p", external_inputs=[_asset("https://upload.test/y.png")]
        )
    assert not any("no sha256" in r.getMessage() for r in caplog.records)


# --- to_template guard ---


def test_to_template_raises_when_external_inputs_present() -> None:
    """Templates describe pipeline shape; runtime Asset payloads can't serialize."""
    p = RecordingProvider()
    pipe = Pipeline("t").step(
        p, model="m", prompt="p", external_inputs=[_asset("https://upload.test/a.png")]
    )
    with pytest.raises(GenblazeError, match="external_inputs.*cannot be serialized"):
        pipe.to_template()


def test_to_template_succeeds_without_external_inputs() -> None:
    """Sanity: existing to_template path still works when external_inputs is unused."""
    p = RecordingProvider()
    pipe = Pipeline("t").step(p, model="m", prompt="p")
    template = pipe.to_template()
    assert len(template.steps) == 1


# --- Async path ---


@pytest.mark.asyncio
async def test_external_inputs_arun_threads_through() -> None:
    p = RecordingProvider()
    asset = _asset("https://upload.test/async.png")

    result = await Pipeline("t").step(p, model="m", prompt="p", external_inputs=[asset]).arun()

    assert result.run.status == RunStatus.COMPLETED
    assert p.received_inputs[0] == [asset]


# --- Cache-key stability ---


def test_external_inputs_cache_key_stable_with_sha256() -> None:
    """Same Asset (with sha256) in step.inputs → same step_cache_key across runs.

    The point: cache stability depends on Asset.sha256 (cache.py:39 uses
    ``a.sha256 or a.url``). With sha256 set, the cache key matches regardless
    of which run produced it.
    """
    from genblaze_core.pipeline.cache import step_cache_key

    asset = _asset("https://upload.test/cache.png", sha="b" * 64)
    s1 = Step(provider="recording", model="m", prompt="p", inputs=[asset])
    s2 = Step(provider="recording", model="m", prompt="p", inputs=[asset])
    assert step_cache_key(s1) == step_cache_key(s2)


def test_external_inputs_cache_key_drifts_without_sha256() -> None:
    """Without sha256, the URL is the cache anchor — rotating URLs drift the key."""
    from genblaze_core.pipeline.cache import step_cache_key

    a1 = Asset(url="https://upload.test/presigned?token=AAA", media_type="image/png")
    a2 = Asset(url="https://upload.test/presigned?token=BBB", media_type="image/png")
    s1 = Step(provider="recording", model="m", prompt="p", inputs=[a1])
    s2 = Step(provider="recording", model="m", prompt="p", inputs=[a2])
    assert step_cache_key(s1) != step_cache_key(s2)


# --- Streaming path ---


def test_external_inputs_streaming_emits_started_event() -> None:
    """Stream consumers see the step lifecycle for an external_inputs step."""
    from genblaze_core.observability.events import StepStartedEvent

    p = RecordingProvider()
    pipe = Pipeline("t").step(
        p, model="m", prompt="p", external_inputs=[_asset("https://upload.test/s.png")]
    )

    events = list(pipe.stream(heartbeats=False))
    assert any(isinstance(e, StepStartedEvent) for e in events)
    assert p.received_inputs[0][0].url == "https://upload.test/s.png"


# --- Batch run reuses external_inputs across iterations ---


def test_batch_run_reuses_external_inputs_across_items() -> None:
    """A single ``external_inputs=[...]`` is shared by every batch_run iteration.

    Per-item asset variation is out of scope (would require items=[{...}]
    threading external_inputs, intentionally deferred).
    """
    p = RecordingProvider()
    asset = _asset("https://upload.test/shared.png")
    pipe = Pipeline("t").step(p, model="m", prompt="p", external_inputs=[asset])

    pipe.batch_run(prompts=["a", "b", "c"], raise_on_failure=False)
    assert len(p.received_inputs) == 3
    for received in p.received_inputs:
        assert received == [asset]
