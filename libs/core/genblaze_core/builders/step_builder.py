"""Fluent builder for Step models."""

from __future__ import annotations

from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, PromptVisibility, StepStatus, StepType
from genblaze_core.models.step import Step


class StepBuilder:
    """Fluent builder for Step models.

    Use ``StepBuilder(provider, model)`` to start a chain, then call methods
    (all returning ``self`` for chaining). Call ``.build()`` to produce a
    ``Step`` instance.
    """

    def __init__(self, provider: str, model: str):
        self._data: dict = {"provider": provider, "model": model}

    def prompt(self, text: str) -> StepBuilder:
        """Set the generation prompt text."""
        self._data["prompt"] = text
        return self

    def negative_prompt(self, text: str) -> StepBuilder:
        """Set a negative prompt to steer the model away from certain concepts."""
        self._data["negative_prompt"] = text
        return self

    def modality(self, m: Modality) -> StepBuilder:
        """Set the output modality (e.g. ``Modality.IMAGE``)."""
        self._data["modality"] = m
        return self

    def visibility(self, v: PromptVisibility) -> StepBuilder:
        """Set the prompt redaction level."""
        self._data["prompt_visibility"] = v
        return self

    def step_type(self, t: StepType) -> StepBuilder:
        """Set the step type (e.g. ``StepType.GENERATE``)."""
        self._data["step_type"] = t
        return self

    def seed(self, s: int) -> StepBuilder:
        """Set a random seed for reproducibility."""
        self._data["seed"] = s
        return self

    def model_version(self, v: str) -> StepBuilder:
        """Set a specific model version hash."""
        self._data["model_version"] = v
        return self

    def model_hash(self, h: str) -> StepBuilder:
        """Set a model weights hash for integrity verification."""
        self._data["model_hash"] = h
        return self

    def input_asset(self, url: str, media_type: str, **kwargs) -> StepBuilder:
        """Add an input asset URL and MIME type.

        Extra keyword arguments are forwarded to ``Asset``.
        """
        self._data.setdefault("inputs", []).append(Asset(url=url, media_type=media_type, **kwargs))
        return self

    def params(self, **kwargs) -> StepBuilder:
        """Add provider-specific parameters as key-value pairs."""
        self._data.setdefault("params", {}).update(kwargs)
        return self

    def status(self, s: StepStatus) -> StepBuilder:
        """Set the initial step status."""
        self._data["status"] = s
        return self

    def asset(self, url: str, media_type: str, **kwargs) -> StepBuilder:
        """Add an output asset URL and MIME type.

        Extra keyword arguments are forwarded to ``Asset``.
        """
        self._data.setdefault("assets", []).append(Asset(url=url, media_type=media_type, **kwargs))
        return self

    def meta(self, **kwargs) -> StepBuilder:
        """Add arbitrary metadata as key-value pairs."""
        self._data.setdefault("metadata", {}).update(kwargs)
        return self

    def build(self) -> Step:
        """Build and return a ``Step`` instance from the accumulated data."""
        return Step(**self._data)
