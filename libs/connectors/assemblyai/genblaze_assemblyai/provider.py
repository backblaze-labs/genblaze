"""AssemblyAIProvider — adapter for the AssemblyAI speech-to-text API.

AssemblyAI is the inverse of every other genblaze connector: it *consumes* an
audio URL and *produces* a text transcript (plus word-level timing, speaker
labels, and optional audio-intelligence). It fits genblaze's primitives with
zero core changes — the transcript lands as a **TEXT ``Asset``** following the
``NvidiaChatProvider`` precedent (``url="text:{sha256}"``,
``media_type="text/plain"``, payload in ``metadata["text"]``, sha256 over the
text bytes), and word timings populate ``AudioMetadata.word_timings``.

**API style:** genuinely async — the SDK's ``Transcriber().submit()`` is
non-blocking (returns a queued ``Transcript`` immediately) and
``aai.Transcript.get_by_id(id)`` polls/fetches by id. So this is a
``BaseProvider`` (submit / poll / fetch_output), which gets adaptive polling,
progress events, ``resume()`` crash-recovery, and poll caching for free.

**Catalog architecture (genblaze-core 0.3.0):** AssemblyAI exposes no live
``GET /models`` catalog; the model set is small and stable. This connector
therefore declares ``DiscoverySupport.NONE`` and ships a single pattern-keyed
``ModelFamily`` for the current ``universal-*`` speech models
(``universal-3-pro`` / ``universal-2``) plus a permissive TEXT fallback so any
slug passes through. ``step.model`` is the AssemblyAI speech model and is sent
on the SDK's plural ``speech_models`` field — the live API has deprecated the
singular ``speech_model`` field (and the legacy ``best`` / ``nano`` aliases).

**Pricing:** AssemblyAI bills per minute of *input* audio. The SDK ships zero
hardcoded prices — register a recipe at runtime that reads
``step.provider_payload["audio_duration"]`` (seconds, captured during
``fetch_output``); see ``docs/reference/pricing-recipes.md`` ("AssemblyAI").

Docs: https://www.assemblyai.com/docs
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, WordTiming
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    BaseProvider,
    DiscoverySupport,
    ModelFamily,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
    validate_chain_input_url,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from ._errors import map_assemblyai_error

logger = logging.getLogger("genblaze.assemblyai")

# Terminal transcript statuses. AssemblyAI's lifecycle is
# queued → processing → completed | error.
_TERMINAL_STATUSES = frozenset({"completed", "error"})

# The AssemblyAI speech-model family covers the current ``universal-*`` line
# (``universal-3-pro``, ``universal-2``) — the only values the live API accepts
# on the ``speech_models`` field as of 2026-06. The legacy ``speech_model``
# tier aliases (``best`` / ``nano``) and the bare ``universal`` slug are now
# rejected server-side; they fall through to the permissive fallback (which
# still lets the request through — failures surface at submit, per NONE
# discovery). Output is always a TEXT transcript, so the spec modality is TEXT.
# spec_template.pricing MUST be None (pricing is user-registered).
_ASSEMBLYAI_SPEECH_FAMILY = ModelFamily(
    name="assemblyai-speech",
    pattern=re.compile(r"^universal"),
    spec_template=ModelSpec(model_id="*", modality=Modality.TEXT),
    description="AssemblyAI speech models — universal-3-pro / universal-2.",
    example_slugs=("universal-3-pro", "universal-2"),
)

_ASSEMBLYAI_FALLBACK = ModelSpec(model_id="*", modality=Modality.TEXT)


def _ms_to_s(value: Any) -> float | None:
    """Convert an AssemblyAI millisecond timestamp to seconds, or None."""
    if value is None:
        return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _status_str(transcript: Any) -> str:
    """Return the transcript status as a lowercase string.

    ``transcript.status`` is an ``aai.TranscriptStatus`` (a ``str`` enum) on
    the real SDK; tests may use a plain string. Normalize both to the bare
    value (``"completed"`` / ``"error"`` / …).
    """
    status = getattr(transcript, "status", None)
    value = getattr(status, "value", None)
    return str(value if value is not None else status).lower()


def _audio_ref_for_sdk(audio_url: str) -> str:
    """Shape a resolved audio URL into the form ``Transcriber.submit()`` wants.

    The AssemblyAI SDK treats any non-HTTP string as a *local file path* and
    opens it with ``open(ref, "rb")``. A ``file://`` URI — the form chained
    SyncProvider outputs take, built via ``Path.as_uri()``
    (``genblaze_core._utils.local_file_url``) — would be opened literally as
    the filename ``file:///tmp/a.wav`` and fail. Convert
    validated ``file://`` URIs to a real filesystem path (``url2pathname``
    handles percent-decoding and platform path conversion); pass ``https://``
    URLs through untouched so the SDK fetches them remotely.

    Assumes the URL already passed ``validate_chain_input_url`` (so the scheme
    is ``https`` or ``file`` and any ``file://`` netloc is empty/``localhost``).
    """
    parsed = urlparse(audio_url)
    if parsed.scheme == "file":
        return url2pathname(parsed.path)
    return audio_url


def _serialize_utterances(utterances: Any) -> list[dict[str, Any]]:
    """Flatten SDK utterance objects to canonical-JSON-safe dicts.

    Times are converted ms → seconds to match ``word_timings``. Kept minimal
    (speaker / text / start / end / confidence) — utterances are pass-through
    context in ``metadata``, not a first-class shape in v1.
    """
    out: list[dict[str, Any]] = []
    for u in utterances or []:
        out.append(
            {
                "speaker": getattr(u, "speaker", None),
                "text": getattr(u, "text", None),
                "start": _ms_to_s(getattr(u, "start", None)),
                "end": _ms_to_s(getattr(u, "end", None)),
                "confidence": getattr(u, "confidence", None),
            }
        )
    return out


class AssemblyAIProvider(BaseProvider):
    """Provider adapter for AssemblyAI speech-to-text transcription.

    Transcribes an audio URL into a hash-verified TEXT asset with word-level
    timings. The audio URL is resolved from (in priority order)
    ``step.inputs[0].url`` → ``step.params["audio_url"]`` → ``step.prompt`` and
    SSRF-validated via ``validate_chain_input_url`` before submission, so the
    same provider works both standalone and chained into a pipeline (e.g.
    generate audio → transcribe).

    ``step.model`` is the AssemblyAI speech model (``universal-3-pro`` /
    ``universal-2``) and is sent on the SDK's plural ``speech_models`` field;
    any other ``TranscriptionConfig`` kwarg (``speaker_labels``,
    ``language_code``, audio-intelligence flags, …) passes through
    ``step.params``.

    Args:
        api_key: AssemblyAI API key. Falls back to ``ASSEMBLYAI_API_KEY``.
        poll_interval: Base seconds between polls (the base class applies
            adaptive backoff on top).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
        retry_policy: Optional retry policy override.
        probe_cache_ttl: Per-instance probe-cache TTL (no-op for NONE discovery
            but accepted for API uniformity with other providers).
        probe_cache_max_entries: Per-instance probe-cache size cap.
    """

    name = "assemblyai"
    discovery_support = DiscoverySupport.NONE
    """AssemblyAI exposes no ``GET /models`` catalog — the model set is small
    and stable. Family-matched ``universal-*`` slugs (``universal-3-pro`` /
    ``universal-2``) preflight as ``OK_PROVISIONAL``; everything else resolves
    through the permissive fallback as ``UNKNOWN_PERMISSIVE``."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        return ModelRegistry(
            provider_families=(_ASSEMBLYAI_SPEECH_FAMILY,),
            fallback=_ASSEMBLYAI_FALLBACK,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        """AssemblyAI: audio in, TEXT transcript out."""
        return ProviderCapabilities(
            supported_modalities=[Modality.TEXT],
            supported_inputs=["audio"],
            accepts_chain_input=True,
            models=self._models.known(),
            output_formats=["text/plain"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        poll_interval: float = 3.0,
        models: ModelRegistry | None = None,
        retry_policy: RetryPolicy | None = None,
        probe_cache_ttl: float | None = None,
        probe_cache_max_entries: int | None = None,
    ):
        super().__init__(
            models=models,
            retry_policy=retry_policy,
            probe_cache_ttl=probe_cache_ttl,
            probe_cache_max_entries=probe_cache_max_entries,
        )
        self.poll_interval = poll_interval
        self._api_key = api_key
        self._client: Any = None

    def _get_client(self) -> Any:
        """Return the ``assemblyai`` module, configured with the API key.

        AssemblyAI's SDK is module-scoped: transcription goes through
        ``aai.Transcriber()`` / ``aai.Transcript.get_by_id()`` with the key set
        on ``aai.settings.api_key``. So the "client" is the module itself. The
        key is resolved here (the SDK does not auto-read ``ASSEMBLYAI_API_KEY``)
        and a missing key fails fast with ``AUTH_FAILURE`` rather than a
        deferred opaque 401.
        """
        if self._client is None:
            key = self._api_key or os.getenv("ASSEMBLYAI_API_KEY")
            if not key:
                raise ProviderError(
                    "No AssemblyAI API key found. Pass api_key=... or set ASSEMBLYAI_API_KEY.",
                    error_code=ProviderErrorCode.AUTH_FAILURE,
                )
            try:
                import assemblyai as aai
            except ImportError as exc:
                raise ProviderError(
                    "assemblyai package not installed. Run: pip install assemblyai"
                ) from exc
            aai.settings.api_key = key
            self._client = aai
        return self._client

    def normalize_params(
        self, params: dict[str, Any], modality: Modality | None = None
    ) -> dict[str, Any]:
        """Map standard names to AssemblyAI's native ``TranscriptionConfig`` keys.

        ``language`` → ``language_code`` (AssemblyAI native). Everything else
        passes through untouched. Idempotent via the ``if x in p and native
        not in p`` guard.
        """
        p = dict(params)
        if "language" in p and "language_code" not in p:
            p["language_code"] = p.pop("language")
        return p

    def _resolve_audio_url(self, step: Step) -> str:
        """Resolve + SSRF-validate the audio URL to transcribe.

        Priority: ``step.inputs[0].url`` (chained pipeline output) →
        ``step.params["audio_url"]`` → ``step.prompt`` (standalone use). The
        chosen URL is validated with ``validate_chain_input_url`` (https:// or
        file:// only) before it leaves the process.
        """
        if step.inputs and step.inputs[0].url:
            url = step.inputs[0].url
        elif step.params.get("audio_url"):
            url = str(step.params["audio_url"])
        elif step.prompt:
            url = step.prompt
        else:
            raise ProviderError(
                "AssemblyAI requires an audio URL via step.inputs[0], "
                "step.params['audio_url'], or step.prompt.",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        validate_chain_input_url(url)
        return url

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Submit a transcription job (non-blocking) and return its id."""
        aai = self._get_client()
        # Resolve + SSRF-validate, then shape for the SDK: a validated file://
        # chain input must reach Transcriber.submit() as a local filesystem
        # path, since the SDK open()s any non-HTTP string literally.
        audio_ref = _audio_ref_for_sdk(self._resolve_audio_url(step))
        try:
            cfg_kwargs = self.normalize_params(dict(step.params), step.modality)
            # audio_url is the submit() argument, not a TranscriptionConfig field.
            cfg_kwargs.pop("audio_url", None)
            # step.model is the speech model. The live API has deprecated the
            # singular ``speech_model`` field (and the legacy best/nano tier
            # aliases); the slug is sent on the plural ``speech_models`` list,
            # which currently accepts ``universal-3-pro`` / ``universal-2``.
            if step.model:
                cfg_kwargs["speech_models"] = [step.model]
            transcription_config = aai.TranscriptionConfig(**cfg_kwargs)
            transcriber = aai.Transcriber()
            transcript = transcriber.submit(audio_ref, config=transcription_config)
            return transcript.id
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"AssemblyAI submit failed: {exc}",
                error_code=map_assemblyai_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Return True once the transcript reaches a terminal status."""
        aai = self._get_client()
        try:
            transcript = aai.Transcript.get_by_id(prediction_id)
            if _status_str(transcript) in _TERMINAL_STATUSES:
                self._cache_poll_result(prediction_id, transcript)
                return True
            return False
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"AssemblyAI poll failed: {exc}",
                error_code=map_assemblyai_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Build the TEXT transcript asset from the completed transcript."""
        aai = self._get_client()
        try:
            transcript = self._get_cached_poll_result(prediction_id)
            if transcript is None:
                transcript = aai.Transcript.get_by_id(prediction_id)

            status = _status_str(transcript)
            if status == "error":
                err = getattr(transcript, "error", None) or "AssemblyAI transcription failed"
                raise ProviderError(str(err), error_code=map_assemblyai_error(err))
            if status != "completed":
                # fetch_output is only reached after poll() confirms a terminal
                # status, so this guards the off-happy-path / resume case rather
                # than silently emitting an empty transcript for a running job.
                raise ProviderError(
                    f"AssemblyAI transcript {prediction_id} is not complete (status={status!r}).",
                    error_code=ProviderErrorCode.SERVER_ERROR,
                )

            text = getattr(transcript, "text", None) or ""
            text_bytes = text.encode("utf-8")
            digest = hashlib.sha256(text_bytes).hexdigest()

            asset = Asset(
                url=f"text:{digest}",  # synthetic TEXT asset (NvidiaChatProvider precedent)
                media_type="text/plain",
                sha256=digest,
                size_bytes=len(text_bytes),
            )

            words = getattr(transcript, "words", None)
            if words:
                # AssemblyAI word start/end are in MILLISECONDS; WordTiming
                # expects seconds — divide by 1000.
                timings = [
                    WordTiming(
                        word=getattr(w, "text", "") or "",
                        start=_ms_to_s(getattr(w, "start", None)) or 0.0,
                        end=_ms_to_s(getattr(w, "end", None)) or 0.0,
                        confidence=getattr(w, "confidence", None),
                    )
                    for w in words
                ]
                asset.audio = AudioMetadata(word_timings=timings)

            asset.metadata["text"] = text
            language = getattr(transcript, "language_code", None)
            if language is not None:
                asset.metadata["language"] = language
            confidence = getattr(transcript, "confidence", None)
            if confidence is not None:
                asset.metadata["confidence"] = confidence
            utterances = getattr(transcript, "utterances", None)
            if utterances:
                asset.metadata["utterances"] = _serialize_utterances(utterances)

            step.assets = [asset]

            # Seconds of input audio; the pricing recipe reads this.
            audio_duration = getattr(transcript, "audio_duration", None)
            if audio_duration is not None:
                step.provider_payload["audio_duration"] = audio_duration
                asset.metadata["audio_duration"] = audio_duration

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"AssemblyAI fetch_output failed: {exc}",
                error_code=map_assemblyai_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
