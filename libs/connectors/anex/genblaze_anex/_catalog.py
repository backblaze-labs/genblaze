"""Reader for anex.sh's public model catalog.

``GET https://anex.sh/api/estimate/catalog`` needs no key and lists every
model the service offers (https://anex.sh/docs/video-models). A video model's
entry has ``task`` ``generate_video`` and a ``video`` block::

    {"id": "minimax-h3", "task": "generate_video",
     "video": {"name": "MiniMax-H3", "names": ["MiniMax-H3"],
               "kinds": ["text", "frames"], "resolutions": ["768P", "2K", "4K"],
               "supports_seed": false},
     "media_fees": {"video_output_usd_per_second": "0.068", ...}}

``video.names`` are the model names a request may use. One name can be listed
by several entries (``MiniMax-H3`` reaches the text and frame model and, with
a reference, the reference model), so a name's profile is the union of the
entries that list it. The upscaler has ``task`` ``upscale_video`` and no
``video`` block. Unknown fields, unknown tasks and malformed entries are
skipped, so a catalog that grows new fields or models still parses.

Nothing here holds a price. :class:`CatalogRates` carries the rates the
catalog published when it was read, for callers who register a pricing
strategy.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

DEFAULT_CATALOG_URL = "https://anex.sh/api/estimate/catalog"

# The request kinds a model answers: text to video, first and last frame, and
# references (images, audio, video editing).
KIND_TEXT = "text"
KIND_FRAMES = "frames"
KIND_REFERENCE = "reference"
_KINDS = frozenset({KIND_TEXT, KIND_FRAMES, KIND_REFERENCE})

_GENERATE_TASK = "generate_video"
_UPSCALE_TASK = "upscale_video"

# A model name is sent as the request's ``model`` and offered to callers, so
# only plain names are taken from the catalog.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RESOLUTION_RE = re.compile(r"[A-Za-z0-9]{1,16}")
# A price above this (USD per second, or per image) is not a price.
_MAX_USD = Decimal(100)


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """What one model name accepts.

    Args:
        kinds: Request kinds the name answers (``text``, ``frames``, ``reference``).
        resolutions: Resolutions it renders, in catalog order.
        seed_kinds: The kinds on which a seed is honoured.
    """

    kinds: frozenset[str]
    resolutions: tuple[str, ...]
    seed_kinds: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CatalogRates:
    """Rates the catalog published when it was read, in USD.

    Args:
        per_second: ``{model name: {resolution: USD per second of video}}``.
            A name whose catalog entries disagree on a rate is left out.
        upscale_per_second: ``{resolution: USD per second}`` for upscaling a
            finished clip.
        free_images: ``{model name: images included in a request}``.
        image_usd: ``{model name: USD per image past the included ones}``.
    """

    per_second: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    upscale_per_second: Mapping[str, float] = field(default_factory=dict)
    free_images: Mapping[str, int] = field(default_factory=dict)
    image_usd: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Catalog:
    """The video part of one catalog answer."""

    models: Mapping[str, ModelProfile]
    rates: CatalogRates


def _usd(value: Any) -> Decimal | None:
    """A catalog price (a decimal string), or None when absent or malformed."""
    if value is None or isinstance(value, bool):
        return None
    try:
        usd = Decimal(str(value))
    except InvalidOperation:
        return None
    return usd if usd.is_finite() and 0 <= usd <= _MAX_USD else None


def _strings(value: Any, pattern: re.Pattern[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and pattern.fullmatch(item)]


def _entry_rates(fees: Any, resolutions: tuple[str, ...]) -> dict[str, float]:
    """USD per second at each resolution: the 768P rate plus the 2K or 4K add-on."""
    if not isinstance(fees, dict):
        return {}
    base = _usd(fees.get("video_output_usd_per_second"))
    if base is None:
        return {}
    add_ons = {
        "768P": Decimal(0),
        "2K": _usd(fees.get("video_upscale_usd_per_second")),
        "4K": _usd(fees.get("video_upscale_4k_usd_per_second")),
    }
    per_resolution: dict[str, float] = {}
    for resolution in resolutions:
        add_on = add_ons.get(resolution)
        if add_on is not None:
            per_resolution[resolution] = float(base + add_on)
    return per_resolution


def _upscale_rates(fees: Any) -> dict[str, float]:
    if not isinstance(fees, dict):
        return {}
    published = {
        "2K": _usd(fees.get("video_output_usd_per_second")),
        "4K": _usd(fees.get("video_output_4k_usd_per_second")),
    }
    return {resolution: float(usd) for resolution, usd in published.items() if usd is not None}


def parse_catalog(payload: Any) -> Catalog:
    """Read the video models out of a catalog answer.

    Raises:
        ValueError: The answer is not a catalog, or lists no usable video
            model. An empty listing is treated as unreadable, so a broken
            answer can never make every model look retired.
    """
    entries = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("catalog has no 'models' list")

    kinds: dict[str, set[str]] = {}
    resolutions: dict[str, list[str]] = {}
    seed_kinds: dict[str, set[str]] = {}
    # Per name, every distinct value its entries give; one value means agreement.
    per_second: dict[str, list[dict[str, float]]] = {}
    free_images: dict[str, set[int]] = {}
    image_usd: dict[str, set[float]] = {}
    upscale_per_second: dict[str, float] = {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        task = entry.get("task")
        fees = entry.get("media_fees")
        if task == _UPSCALE_TASK:
            upscale_per_second.update(_upscale_rates(fees))
            continue
        video = entry.get("video")
        if task != _GENERATE_TASK or not isinstance(video, dict):
            continue
        names = _strings(video.get("names"), _NAME_RE)
        if not names and isinstance(video.get("name"), str):
            names = _strings([video["name"]], _NAME_RE)
        entry_kinds = [k for k in _strings(video.get("kinds"), _RESOLUTION_RE) if k in _KINDS]
        entry_resolutions = tuple(_strings(video.get("resolutions"), _RESOLUTION_RE))
        if not names or not entry_kinds or not entry_resolutions:
            continue
        rates = _entry_rates(fees, entry_resolutions)
        fees = fees if isinstance(fees, dict) else {}
        free = fees.get("video_free_images")
        free = free if isinstance(free, int) and not isinstance(free, bool) and free >= 0 else 0
        image = _usd(fees.get("video_input_image_usd"))
        for name in names:
            kinds.setdefault(name, set()).update(entry_kinds)
            known = resolutions.setdefault(name, [])
            known.extend(r for r in entry_resolutions if r not in known)
            if video.get("supports_seed") is True:
                seed_kinds.setdefault(name, set()).update(entry_kinds)
            if rates not in per_second.setdefault(name, []):
                per_second[name].append(rates)
            free_images.setdefault(name, set()).add(free)
            if image is not None:
                image_usd.setdefault(name, set()).add(float(image))

    if not kinds:
        raise ValueError("catalog lists no video models")

    models = {
        name: ModelProfile(
            kinds=frozenset(kinds[name]),
            resolutions=tuple(resolutions[name]),
            seed_kinds=frozenset(seed_kinds.get(name, ())),
        )
        for name in kinds
    }
    return Catalog(
        models=models,
        rates=CatalogRates(
            per_second={n: v[0] for n, v in per_second.items() if len(v) == 1 and v[0]},
            upscale_per_second=upscale_per_second,
            free_images={n: next(iter(v)) for n, v in free_images.items() if len(v) == 1},
            image_usd={n: next(iter(v)) for n, v in image_usd.items() if len(v) == 1},
        ),
    )
