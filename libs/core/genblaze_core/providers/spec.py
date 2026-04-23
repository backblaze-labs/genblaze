"""ModelSpec and ParamSchema — declarative per-model configuration.

Each provider describes its models as ``ModelSpec`` instances registered in a
``ModelRegistry``. A spec collapses parameter semantics (names, types,
constraints), chain-input routing, and pricing into data rather than code.

Specs are frozen and hashable — safe to share across threads.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.providers.pricing import PricingStrategy

# --- ParamSchema variants ----------------------------------------------------


def _err(key: str, msg: str) -> ProviderError:
    return ProviderError(
        f"Invalid parameter {key!r}: {msg}",
        error_code=ProviderErrorCode.INVALID_INPUT,
    )


@dataclass(frozen=True, slots=True)
class IntSchema:
    """Integer with optional enum membership or min/max bounds."""

    min: int | None = None
    max: int | None = None
    enum: frozenset[int] | None = None

    def validate(self, key: str, value: Any) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise _err(key, f"expected int, got {type(value).__name__}")
        if self.enum is not None and value not in self.enum:
            raise _err(key, f"must be one of {sorted(self.enum)}")
        if self.min is not None and value < self.min:
            raise _err(key, f"must be >= {self.min}")
        if self.max is not None and value > self.max:
            raise _err(key, f"must be <= {self.max}")


@dataclass(frozen=True, slots=True)
class FloatSchema:
    """Float (or int-coerced) with optional min/max bounds."""

    min: float | None = None
    max: float | None = None

    def validate(self, key: str, value: Any) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _err(key, f"expected number, got {type(value).__name__}")
        if self.min is not None and value < self.min:
            raise _err(key, f"must be >= {self.min}")
        if self.max is not None and value > self.max:
            raise _err(key, f"must be <= {self.max}")


@dataclass(frozen=True, slots=True)
class StringSchema:
    """String with optional length bounds, enum, or regex pattern."""

    min_len: int | None = None
    max_len: int | None = None
    enum: frozenset[str] | None = None
    pattern: str | None = None

    def validate(self, key: str, value: Any) -> None:
        if not isinstance(value, str):
            raise _err(key, f"expected string, got {type(value).__name__}")
        if self.enum is not None and value not in self.enum:
            raise _err(key, f"must be one of {sorted(self.enum)}")
        if self.min_len is not None and len(value) < self.min_len:
            raise _err(key, f"length must be >= {self.min_len}")
        if self.max_len is not None and len(value) > self.max_len:
            raise _err(key, f"length must be <= {self.max_len}")
        if self.pattern is not None:
            import re

            if not re.match(self.pattern, value):
                raise _err(key, f"must match pattern {self.pattern!r}")


@dataclass(frozen=True, slots=True)
class EnumSchema:
    """Exact value membership in a set (strings, ints, mixed)."""

    values: frozenset[Any]

    def validate(self, key: str, value: Any) -> None:
        if value not in self.values:
            raise _err(key, f"must be one of {sorted(self.values, key=str)}")


@dataclass(frozen=True, slots=True)
class BoolSchema:
    """Strict boolean (rejects 0/1 ints)."""

    def validate(self, key: str, value: Any) -> None:
        if not isinstance(value, bool):
            raise _err(key, f"expected bool, got {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ArraySchema:
    """List with optional length bounds and per-item schema."""

    min_len: int | None = None
    max_len: int | None = None
    item: Any | None = None  # another ParamSchema

    def validate(self, key: str, value: Any) -> None:
        if not isinstance(value, list):
            raise _err(key, f"expected list, got {type(value).__name__}")
        if self.min_len is not None and len(value) < self.min_len:
            raise _err(key, f"length must be >= {self.min_len}")
        if self.max_len is not None and len(value) > self.max_len:
            raise _err(key, f"length must be <= {self.max_len}")
        if self.item is not None:
            for i, v in enumerate(value):
                self.item.validate(f"{key}[{i}]", v)


ParamSchema = IntSchema | FloatSchema | StringSchema | EnumSchema | BoolSchema | ArraySchema
"""Union of supported param schema types."""


# --- ModelSpec ---------------------------------------------------------------


InputMapping = Callable[[Sequence[Asset]], dict[str, Any]]
Constraint = Callable[[dict[str, Any]], None]
ParamTransformer = Callable[[dict[str, Any]], dict[str, Any]]
ParamCoercer = Callable[[Any], Any]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Declarative per-model configuration.

    Every field except ``model_id`` is optional. Empty spec = no-op (permissive
    pass-through), matching the historical Replicate behavior.

    Args:
        model_id: Provider-native identifier. Used as the registry key.
        aliases: Extra names that resolve to this spec (e.g. dated snapshots).
        deprecated_aliases: Old ids that still resolve but emit DeprecationWarning
            pointing callers to ``model_id``. Use when a provider renames a slug;
            keep the old name here for one minor version before removal.
        modality: Primary output modality — informational, drives capability
            discovery and pipeline compatibility checks.
        pricing: Callable taking ``PricingContext`` and returning USD cost
            (or None if not priceable). Packaged helpers in ``pricing.py``.
        param_aliases: 1:1 canonical-to-native renames. ``{"aspect_ratio": "ratio"}``
            rewrites canonical ``aspect_ratio`` keys to native ``ratio``.
        param_transformer: Function for many-to-one rewrites (e.g. OpenAI Sora's
            ``(resolution, aspect_ratio) → size``). Runs after aliases.
        param_coercers: Per-key type/value coercion. ``{"duration": str}`` turns
            ``5`` into ``"5"`` for Kling. ``{"sound": _bool_to_on_off}``.
        param_schemas: Per-key validation. ``{"duration": IntSchema(min=1, max=15)}``.
        param_defaults: Filled in if user didn't supply. User values win.
        param_required: Keys that must be present after defaults are applied.
        param_allowlist: If set, only these keys are forwarded. ``None`` = pass
            everything (Replicate-style).
        param_constraints: Cross-field rules (``requires_together("a","b")`` etc.).
        input_mapping: Routes ``step.inputs`` into the payload dict. See
            ``input_mapping.py`` for packaged helpers.
        extras: Provider-specific escape hatch — not interpreted by the pipeline.
            Common uses: ``{"envelope_key": "payload"}`` (GMI), ``{"response_format":
            "b64_json"}`` (OpenAI image).
    """

    model_id: str
    aliases: frozenset[str] = field(default_factory=frozenset)
    deprecated_aliases: frozenset[str] = field(default_factory=frozenset)
    modality: Modality | None = None
    pricing: PricingStrategy | None = None
    param_aliases: Mapping[str, str] = field(default_factory=dict)
    param_transformer: ParamTransformer | None = None
    param_coercers: Mapping[str, ParamCoercer] = field(default_factory=dict)
    param_schemas: Mapping[str, ParamSchema] = field(default_factory=dict)
    param_defaults: Mapping[str, Any] = field(default_factory=dict)
    param_required: frozenset[str] = field(default_factory=frozenset)
    param_allowlist: frozenset[str] | None = None
    param_constraints: tuple[Constraint, ...] = ()
    input_mapping: InputMapping | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_permissive(self) -> bool:
        """True if spec performs no transformations — fast-path sentinel."""
        return (
            not self.param_aliases
            and self.param_transformer is None
            and not self.param_coercers
            and not self.param_schemas
            and not self.param_defaults
            and not self.param_required
            and self.param_allowlist is None
            and not self.param_constraints
            and self.input_mapping is None
        )


# The permissive fallback used when no connector-provided spec matches.
FALLBACK_SPEC = ModelSpec(model_id="*")
