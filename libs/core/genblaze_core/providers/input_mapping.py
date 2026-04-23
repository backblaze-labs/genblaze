"""Chain-input routers.

A ``ModelSpec.input_mapping`` takes ``list[Asset]`` (from ``step.inputs``) and
returns a dict of native-parameter keys → URLs. Packaged helpers cover the
common patterns; users can write bespoke callables for anything else.

Routers should emit **native** parameter names — the prepare-payload pipeline
runs ``input_mapping`` after ``param_aliases`` and does not rewrite its output.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from genblaze_core.models.asset import Asset

InputMapping = Callable[[Sequence[Asset]], dict[str, Any]]


def _by_media_prefix(asset: Asset) -> str:
    """Return 'image', 'video', 'audio', or 'other' from the asset's MIME type."""
    mt = asset.media_type or ""
    if mt.startswith("image/"):
        return "image"
    if mt.startswith("video/"):
        return "video"
    if mt.startswith("audio/"):
        return "audio"
    return "other"


def route_by_media_type(slots: Mapping[str, str]) -> InputMapping:
    """Route the first asset of each media-type prefix to a named slot.

    Example::

        route_by_media_type({"image": "image_uri", "audio": "audio_uri"})

    Returns ``{"image_uri": url}`` or ``{"audio_uri": url}`` from the first
    matching asset. Subsequent assets of the same type are ignored.
    """

    def _mapper(inputs: Sequence[Asset]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        seen: set[str] = set()
        for a in inputs:
            kind = _by_media_prefix(a)
            native = slots.get(kind)
            if native is None or kind in seen:
                continue
            out[native] = a.url
            seen.add(kind)
        return out

    return _mapper


def route_images(
    *,
    slots: Sequence[str] | None = None,
    array_slot: str | None = None,
) -> InputMapping:
    """Route image assets positionally (``slots``) and/or aggregate (``array_slot``).

    Behavior:
    - First N images go to the positional slots (e.g. ``("image", "image_tail")``).
    - Anything left over (or everything if ``slots`` is None) goes into ``array_slot``
      as a list. If ``array_slot`` is None, extras are dropped.

    Examples::

        route_images(slots=("image",))  # single image slot
        route_images(slots=("first_frame", "last_frame"))  # two positional slots
        route_images(array_slot="reference_images")  # everything to array
        route_images(slots=("image",), array_slot="reference_images")  # first + rest
    """
    positional = tuple(slots) if slots else ()

    def _mapper(inputs: Sequence[Asset]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        images = [a for a in inputs if _by_media_prefix(a) == "image"]
        for i, slot in enumerate(positional):
            if i < len(images):
                out[slot] = images[i].url
        overflow = images[len(positional) :]
        if array_slot is not None and overflow:
            out[array_slot] = [a.url for a in overflow]
        return out

    return _mapper


def route_audio(*, slot: str = "audio") -> InputMapping:
    """Route the first audio asset into ``slot``."""

    def _mapper(inputs: Sequence[Asset]) -> dict[str, Any]:
        for a in inputs:
            if _by_media_prefix(a) == "audio":
                return {slot: a.url}
        return {}

    return _mapper


def route_video(*, slot: str = "video") -> InputMapping:
    """Route the first video asset into ``slot``."""

    def _mapper(inputs: Sequence[Asset]) -> dict[str, Any]:
        for a in inputs:
            if _by_media_prefix(a) == "video":
                return {slot: a.url}
        return {}

    return _mapper


def route_keyframes(
    *,
    frames: Sequence[str] = ("frame0", "frame1"),
    key: str = "keyframes",
    wrap: Callable[[Asset], dict[str, Any]] | None = None,
) -> InputMapping:
    """Route images into a nested ``keyframes`` dict (Luma pattern).

    Example output for two image inputs::

        {"keyframes": {"frame0": {"type": "image", "url": "..."},
                       "frame1": {"type": "image", "url": "..."}}}
    """
    wrap = wrap or (lambda a: {"type": "image", "url": a.url})

    def _mapper(inputs: Sequence[Asset]) -> dict[str, Any]:
        images = [a for a in inputs if _by_media_prefix(a) == "image"]
        if not images:
            return {}
        nested: dict[str, Any] = {}
        for i, name in enumerate(frames):
            if i < len(images):
                nested[name] = wrap(images[i])
        return {key: nested}

    return _mapper


def chain_routers(*mappers: InputMapping) -> InputMapping:
    """Compose multiple routers; later keys overwrite earlier (user-friendly merge)."""

    def _mapper(inputs: Sequence[Asset]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for m in mappers:
            out.update(m(inputs))
        return out

    return _mapper
