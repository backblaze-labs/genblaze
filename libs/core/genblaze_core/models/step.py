"""Step model — a single generation step within a run."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from genblaze_core._utils import new_id
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import (
    RETRYABLE_ERROR_CODES,
    Modality,
    PromptVisibility,
    ProviderErrorCode,
    StepStatus,
    StepType,
)


class Step(BaseModel):
    """A single generation step within a run."""

    step_id: str = Field(default_factory=new_id, description="Unique step identifier (UUID).")
    run_id: str | None = Field(default=None, description="Parent run ID. Set by RunBuilder.")
    provider: str = Field(description="Provider name (e.g. 'replicate').")
    model: str = Field(description="Model identifier (e.g. 'black-forest-labs/flux-schnell').")
    step_type: StepType = Field(default=StepType.GENERATE, description="Type of operation.")
    model_version: str | None = Field(default=None, description="Specific model version hash.")
    model_hash: str | None = Field(default=None, description="Model weights hash.")
    modality: Modality = Field(default=Modality.IMAGE, description="Output modality.")
    prompt: str | None = Field(default=None, description="Generation prompt text.")
    negative_prompt: str | None = Field(default=None, description="Negative prompt text.")
    prompt_visibility: PromptVisibility = Field(
        default=PromptVisibility.PUBLIC, description="Prompt redaction level."
    )
    seed: int | None = Field(default=None, description="Random seed for reproducibility.")
    params: dict[str, Any] = Field(
        default_factory=dict, description="Provider-specific parameters."
    )
    status: StepStatus = Field(default=StepStatus.PENDING, description="Current execution status.")
    inputs: list[Asset] = Field(default_factory=list, description="Input assets for this step.")
    assets: list[Asset] = Field(default_factory=list, description="Output assets from this step.")
    provider_payload: dict[str, Any] = Field(
        default_factory=dict, description="Raw provider response data."
    )
    retries: int = Field(default=0, description="Number of retry attempts.")
    cost_usd: float | None = Field(default=None, description="Estimated cost in USD.")
    error: str | None = Field(default=None, description="Error message if failed.")
    error_code: ProviderErrorCode | None = Field(
        default=None, description="Classified error code."
    )
    started_at: datetime | None = Field(default=None, description="Step start timestamp.")
    completed_at: datetime | None = Field(default=None, description="Step completion timestamp.")
    step_index: int | None = Field(
        default=None, description="Position in run (0-based). Set by RunBuilder."
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary metadata.")

    @property
    def retryable(self) -> bool:
        """Whether this step's error is transient and safe to retry."""
        return self.error_code is not None and self.error_code in RETRYABLE_ERROR_CODES

    def __repr__(self) -> str:
        return (
            f"Step(id={self.step_id[:8]}..., provider={self.provider!r}, "
            f"model={self.model!r}, status={self.status})"
        )
