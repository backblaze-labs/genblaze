"""Provider discovery + shared instantiation helpers."""

from __future__ import annotations

import importlib.metadata
import inspect
import logging
from typing import Any

from genblaze_core.providers.base import BaseProvider

logger = logging.getLogger("genblaze.provider")

ENTRY_POINT_GROUP = "genblaze.providers"


# Credential parameter names known to genblaze connectors. Tooling that needs
# to instantiate a provider without caring which name it uses (conformance
# tests, probe runner, model-matrix renderer) reads this list.
CREDENTIAL_KWARGS: tuple[str, ...] = (
    "api_key",
    "api_token",
    "api_secret",
    "auth_token",
    "token",
)


def discover_providers() -> dict[str, type[BaseProvider]]:
    """Discover installed provider plugins via entry points.

    Returns a dict mapping provider name → provider class for all packages
    that register under the ``genblaze.providers`` entry point group.
    """
    providers: dict[str, type[BaseProvider]] = {}
    # group= kwarg available since Python 3.9; project requires 3.11+
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            cls = ep.load()
            if isinstance(cls, type) and issubclass(cls, BaseProvider):
                providers[ep.name] = cls
            else:
                logger.warning("Entry point %r is not a BaseProvider subclass", ep.name)
        except Exception:
            logger.warning("Failed to load provider entry point %r", ep.name, exc_info=True)
    return providers


def instantiate_with_credential(
    cls: type[BaseProvider],
    credential: str = "test-key",
    **extra: Any,
) -> BaseProvider:
    """Instantiate ``cls`` with the first matching credential kwarg.

    Connectors disagree on the credential parameter name (``api_key`` vs
    ``api_token`` vs ``api_secret`` vs ``auth_token``). This helper inspects
    the constructor signature and supplies ``credential`` to whichever name
    the connector accepts. ``extra`` kwargs (e.g. ``models=``) are forwarded
    verbatim and override the credential default if they collide.

    Used by the conformance suite, ``tools/probe_models.py``, and
    ``tools/gen_model_matrix.py`` so they don't each duplicate the
    credential-name fallback logic.
    """
    sig = inspect.signature(cls.__init__)
    kwargs: dict[str, Any] = dict(extra)
    for name in CREDENTIAL_KWARGS:
        if name in sig.parameters:
            kwargs.setdefault(name, credential)
            break
    return cls(**kwargs)  # type: ignore[arg-type]
