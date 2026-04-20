"""Shared base for all GMICloud media providers (video, image, audio).

Owns auth, HTTP client lifecycle, and the common poll() implementation
since all modalities use the same async request queue API.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.providers.base import BaseProvider, SubmitResult
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_gmicloud_error

_BASE_URL = "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey"

_TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled"})


class GMICloudBase(BaseProvider):
    """Base class for GMICloud providers — handles auth, HTTP client, and polling.

    All GMICloud media APIs share the same request queue, auth, and poll
    lifecycle. Subclasses implement ``get_capabilities()``, ``submit()``,
    and ``fetch_output()`` for their specific modality.

    Args:
        api_key: GMICloud API key. Falls back to GMI_API_KEY env var.
        poll_interval: Seconds between request status polls (default 5).
        http_timeout: HTTP request timeout in seconds (default 120).
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        poll_interval: float = 5.0,
        http_timeout: float = 120.0,
    ):
        super().__init__()
        self.poll_interval = poll_interval
        self._api_key: str | None = api_key or os.environ.get("GMI_API_KEY")
        self._http_timeout = http_timeout
        self._http_client: httpx.Client | None = None

    def _get_http_client(self) -> httpx.Client:
        """Lazy-create httpx client with API key Bearer auth."""
        if self._http_client is None:
            if not self._api_key:
                raise ProviderError(
                    "No API key found. Set GMI_API_KEY env var or pass api_key=.",
                    error_code=ProviderErrorCode.AUTH_FAILURE,
                )
            self._http_client = httpx.Client(
                base_url=_BASE_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._http_timeout,
            )
        return self._http_client

    def close(self) -> None:
        """Close the HTTP client and release connection pool resources."""
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def _submit_request(self, model: str, payload: dict) -> SubmitResult:
        """POST a generation request and return a SubmitResult."""
        client = self._get_http_client()
        resp = client.post("/requests", json={"model": model, "payload": payload})
        if resp.status_code >= 400:
            raise ProviderError(
                f"GMICloud submit failed ({resp.status_code}): {resp.text}",
                error_code=map_gmicloud_error(Exception(resp.text), resp.status_code),
            )
        data = resp.json()
        request_id = data.get("request_id") or data.get("id")
        return SubmitResult(prediction_id=request_id, estimated_seconds=30.0)

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check if a GMICloud request is complete (shared across all modalities)."""
        try:
            client = self._get_http_client()
            resp = client.get(f"/requests/{prediction_id}")
            if resp.status_code >= 400:
                raise ProviderError(
                    f"GMICloud poll failed ({resp.status_code}): {resp.text}",
                    error_code=map_gmicloud_error(Exception(resp.text), resp.status_code),
                )
            detail = resp.json()
            if detail.get("status", "") in _TERMINAL_STATUSES:
                self._cache_poll_result(prediction_id, detail)
                return True
            return False
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"GMICloud poll failed: {exc}",
                error_code=map_gmicloud_error(exc),
            ) from exc

    def _fetch_detail(self, prediction_id: Any) -> dict:
        """Retrieve cached poll result, or re-fetch with error checking."""
        detail = self._get_cached_poll_result(prediction_id)
        if detail is not None:
            return detail
        client = self._get_http_client()
        resp = client.get(f"/requests/{prediction_id}")
        if resp.status_code >= 400:
            raise ProviderError(
                f"GMICloud fetch failed ({resp.status_code}): {resp.text}",
                error_code=map_gmicloud_error(Exception(resp.text), resp.status_code),
            )
        return resp.json()
