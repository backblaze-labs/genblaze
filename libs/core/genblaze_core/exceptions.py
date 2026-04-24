"""Genblaze exception hierarchy."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genblaze_core.models.enums import ProviderErrorCode


class GenblazeError(Exception):
    """Base exception for all genblaze errors."""


class ProviderError(GenblazeError):
    """Raised when a provider operation fails.

    ``retry_after`` carries the server's ``Retry-After`` hint (seconds) when the
    connector parsed it from an HTTP response; the retry helper honors it over
    computed backoff, clamped to ``MAX_RETRY_AFTER_SEC``. ``attempts`` reflects
    how many tries were made before the terminal failure; populated by the
    retry helper when it exhausts its budget.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: ProviderErrorCode | None = None,
        retry_after: float | None = None,
        attempts: int = 1,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after
        self.attempts = attempts


class ManifestError(GenblazeError):
    """Raised when manifest creation/validation fails."""


class EmbeddingError(GenblazeError):
    """Raised when media embedding or extraction fails."""


class SinkError(GenblazeError):
    """Raised when a sink write operation fails."""


class PipelineTimeoutError(GenblazeError):
    """Raised when a pipeline exceeds its wall-clock timeout."""


class StorageError(GenblazeError):
    """Raised when an object storage operation fails."""


class WebhookError(GenblazeError):
    """Raised when a webhook delivery fails."""
