"""``KeyBuilder`` — pure value-object for storage keys with seam-only dedupe.

Phase 1C of the storage-backend hardening tranche introduces this primitive
to fix bug #5: under :class:`KeyStrategy.HIERARCHICAL`, a caller passing
``prefix="runs"`` produced keys like ``runs/runs/{tenant}/{date}/{run_id}/...``
because the strategy's hardcoded ``runs/`` segment got concatenated to a
prefix that already ended in ``runs``. Same issue for any prefix whose
last segment matches the strategy's leading segment.

Why a dedicated primitive instead of a string-replace fix:

* **Composability** — the bug shows up at four call sites
  (``ObjectStorageSink.__init__``, ``ObjectStorageSink.manifest_key_for``,
  ``AssetTransfer._build_key``, and the temp-key construction in
  ``_transfer_pipelined``). One value object replaces four ad-hoc joins.
* **Boundary discipline** — dedupe is applied only at the prefix↔args
  seam. Duplicates within the prefix (e.g. ``"archive/archive"``) or
  within the segments list are preserved as caller intent. This is the
  smallest possible blast radius for the fix; existing buckets only
  see key changes for the documented dup-prone case.
* **Idempotency** — ``KeyBuilder.from_prefix(...).prefix`` is normalized
  once at construction (no leading/trailing slashes, no consecutive
  slashes); subsequent ``build`` / ``append`` calls preserve invariants.

The class is :func:`@dataclass(frozen=True, slots=True)` for parity with
:class:`StorageConfig` and :class:`RetryPolicy` — pure-Python config
helpers, not wire models. Hashable, immutable.
"""

from __future__ import annotations

from dataclasses import dataclass


def _normalize_segments(s: str) -> list[str]:
    """Split a path on ``/`` and drop empty segments.

    Preserves intentional duplicates within ``s`` (``"archive/archive"``
    stays two segments), strips leading/trailing slashes, and collapses
    consecutive separators (``"a//b"`` → ``["a", "b"]``).
    """
    return [p for p in s.split("/") if p]


def _join_with_seam_dedupe(prefix_parts: list[str], arg_parts: list[str]) -> str:
    """Join prefix and args, dropping a single seam-duplicate if present.

    Only checks the seam (last of prefix vs first of args). Does NOT walk
    the full path looking for global duplicates — that would silently
    mangle paths like ``a/b/a/b/data`` where the doubled ``a/b`` is the
    caller's intent.
    """
    if prefix_parts and arg_parts and prefix_parts[-1] == arg_parts[0]:
        arg_parts = arg_parts[1:]
    return "/".join(prefix_parts + arg_parts)


@dataclass(frozen=True, slots=True)
class KeyBuilder:
    """A normalized prefix + safe key-construction helpers.

    Construct via :meth:`from_prefix` rather than the dataclass directly —
    the classmethod runs the normalization pass that the rest of the API
    relies on (no leading/trailing slashes, no consecutive separators).

    Use :meth:`append` to extend the prefix into a new ``KeyBuilder``
    (e.g. when a sink layer wants to reserve a sub-tree like ``"runs/"``
    or ``"assets/"`` and hand it to a downstream component).

    Use :meth:`build` to produce a final key string from the prefix
    plus terminal segments.

    Both ``append`` and ``build`` apply seam-only dedupe: if the last
    segment of the existing prefix matches the first segment of the new
    arguments, the duplicate is dropped. Duplicates *within* the prefix
    or *within* the argument list are preserved.
    """

    prefix: str

    @classmethod
    def from_prefix(cls, prefix: str) -> KeyBuilder:
        """Normalize ``prefix`` and return a ``KeyBuilder``.

        Normalization strips leading/trailing slashes and collapses
        consecutive separators. Empty / whitespace-only prefixes
        produce an empty-prefix builder.
        """
        return cls(prefix="/".join(_normalize_segments(prefix)))

    def append(self, *segments: str) -> KeyBuilder:
        """Return a new ``KeyBuilder`` with ``segments`` appended to the prefix.

        The seam-dedupe rule applies: if the last existing prefix
        segment equals the first non-empty appended segment, one of
        the duplicates is dropped. Empty / slash-only segments are
        skipped so callers can pass conditional values like
        ``builder.append(tenant_id or "")``.
        """
        prefix_parts = _normalize_segments(self.prefix)
        arg_parts: list[str] = []
        for seg in segments:
            arg_parts.extend(_normalize_segments(seg))
        return KeyBuilder(prefix=_join_with_seam_dedupe(prefix_parts, arg_parts))

    def build(self, *segments: str) -> str:
        """Return the full key formed by joining ``segments`` to the prefix.

        Equivalent to ``self.append(*segments).prefix`` but spelled
        explicitly because callers are typically building a terminal key
        (e.g. ``"manifests/{run_id}.json"``) rather than carrying the
        builder forward.
        """
        prefix_parts = _normalize_segments(self.prefix)
        arg_parts: list[str] = []
        for seg in segments:
            arg_parts.extend(_normalize_segments(seg))
        return _join_with_seam_dedupe(prefix_parts, arg_parts)


__all__ = ["KeyBuilder"]
