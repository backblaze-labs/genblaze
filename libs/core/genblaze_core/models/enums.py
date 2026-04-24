"""Enumerations for genblaze models."""

from enum import StrEnum


class Modality(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"


class StepStatus(StrEnum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PromptVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    REDACTED = "redacted"
    ENCRYPTED = "encrypted"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepType(StrEnum):
    GENERATE = "generate"
    UPSCALE = "upscale"
    TRANSCODE = "transcode"
    MIX = "mix"
    EDIT = "edit"  # extend, inpaint, outpaint, style-transfer
    CUSTOM = "custom"


class ProviderErrorCode(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTH_FAILURE = "auth_failure"
    INVALID_INPUT = "invalid_input"
    MODEL_ERROR = "model_error"
    SERVER_ERROR = "server_error"
    # Provider refused the request on safety / content-policy grounds.
    # Deterministic given the same prompt — never retryable.
    CONTENT_POLICY = "content_policy"
    UNKNOWN = "unknown"


# Error codes that are safe to retry (transient failures)
RETRYABLE_ERROR_CODES: frozenset[ProviderErrorCode] = frozenset(
    {ProviderErrorCode.TIMEOUT, ProviderErrorCode.RATE_LIMIT, ProviderErrorCode.SERVER_ERROR}
)
