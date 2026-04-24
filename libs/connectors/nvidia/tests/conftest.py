"""Shared test fixtures for NVIDIA providers."""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import MagicMock

# 1×1 transparent PNG — smallest valid PNG that exercises base64 decoding.
_PNG_1X1 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63000100000005000001e221bc330000000049454e44ae426082"
    )
).decode()

# Minimal MP4-like bytes — we don't actually test decoding, just that the
# provider writes bytes through unchanged.
_MP4_STUB = base64.b64encode(b"fake-mp4-bytes-for-test").decode()
_MP3_STUB = base64.b64encode(b"fake-mp3-bytes-for-test").decode()


def make_mock_http_client(
    *,
    submit_status: int = 200,
    submit_body: dict[str, Any] | None = None,
    submit_headers: dict[str, str] | None = None,
    poll_statuses: list[int] | None = None,
    poll_body: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a mock httpx.Client for NVIDIA providers.

    The default shape is a synchronous 200 with an inline PNG artifact — good
    enough for image and audio tests. Pass ``submit_status=202`` plus
    ``submit_headers={"NVCF-REQID": "..."}`` to exercise the async path.

    Args:
        submit_status: HTTP status returned by the initial POST.
        submit_body: JSON body returned by the initial POST. Defaults to a
            one-artifact image payload.
        submit_headers: Response headers (case-insensitive via httpx Headers).
        poll_statuses: Sequence of status codes returned by successive NVCF
            polls. ``[202, 200]`` = one "still running" tick, then done.
        poll_body: JSON body returned once polling completes. Defaults to the
            same shape as ``submit_body``.
    """
    client = MagicMock()

    if submit_body is None:
        submit_body = {"artifacts": [{"base64": _PNG_1X1, "mime_type": "image/png"}]}
    if poll_body is None:
        poll_body = submit_body

    submit_resp = _make_response(submit_status, submit_body, submit_headers)
    client.post.return_value = submit_resp

    statuses = poll_statuses or [200]
    responses = [_make_response(s, poll_body, {}) for s in statuses]
    # MagicMock consumes side_effect lists one entry per call and raises
    # StopIteration after the list is exhausted — use return_value for the
    # common single-response case so tests that retry a poll still work.
    if len(responses) > 1:
        client.get.side_effect = responses
    else:
        client.get.return_value = responses[0]

    return client


def _make_response(status: int, body: dict, headers: dict[str, str] | None) -> MagicMock:
    """Build a mocked httpx response with the right surface for our client code."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = str(body)
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    resp.headers = hdrs
    return resp
