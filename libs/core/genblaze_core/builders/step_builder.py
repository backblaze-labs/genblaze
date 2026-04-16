"""Fluent builder for Step models."""

from __future__ import annotations

from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, PromptVisibility, StepStatus, StepType
from genblaze_core.models.step import Step


class StepBuilder:
    def __init__(self, provider: str, model: str):
        self._data: dict = {"provider": provider, "model": model}

    def prompt(self, text: str) -> StepBuilder:
        self._data["prompt"] = text
        return self

    def negative_prompt(self, text: str) -> StepBuilder:
        self._data["negative_prompt"] = text
        return self

    def modality(self, m: Modality) -> StepBuilder:
        self._data["modality"] = m
        return self

    def visibility(self, v: PromptVisibility) -> StepBuilder:
        self._data["prompt_visibility"] = v
        return self

    def step_type(self, t: StepType) -> StepBuilder:
        self._data["step_type"] = t
        return self

    def seed(self, s: int) -> StepBuilder:
        self._data["seed"] = s
        return self

    def model_version(self, v: str) -> StepBuilder:
        self._data["model_version"] = v
        return self

    def model_hash(self, h: str) -> StepBuilder:
        self._data["model_hash"] = h
        return self

    def input_asset(self, url: str, media_type: str, **kwargs) -> StepBuilder:
        self._data.setdefault("inputs", []).append(Asset(url=url, media_type=media_type, **kwargs))
        return self

    def params(self, **kwargs) -> StepBuilder:
        self._data.setdefault("params", {}).update(kwargs)
        return self

    def status(self, s: StepStatus) -> StepBuilder:
        self._data["status"] = s
        return self

    def asset(self, url: str, media_type: str, **kwargs) -> StepBuilder:
        self._data.setdefault("assets", []).append(Asset(url=url, media_type=media_type, **kwargs))
        return self

    def meta(self, **kwargs) -> StepBuilder:
        self._data.setdefault("metadata", {}).update(kwargs)
        return self

    def build(self) -> Step:
        return Step(**self._data)
