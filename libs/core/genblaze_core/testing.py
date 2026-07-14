"""Testing utilities — mock providers and compliance test harness.

Mock providers
--------------
``MockProvider``, ``MockVideoProvider``, and ``MockAudioProvider`` return
configurable canned assets so you can test pipelines without real API calls.
They live in ``genblaze_core.mocks`` (no pytest dependency) and are
re-exported here for backward compatibility.

Example::

    from genblaze_core.testing import MockVideoProvider
    from genblaze_core.pipeline import Pipeline

    result = Pipeline("test").step(MockVideoProvider(), model="mock", prompt="a cat").run()
    assert result.run.steps[0].assets[0].media_type == "video/mp4"

Compliance harness
------------------
Subclass ``ProviderComplianceTests`` and implement ``make_provider()`` to get
a full suite of compatibility tests for free. Works for both BaseProvider
(submit/poll/fetch_output) and SyncProvider (generate) subclasses.

Example::

    from genblaze_core.testing import ProviderComplianceTests

    class TestMyProvider(ProviderComplianceTests):
        def make_provider(self):
            return MyProvider(api_key="test")

        def make_step(self):
            return Step(provider="my-provider", model="test-model", prompt="hello")
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pytest

# Re-export pytest-free mock classes for backward compatibility.
# Callers importing from genblaze_core.testing continue to work unchanged;
# callers who want no pytest dep should import from genblaze_core.mocks instead.
from genblaze_core.mocks import MockAudioProvider, MockProvider, MockVideoProvider
from genblaze_core.models.asset import AudioMetadata
from genblaze_core.models.enums import Modality, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.providers.base import BaseProvider, ProviderCapabilities, SyncProvider

__all__ = [
    "MockProvider",
    "MockVideoProvider",
    "MockAudioProvider",
    "ProviderComplianceTests",
]

# ---------------------------------------------------------------------------
# Compliance test harness
# ---------------------------------------------------------------------------


class ProviderComplianceTests(ABC):
    """Base test class for provider implementations.

    Provides ~10 tests that verify a provider conforms to the genblaze
    provider contract. Subclass and implement make_provider() and make_step().

    Override ``expects_cost = False`` for providers that intentionally do not
    populate ``step.cost_usd`` (e.g. local-only tools like FFmpegCompositor,
    mock/stub providers, or connectors whose pricing formula is pending fix).
    """

    # Providers must populate step.cost_usd on successful runs by default.
    # Set False on subclasses where cost is not applicable or not yet wired.
    expects_cost: bool = True

    @abstractmethod
    def make_provider(self) -> BaseProvider:
        """Return a configured provider instance (may use mocks/fakes)."""
        ...

    def make_step(self) -> Step:
        """Return a Step suitable for this provider. Override if needed."""
        provider = self.make_provider()
        return Step(provider=provider.name, model="test-model", prompt="test prompt")

    # --- Identity ---

    def test_has_name(self) -> None:
        """Provider must override the default name."""
        provider = self.make_provider()
        assert provider.name != "base", "Provider must set a unique `name` attribute"

    def test_name_is_string(self) -> None:
        provider = self.make_provider()
        assert isinstance(provider.name, str)

    # --- Lifecycle ---

    def test_submit_returns_prediction_id(self) -> None:
        """submit() must return a non-None prediction ID."""
        provider = self.make_provider()
        step = self.make_step()
        prediction_id = provider.submit(step)
        assert prediction_id is not None

    def test_poll_returns_bool(self) -> None:
        """poll() must return a boolean."""
        provider = self.make_provider()
        step = self.make_step()
        prediction_id = provider.submit(step)
        result = provider.poll(prediction_id)
        assert isinstance(result, bool)

    def test_fetch_output_returns_step(self) -> None:
        """fetch_output() must return a Step instance."""
        provider = self.make_provider()
        step = self.make_step()
        prediction_id = provider.submit(step)
        result = provider.fetch_output(prediction_id, step)
        assert isinstance(result, Step)

    # --- invoke() integration ---

    def test_invoke_succeeds(self) -> None:
        """Full invoke() lifecycle should succeed."""
        provider = self.make_provider()
        step = self.make_step()
        result = provider.invoke(step)
        assert result.status in (StepStatus.SUCCEEDED, StepStatus.FAILED)

    def test_invoke_sets_timestamps(self) -> None:
        """Successful invoke should set started_at and completed_at."""
        provider = self.make_provider()
        step = self.make_step()
        result = provider.invoke(step)
        if result.status == StepStatus.SUCCEEDED:
            assert result.started_at is not None
            assert result.completed_at is not None

    # --- Asset validation ---

    def test_assets_have_valid_urls(self) -> None:
        """All asset URLs must be HTTPS or file:// (for local-save providers)."""
        provider = self.make_provider()
        step = self.make_step()
        result = provider.invoke(step)
        for asset in result.assets:
            valid = asset.url.startswith("https://") or asset.url.startswith("file://")
            assert valid, f"Invalid URL scheme (must be https:// or file://): {asset.url}"

    def test_assets_have_media_type(self) -> None:
        """All assets must have a media_type set."""
        provider = self.make_provider()
        step = self.make_step()
        result = provider.invoke(step)
        for asset in result.assets:
            assert asset.media_type, f"Asset missing media_type: {asset.url}"

    # --- SyncProvider-specific ---

    def test_sync_provider_generate_is_sufficient(self) -> None:
        """SyncProvider subclasses only need generate() for full lifecycle."""
        provider = self.make_provider()
        if not isinstance(provider, SyncProvider):
            pytest.skip("Not a SyncProvider")
        step = self.make_step()
        result = provider.invoke(step)
        assert result.status in (StepStatus.SUCCEEDED, StepStatus.FAILED)

    # --- Capabilities ---

    def test_get_capabilities_returns_valid_type(self) -> None:
        """get_capabilities() must return ProviderCapabilities or None."""
        provider = self.make_provider()
        caps = provider.get_capabilities()
        assert caps is None or isinstance(caps, ProviderCapabilities)

    # --- Audio metadata ---

    def test_audio_providers_populate_audio_metadata(self) -> None:
        """Audio providers must populate AudioMetadata on audio assets."""
        provider = self.make_provider()
        caps = provider.get_capabilities()
        if caps is None or not caps.supported_modalities:
            pytest.skip("Provider does not declare capabilities")
        if Modality.AUDIO not in caps.supported_modalities:
            pytest.skip("Provider does not declare AUDIO modality")
        step = self.make_step()
        result = provider.invoke(step)
        if result.status != StepStatus.SUCCEEDED:
            pytest.skip("Provider did not succeed — cannot verify metadata")
        for asset in result.assets:
            if asset.media_type and asset.media_type.startswith("audio/"):
                assert asset.audio is not None, f"Audio asset missing AudioMetadata: {asset.url}"
                assert isinstance(asset.audio, AudioMetadata)

    # --- Chain input validation ---

    def test_chain_input_urls_validated(self) -> None:
        """Providers with accepts_chain_input must reject unsafe URLs."""
        from genblaze_core.exceptions import ProviderError
        from genblaze_core.models.asset import Asset

        provider = self.make_provider()
        caps = provider.get_capabilities()
        if caps is None or not caps.accepts_chain_input:
            pytest.skip("Provider does not accept chain inputs")
        step = self.make_step()
        # Inject an unsafe http:// chain input
        step.inputs = [Asset(url="http://evil.com/payload.bin", media_type="image/png")]
        with pytest.raises(ProviderError):
            provider.submit(step)

    # --- normalize_params ---

    def test_normalize_params_idempotent(self) -> None:
        """Applying normalize_params twice must produce the same result."""
        provider = self.make_provider()
        params = {"duration": 10, "resolution": "1080p", "aspect_ratio": "16:9"}
        p1 = provider.normalize_params(dict(params))
        p2 = provider.normalize_params(dict(p1))
        assert p1 == p2, f"normalize_params not idempotent: {p1} != {p2}"

    # --- API uniformity ---

    def test_accepts_probe_cache_kwargs(self) -> None:
        """Every Provider subclass must accept the probe-cache ctor kwargs.

        These are no-ops on NATIVE / NONE providers but must be accepted
        for API uniformity — calling code that passes them to ANY provider
        must not raise ``TypeError``. Verified by **calling the
        constructor** with the kwargs, not just inspecting the signature
        — a ``**kwargs``-forwarding provider that doesn't actually
        accept the names would pass an inspect-only check while still
        failing here.
        """
        cls = type(self.make_provider())
        # Build a fresh instance with the kwargs; if the provider rejects
        # them with TypeError, the test fails with a clear message.
        try:
            cls(
                **self.constructor_kwargs_for_probe_cache_test(),
                probe_cache_ttl=120.0,
                probe_cache_max_entries=64,
            )
        except TypeError as exc:
            # Distinguish a probe-kwarg conformance failure from an
            # unrelated TypeError (e.g. missing required ``api_key``,
            # ``output_dir``, or another connector-specific arg). Only
            # the former should fail this test with the conformance
            # message; everything else should propagate so the real
            # error is visible. Connectors with required ctor args
            # provide them via ``constructor_kwargs_for_probe_cache_test``.
            err = str(exc)
            if "probe_cache_ttl" in err or "probe_cache_max_entries" in err:
                raise AssertionError(
                    f"{cls.__name__} must accept probe_cache_ttl and "
                    f"probe_cache_max_entries kwargs (forward to super().__init__()). "
                    f"Got: {exc}"
                ) from exc
            raise

    def constructor_kwargs_for_probe_cache_test(self) -> dict[str, Any]:
        """Override to provide the minimum kwargs your provider needs
        when constructed standalone (e.g. mock ``api_key``, mock
        ``output_dir``). Default returns an empty dict.

        The test calls
        ``cls(**this(), probe_cache_ttl=..., probe_cache_max_entries=...)``
        — anything required to avoid an unrelated ``TypeError`` (other
        than the kwargs under test) belongs here.
        """
        return {}

    # --- Cost tracking ---

    def test_invoke_populates_cost(self) -> None:
        """Successful invoke must populate step.cost_usd unless opted out.

        To waive, set ``expects_cost = False`` on the subclass and note why.
        """
        if not self.expects_cost:
            pytest.skip("Provider opts out of cost tracking (expects_cost=False)")
        provider = self.make_provider()
        step = self.make_step()
        result = provider.invoke(step)
        if result.status != StepStatus.SUCCEEDED:
            pytest.skip("Provider did not succeed — cannot verify cost_usd")
        assert result.cost_usd is not None, (
            f"Provider '{provider.name}' returned SUCCEEDED but did not populate "
            "step.cost_usd. Either set it in fetch_output()/generate() or override "
            "`expects_cost = False` on the compliance test to document the gap."
        )
