"""Shared lazy ``google.genai.Client`` construction — Veo / Imagen / Gemini-image.

All three providers support the same two auth modes (Gemini API key or
Vertex AI ``project``/``location``) and the same family-probe forwarding
contract (``_invoke_family_probe`` hands the lazily-built client to
whichever ``FamilyProbe`` the matched ``ModelFamily`` carries). Before this
was extracted, ``VeoProvider`` and ``ImagenProvider`` carried byte-identical
copies of both methods; ``GeminiImageProvider`` would have made it a third.
"""

from __future__ import annotations

from typing import Any

from genblaze_core.exceptions import ProviderError
from genblaze_core.providers import LiveProbeResult


class GoogleClientMixin:
    """Lazy genai-client builder shared by the Google connector's providers.

    Expects the including class to set ``self._api_key``, ``self._project``,
    ``self._location``, and ``self._client = None`` in its own ``__init__``
    — this mixin supplies the lazy-construction and probe-forwarding
    *behavior* only, not the state, so each provider keeps its own
    constructor signature and docstring.
    """

    _api_key: str | None
    _project: str | None
    _location: str
    _client: Any

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise ProviderError(
                    "google-genai package not installed. Run: pip install google-genai"
                ) from exc
            if self._project:
                self._client = genai.Client(
                    vertexai=True, project=self._project, location=self._location
                )
            else:
                kwargs: dict[str, Any] = {}
                if self._api_key:
                    kwargs["api_key"] = self._api_key
                self._client = genai.Client(**kwargs)
        return self._client

    def _invoke_family_probe(self, probe: Any, model_id: str) -> LiveProbeResult:
        """Forward the family probe with this provider's lazy genai client."""
        return probe(model_id, client=self._get_client())
