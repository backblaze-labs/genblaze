"""Genblaze exception hierarchy."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genblaze_core.models.enums import ProviderErrorCode


class GenblazeError(Exception):
    """Base exception for all genblaze errors."""


class ProviderError(GenblazeError):
    """Raised when a provider operation fails."""

    def __init__(self, message: str, *, error_code: ProviderErrorCode | None = None):
        super().__init__(message)
        self.error_code = error_code


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
