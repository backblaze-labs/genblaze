"""Cross-field parameter constraints used by ``ModelSpec.param_constraints``.

Each helper returns ``Callable[[dict], None]`` that raises ``ProviderError``
with ``INVALID_INPUT`` on violation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode

Constraint = Callable[[dict[str, Any]], None]


def _raise(msg: str) -> None:
    raise ProviderError(msg, error_code=ProviderErrorCode.INVALID_INPUT)


def requires_together(*keys: str) -> Constraint:
    """All keys must be present, or none."""

    def _check(params: dict[str, Any]) -> None:
        present = [k for k in keys if k in params]
        if 0 < len(present) < len(keys):
            missing = [k for k in keys if k not in params]
            _raise(f"Parameters {list(keys)} must be supplied together; missing: {missing}")

    return _check


def mutually_exclusive(*keys: str) -> Constraint:
    """At most one of the keys may be present."""

    def _check(params: dict[str, Any]) -> None:
        present = [k for k in keys if k in params]
        if len(present) > 1:
            _raise(f"Parameters {present} are mutually exclusive; provide only one")

    return _check


def required_one_of(*keys: str) -> Constraint:
    """At least one of the keys must be present."""

    def _check(params: dict[str, Any]) -> None:
        if not any(k in params for k in keys):
            _raise(f"At least one of {list(keys)} is required")

    return _check


def implies(predicate: str, required: str) -> Constraint:
    """If ``predicate`` is present (truthy), ``required`` must also be present."""

    def _check(params: dict[str, Any]) -> None:
        if params.get(predicate) and required not in params:
            _raise(f"Parameter {required!r} is required when {predicate!r} is set")

    return _check
