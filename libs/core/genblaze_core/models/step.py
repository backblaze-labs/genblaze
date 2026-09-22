"""Step model — a single generation step within a run."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from genblaze_core._utils import new_id, sanitize_error
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

# Metadata key used to surface provider prediction/job ids to progress and
# streaming consumers. The value is observability data, not retry authority.
UPSTREAM_ID_KEY = "upstream_id"

# Upstream ids are opaque provider job ids; the cap bounds what a hostile or
# buggy provider can push into every manifest.
_MAX_UPSTREAM_ID_LENGTH = 256


class StepAttempt(BaseModel):
    """A failed provider invocation superseded by a later attempt of the same step.

    Recorded when ``fallback_models`` moves a step from one model to the next,
    so the manifest keeps the provenance and cost of work the final Step
    replaced. Deliberately a narrow projection of Step: no prompt/params/inputs
    (identical to the final Step's) and no ``provider_payload`` (raw, unbounded,
    and the likeliest place for credentials to leak).

    Only ``model``, ``provider`` and ``error_code`` enter the manifest's
    canonical hash; the remaining fields are operational, like their Step
    counterparts. Provider-level retries inside one attempt are counted in
    ``retries``, not listed as separate attempts.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str | None = Field(
        default=None,
        description="Step id this attempt ran under; correlates with its tracer and "
        "progress events.",
    )
    model: str = Field(description="Model identifier this attempt ran against.")
    provider: str | None = Field(default=None, description="Provider name.")
    error: str | None = Field(default=None, description="Sanitized error message.")
    error_code: ProviderErrorCode | None = Field(
        default=None, description="Normalized error code."
    )
    upstream_id: str | None = Field(
        default=None,
        max_length=_MAX_UPSTREAM_ID_LENGTH,
        description="Provider prediction/job id, when the provider accepted the job "
        "before failing. None means it failed before submit returned.",
    )
    cost_usd: float | None = Field(
        default=None,
        ge=0,
        description="Cost the provider reported for this attempt. None means unknown, "
        "not free — a failed job may still have been billed. Usually None: "
        "providers price successful outputs only.",
    )
    retries: int = Field(
        default=0, ge=0, description="Provider-level retries within this attempt."
    )
    started_at: datetime | None = Field(default=None, description="Attempt start timestamp.")
    completed_at: datetime | None = Field(
        default=None, description="Attempt completion timestamp."
    )

    @classmethod
    def from_step(cls, step: Step) -> StepAttempt:
        """Project a failed Step onto the attempt record.

        The error is sanitized here rather than trusted from the caller: this
        record is written to the manifest verbatim. Out-of-contract provider
        values are normalized rather than raised — a validation error here
        would abort the fallback chain the record exists to describe.
        """
        upstream_id = step.metadata.get(UPSTREAM_ID_KEY)
        cost = step.cost_usd
        return cls(
            step_id=step.step_id,
            model=step.model,
            provider=step.provider,
            error=sanitize_error(step.error) if step.error else step.error,
            error_code=step.error_code,
            upstream_id=str(upstream_id)[:_MAX_UPSTREAM_ID_LENGTH] if upstream_id else None,
            cost_usd=cost if cost is None or (math.isfinite(cost) and cost >= 0) else None,
            retries=max(step.retries, 0),
            started_at=step.started_at,
            completed_at=step.completed_at,
        )


class Step(BaseModel):
    """A single generation step within a run."""

    # Reject unrecognized constructor kwargs instead of silently discarding
    # them (Pydantic v2 default is extra="ignore"). Provider-specific keys
    # belong in ``params={...}``, not top-level — see issue #133.
    model_config = ConfigDict(extra="forbid")

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
    failed_attempts: list[StepAttempt] = Field(
        default_factory=list,
        description=(
            "Earlier failed attempts this step superseded (fallback chain), oldest "
            "first. The step itself is the final attempt. Omitted from "
            "serialization when empty."
        ),
    )

    # No return annotation on purpose: pydantic derives the serialization JSON
    # schema from it, and any annotation (dict/Any) would collapse Step's
    # schema to an opaque object for consumers such as FastAPI response models.
    @model_serializer(mode="wrap")
    def _omit_empty_failed_attempts(self, handler: SerializerFunctionWrapHandler):  # type: ignore[no-untyped-def]
        """Drop ``failed_attempts`` from the dump when empty.

        Keeps the canonical hash of attempt-free steps byte-identical to
        manifests written before the field existed, and keeps those manifests
        readable by older releases whose Step rejects unknown keys.
        """
        data: dict[str, Any] = handler(self)
        if not self.failed_attempts:
            data.pop("failed_attempts", None)
        return data

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
