"""Parametric provider-contract tests.

Every entry-point-discovered provider must:
- Construct without error given ``api_key="test-key"`` and ``models=<Registry>``.
  (Catches the P0-03 class of bugs: connector overrides ``__init__`` and forgets
  to forward ``models=`` to ``BaseProvider``.)
- Expose the new contract methods (``preflight_auth``, ``probe_model``,
  ``list_voices``, ``list_models``, ``estimate_cost``) as callables.
- Return correct types from the safe-to-call hooks (``list_models``,
  ``list_voices``, ``estimate_cost``).

These tests do **no** network I/O — ``GENBLAZE_SKIP_PREFLIGHT`` is set on the
test process and we never call ``submit`` / ``poll`` / ``fetch_output`` /
``preflight_auth`` / ``probe_model`` (those would dial out for connectors that
override them).

If discovery returns no providers, the suite skips with a clear message — that
means the test runner is in a venv without any connector installed, which is
fine but uninformative.
"""

from __future__ import annotations

import inspect
import os
from decimal import Decimal

import pytest
from genblaze_core.providers import (
    BaseProvider,
    ModelRegistry,
    ModelSpec,
    ProbeResult,
    discover_providers,
    instantiate_with_credential,
)

# Disable the auto preflight for the entire suite — we instantiate providers
# but never invoke them, so a stray preflight would dial out with bogus creds.
os.environ.setdefault("GENBLAZE_SKIP_PREFLIGHT", "1")


def _provider_classes() -> list[type[BaseProvider]]:
    """Sorted list of every entry-point-discovered provider class."""
    return [cls for _, cls in sorted(discover_providers().items())]


_PROVIDER_CLASSES = _provider_classes()


if not _PROVIDER_CLASSES:
    pytest.skip(
        "No genblaze providers discovered via entry points — install at least one "
        "connector (`pip install genblaze-gmicloud`) to run conformance tests.",
        allow_module_level=True,
    )


def _instantiate(cls: type[BaseProvider], **extra: object) -> BaseProvider:
    """Thin wrapper around the shared ``instantiate_with_credential`` helper."""
    return instantiate_with_credential(cls, "test-key-conformance", **extra)


# --- Construction contracts --------------------------------------------------


@pytest.mark.parametrize("cls", _PROVIDER_CLASSES, ids=lambda c: c.__name__)
def test_constructs_with_test_api_key(cls: type[BaseProvider]) -> None:
    """Smoke check — every provider can be built with a fake key."""
    provider = _instantiate(cls)
    assert isinstance(provider, BaseProvider)


@pytest.mark.parametrize("cls", _PROVIDER_CLASSES, ids=lambda c: c.__name__)
def test_accepts_models_kwarg(cls: type[BaseProvider]) -> None:
    """``Provider(api_key=..., models=reg)`` must not raise (closes P0-03 generically).

    Connectors that override ``__init__`` must forward ``models=`` to
    ``super().__init__``; otherwise the documented per-instance registry
    override is broken.
    """
    custom = cls.models_default().fork()
    provider = _instantiate(cls, models=custom)
    assert provider.models is custom, (
        f"{cls.__name__} ignored the models= kwarg — forward it to super().__init__()."
    )


@pytest.mark.parametrize("cls", _PROVIDER_CLASSES, ids=lambda c: c.__name__)
def test_accepts_retry_policy_kwarg(cls: type[BaseProvider]) -> None:
    """``Provider(api_key=..., retry_policy=p)`` must not raise.

    Connectors that override ``__init__`` must forward ``retry_policy=`` to
    ``super().__init__``; otherwise per-instance retry tuning is broken.
    Using a recognizable preset so a forwarding bug surfaces as a value
    mismatch rather than a silent default.
    """
    from genblaze_core.providers import RetryPolicy

    custom = RetryPolicy.conservative()
    provider = _instantiate(cls, retry_policy=custom)
    assert provider.retry_policy is custom, (
        f"{cls.__name__} ignored the retry_policy= kwarg — forward it to super().__init__()."
    )


# --- Method-presence contracts ----------------------------------------------

_HOOKS = (
    "preflight_auth",
    "probe_model",
    "list_voices",
    "list_models",
    "estimate_cost",
)


@pytest.mark.parametrize("cls", _PROVIDER_CLASSES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("method", _HOOKS)
def test_hook_is_callable(cls: type[BaseProvider], method: str) -> None:
    """All standardization hooks resolve as callables on every provider."""
    provider = _instantiate(cls)
    fn = getattr(provider, method, None)
    assert callable(fn), f"{cls.__name__}.{method} is not callable"


# --- Safe-to-call hook return types -----------------------------------------


@pytest.mark.parametrize("cls", _PROVIDER_CLASSES, ids=lambda c: c.__name__)
def test_list_models_returns_modelspecs(cls: type[BaseProvider]) -> None:
    provider = _instantiate(cls)
    specs = provider.list_models()
    assert isinstance(specs, list)
    for spec in specs:
        assert isinstance(spec, ModelSpec)


@pytest.mark.parametrize("cls", _PROVIDER_CLASSES, ids=lambda c: c.__name__)
def test_list_voices_returns_voices(cls: type[BaseProvider]) -> None:
    """``list_voices()`` may return [] (default) or a list[Voice]; never None."""
    provider = _instantiate(cls)
    # Skip live-API connectors that would dial out; recognized by overriding the
    # method on a non-BaseProvider class. Default impl always returns [].
    if cls.list_voices is BaseProvider.list_voices:
        assert provider.list_voices() == []
        return
    # Custom impl — test signature only; calling it might hit the network.
    sig = inspect.signature(provider.list_voices)
    assert "model" in sig.parameters and "language" in sig.parameters


@pytest.mark.parametrize("cls", _PROVIDER_CLASSES, ids=lambda c: c.__name__)
def test_estimate_cost_offline(cls: type[BaseProvider]) -> None:
    """``estimate_cost`` must return ``Decimal`` or ``None`` without networking.

    Walks every priced model in the default registry and confirms the synthesized
    pricing context produces a numeric estimate. Models without ``pricing`` set
    return ``None`` and are skipped (they're not estimable without a real run).
    """
    provider = _instantiate(cls)
    for model_id, spec in provider.models.items():
        if spec.pricing is None:
            assert provider.estimate_cost(model_id) is None
            continue
        # Some pricing strategies (per-second, per-input-chars) need a hint —
        # supply ``duration=5`` and a non-empty prompt so they have data to
        # work with. None is still a valid return for response-only pricing.
        cost = provider.estimate_cost(model_id, params={"duration": 5, "prompt": "test"})
        assert cost is None or isinstance(cost, Decimal), (
            f"{cls.__name__}.estimate_cost({model_id}) returned {type(cost).__name__}"
        )


@pytest.mark.parametrize("cls", _PROVIDER_CLASSES, ids=lambda c: c.__name__)
def test_probe_model_default_skips(cls: type[BaseProvider]) -> None:
    """Default ``probe_model`` is a no-op returning SKIPPED.

    Connectors that override ``probe_model`` are exercised by
    ``tools/probe_models.py`` against real credentials, not the unit suite.
    Here we just confirm the default behavior is observable.
    """
    provider = _instantiate(cls)
    if cls.probe_model is BaseProvider.probe_model:
        result = provider.probe_model("nonexistent-model-xyz")
        assert isinstance(result, ProbeResult)
        assert result.status.value == "skipped"


# --- Registry iteration contracts -------------------------------------------


@pytest.mark.parametrize("cls", _PROVIDER_CLASSES, ids=lambda c: c.__name__)
def test_registry_is_iterable(cls: type[BaseProvider]) -> None:
    """``ModelRegistry`` exposes ``__iter__`` / ``items`` / ``__contains__`` / ``__len__``."""
    reg = cls.models_default()
    assert isinstance(reg, ModelRegistry)
    ids_via_iter = list(reg)
    ids_via_known = reg.known()
    assert ids_via_iter == ids_via_known
    assert len(reg) == len(ids_via_known)
    for mid, spec in reg.items():
        assert isinstance(mid, str)
        assert isinstance(spec, ModelSpec)
        assert mid in reg
