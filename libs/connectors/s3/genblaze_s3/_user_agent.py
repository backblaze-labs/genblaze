"""User-agent assembly for ``S3StorageBackend``.

Plan 5 Phase 1C — replaces the historic hardcoded
``_USER_AGENT = f"b2ai-genblaze/{__version__}"`` constant at
``backend.py:22``. The version now flows through
:mod:`genblaze_core._version` (which itself reads from
``importlib.metadata`` per Phase 1A), so the user-agent string is
always coherent with the installed wheel — no manual sync per
release.

The ``b2ai-`` prefix is a Backblaze-attribution convention and stays
intentional. Caller apps publishing samples under the parent
``sampleapps/`` repository follow the
``<app-slug>/<version>`` convention; pass that string via ``extra=``
(or :class:`StorageConfig.user_agent_extra`) to compose
``b2ai-genblaze/0.X.Y <my-app>/1.2.3`` for B2's request-attribution
logs.

The default ``base`` can be overridden when a caller wants to
replace the prefix entirely (e.g. an internal-fork rebrand) — pass
``base="my-fork/0.1"``.
"""

from __future__ import annotations

from genblaze_core._version import __version__ as _CORE_VERSION

# Default user-agent prefix. The ``b2ai-`` namespace is reserved for
# Backblaze's AI-generated-content tooling — using it is the
# documented attribution path for B2 usage reporting.
_DEFAULT_BASE = f"b2ai-genblaze/{_CORE_VERSION}"


def build_user_agent(*, base: str | None = None, extra: str | None = None) -> str:
    """Assemble the user-agent string for S3 requests.

    Args:
        base: Optional override for the prefix. ``None`` (default)
            uses ``b2ai-genblaze/<genblaze-core-version>``.
        extra: Optional append. Apps publishing samples under
            Backblaze's ``sampleapps/`` standard pass their
            ``<app-slug>/<version>`` here so B2 logs attribute
            requests at the app level.

    Returns:
        The composed user-agent string. Never empty.

    Examples:
        ``build_user_agent()`` → ``"b2ai-genblaze/0.2.7"``
        ``build_user_agent(extra="my-app/1.0")`` →
            ``"b2ai-genblaze/0.2.7 my-app/1.0"``
        ``build_user_agent(base="my-fork/0.1", extra="ext/2")`` →
            ``"my-fork/0.1 ext/2"``
    """
    parts: list[str] = [base or _DEFAULT_BASE]
    if extra:
        parts.append(extra)
    return " ".join(parts)


__all__ = ["build_user_agent"]
