"""Shared test fixtures for GMICloud providers."""

from __future__ import annotations

from unittest.mock import MagicMock


def make_mock_http_client(
    request_id: str = "req-abc123",
    outcome_key: str = "video_url",
    outcome_url: str = "https://gmicloud-output.com/result.mp4",
) -> MagicMock:
    """Build a mock httpx client that returns success for submit + poll.

    Args:
        request_id: The request ID returned by POST /requests.
        outcome_key: The key in the outcome dict (video_url, image_url, audio_url).
        outcome_url: The URL returned in the outcome.
    """
    client = MagicMock()

    submit_resp = MagicMock()
    submit_resp.status_code = 200
    submit_resp.json.return_value = {"request_id": request_id, "status": "queued"}
    client.post.return_value = submit_resp

    poll_resp = MagicMock()
    poll_resp.status_code = 200
    poll_resp.json.return_value = {
        "request_id": request_id,
        "status": "success",
        "outcome": {outcome_key: outcome_url},
    }
    client.get.return_value = poll_resp

    return client
