"""close() releases the lazily built SDK client (issue #10)."""

from __future__ import annotations

import pytest
from genblaze_google import GeminiImageProvider, ImagenProvider, VeoProvider


class _FakeClient:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize(
    "provider_cls",
    [
        ImagenProvider,
        VeoProvider,
        GeminiImageProvider,
    ],
)
def test_close_releases_client_once_and_is_idempotent(provider_cls):
    provider = provider_cls()
    provider.close()  # never built: nothing to release
    client = _FakeClient()
    provider._client = client
    with provider:
        pass
    assert client.closed == 1
    assert provider._client is None
    provider.close()
    assert client.closed == 1
