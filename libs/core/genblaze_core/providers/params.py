"""ParamSurface — composable per-model allowlist + alias + coercer builder.

Replaces the "share one frozenset across every model" anti-pattern (e.g. the
GMICloud ``_COMMON_ALLOWLIST``) so each ``ModelSpec`` declares the exact
parameter surface its upstream model accepts.

Typical usage::

    _PIXVERSE = (
        ParamSurface.for_modality(Modality.VIDEO)
        .extend("quality")
        .build()
    )
    _BRIA_INPAINT = (
        ParamSurface.for_modality(Modality.IMAGE)
        .extend("mask", "mask_url", "denoise", "strength")
        .build()
    )

    ModelSpec(model_id="pixverse-v5.6-t2v", **_PIXVERSE, ...)

The modality defaults are the union of universally meaningful params for that
modality (``prompt``, ``seed``, ``aspect_ratio``, …). Adding a new universally
useful param is a one-line change here — every connector that builds through
``for_modality`` picks it up on the next import.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from genblaze_core.models.enums import Modality

# Universal across modalities — every generation step has a prompt + seed.
_BASE_PARAMS: frozenset[str] = frozenset({"prompt", "negative_prompt", "seed"})

# Modality-specific defaults. Connectors extend these for model-specific quirks
# (Pixverse `quality`, Bria `mask`, voice clone `pitch`, etc.).
_MODALITY_DEFAULTS: dict[Modality, frozenset[str]] = {
    Modality.IMAGE: _BASE_PARAMS
    | {"aspect_ratio", "resolution", "number_of_images", "image", "image_url"},
    Modality.VIDEO: _BASE_PARAMS
    | {
        "aspect_ratio",
        "resolution",
        "duration",
        "cfg_scale",
        "image",
        "image_url",
        "video",
        "video_url",
    },
    Modality.AUDIO: _BASE_PARAMS
    | {
        "voice_id",
        "language",
        "duration",
        "output_format",
        "reference_audio",
    },
    Modality.TEXT: _BASE_PARAMS,
}


@dataclass(frozen=True, slots=True)
class ParamSurface:
    """Composable bundle of ``ModelSpec`` parameter fields.

    Build with ``for_modality`` or ``empty``, chain ``extend`` / ``remove`` /
    ``with_aliases`` / ``with_coercers`` calls (each returns a new frozen
    instance), then call ``build()`` to get the kwargs dict for ``ModelSpec``.
    """

    allowlist: frozenset[str] = field(default_factory=frozenset)
    aliases: Mapping[str, str] = field(default_factory=dict)
    coercers: Mapping[str, Callable[[Any], Any]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> ParamSurface:
        """Start with no allowlist (matches Replicate-style permissive pass-through)."""
        return cls()

    @classmethod
    def for_modality(cls, modality: Modality) -> ParamSurface:
        """Start from the universal-defaults surface for a modality."""
        defaults = _MODALITY_DEFAULTS.get(modality, _BASE_PARAMS)
        return cls(allowlist=defaults)

    def extend(self, *params: str) -> ParamSurface:
        """Add params to the allowlist."""
        return replace(self, allowlist=self.allowlist | frozenset(params))

    def remove(self, *params: str) -> ParamSurface:
        """Drop params from the allowlist (e.g. when a model doesn't accept ``seed``)."""
        return replace(self, allowlist=self.allowlist - frozenset(params))

    def with_aliases(
        self, mapping: Mapping[str, str] | None = None, /, **kwargs: str
    ) -> ParamSurface:
        """Merge canonical→native renames. Later calls override earlier keys.

        **Note:** the rename *target* (the value side of each pair) is
        auto-added to the allowlist. This is necessary because the alias is
        the whole point — if the target weren't allowed, the renamed value
        would be dropped at the allowlist filter and the alias would be a
        no-op. To suppress a renamed key, drop it explicitly *after* the
        alias call: ``surface.with_aliases(foo="bar").remove("bar")``.
        """
        merged: dict[str, str] = dict(self.aliases)
        if mapping:
            merged.update(mapping)
        merged.update(kwargs)
        return replace(
            self,
            aliases=merged,
            allowlist=self.allowlist | frozenset(merged.values()),
        )

    def with_coercers(
        self,
        mapping: Mapping[str, Callable[[Any], Any]] | None = None,
        /,
        **kwargs: Callable[[Any], Any],
    ) -> ParamSurface:
        """Merge per-key value coercers (e.g. ``duration=str`` for Kling)."""
        merged: dict[str, Callable[[Any], Any]] = dict(self.coercers)
        if mapping:
            merged.update(mapping)
        merged.update(kwargs)
        return replace(self, coercers=merged)

    def build(self) -> dict[str, Any]:
        """Return the kwargs dict to splat into ``ModelSpec(...)``."""
        out: dict[str, Any] = {}
        if self.allowlist:
            out["param_allowlist"] = self.allowlist
        if self.aliases:
            out["param_aliases"] = dict(self.aliases)
        if self.coercers:
            out["param_coercers"] = dict(self.coercers)
        return out


def modality_defaults(modality: Modality) -> frozenset[str]:
    """Return the default param allowlist for a modality (read-only view)."""
    return _MODALITY_DEFAULTS.get(modality, _BASE_PARAMS)


def register_modality_default(modality: Modality, params: Iterable[str]) -> None:
    """Add params to a modality's default surface for *all* connectors.

    Use sparingly — this affects every provider that built through
    ``for_modality``. Intended for adding a newly-universal param (e.g. when
    the entire industry adopts ``aspect_ratio``); not for connector-specific
    quirks.
    """
    current = _MODALITY_DEFAULTS.get(modality, _BASE_PARAMS)
    _MODALITY_DEFAULTS[modality] = current | frozenset(params)
