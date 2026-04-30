"""Step model — a single generation step within a run."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

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

# Step types that are allowed to have ``provider=None`` — non-generative
# operations where there is no upstream service to attribute. Generative
# step types (everything else) MUST have a provider.
_PROVIDERLESS_STEP_TYPES = frozenset({StepType.INGEST, StepType.IMPORT})


class Step(BaseModel):
    """A single generation step within a run."""

    step_id: str = Field(default_factory=new_id, description="Unique step identifier (UUID).")
    run_id: str | None = Field(default=None, description="Parent run ID. Set by RunBuilder.")
    provider: str | None = Field(
        default=None,
        description=(
            "Provider name (e.g. 'replicate'). Required for generative step "
            "types; may be ``None`` only when ``step_type`` is ``INGEST`` or "
            "``IMPORT`` (non-generative — no upstream service to attribute)."
        ),
    )
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

    @model_validator(mode="after")
    def _validate_provider_required_for_generative_steps(self) -> Step:
        """``provider=None`` is only allowed for non-generative step types.

        ``StepType.INGEST`` / ``IMPORT`` represent the act of bringing
        existing bytes into the system (RSS feed pulls, UGC uploads,
        DAM bulk imports, cross-tenancy migrations) — there is no
        upstream service to attribute, so provider is genuinely null.
        Every other step type produces new content via a provider and
        MUST have one set.
        """
        if self.provider is None and self.step_type not in _PROVIDERLESS_STEP_TYPES:
            raise ValueError(
                f"Step.provider is required when step_type={self.step_type.value!r}; "
                f"only {sorted(t.value for t in _PROVIDERLESS_STEP_TYPES)} step types "
                "may have provider=None."
            )
        return self

    @property
    def retryable(self) -> bool:
        """Whether this step's error is transient and safe to retry."""
        return self.error_code is not None and self.error_code in RETRYABLE_ERROR_CODES

    def __repr__(self) -> str:
        return (
            f"Step(id={self.step_id[:8]}..., provider={self.provider!r}, "
            f"model={self.model!r}, status={self.status})"
        )
