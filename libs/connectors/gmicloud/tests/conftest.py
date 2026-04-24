"""Shared test fixtures for GMICloud providers."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


def make_mock_http_client(
    request_id: str = "req-abc123",
    outcome_url: str = "https://gmicloud-output.com/result.mp4",
    outcome_urls: list[str] | None = None,
    outcome_key: str | None = None,
    extra_outcome: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a mock httpx client that returns success for submit + poll.

    The default shape matches the live GMICloud envelope:
    ``outcome.media_urls[*].url``. Pass ``outcome_urls`` to emit multiple URLs
    in the envelope (exercises the multi-image path). Pass ``outcome_key`` to
    exercise the legacy flat-key fallback.

    Args:
        request_id: The request ID returned by POST /requests.
        outcome_url: Single URL returned in the outcome (ignored when
            ``outcome_urls`` is set).
        outcome_urls: Multiple URLs returned in the envelope. Enables multi-
            image / multi-output testing.
        outcome_key: If set, emits ``{outcome_key: outcome_url}`` instead of the
            current ``media_urls`` shape — used to test legacy compatibility.
        extra_outcome: Extra keys merged into the outcome dict (e.g.
            ``{"thumbnail_image_url": "..."}`` for image fallback tests).
    """
    client = MagicMock()

    submit_resp = MagicMock()
    submit_resp.status_code = 200
    submit_resp.json.return_value = {"request_id": request_id, "status": "queued"}
    client.post.return_value = submit_resp

    if outcome_key is not None:
        outcome: dict[str, Any] = {outcome_key: outcome_url}
    elif outcome_urls is not None:
        outcome = {"media_urls": [{"url": u} for u in outcome_urls]}
    else:
        outcome = {"media_urls": [{"url": outcome_url}]}
    if extra_outcome:
        outcome.update(extra_outcome)

    poll_resp = MagicMock()
    poll_resp.status_code = 200
    poll_resp.json.return_value = {
        "request_id": request_id,
        "status": "success",
        "outcome": outcome,
    }
    client.get.return_value = poll_resp

    return client
