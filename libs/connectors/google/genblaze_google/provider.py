"""VeoProvider — adapter for Google Veo video generation.

Uses the google-genai SDK with the async operation-based workflow:
  client.models.generate_videos() → poll operation → download video

**Catalog architecture (genblaze-core 0.3.0):** the SDK ships the
pattern-keyed ``google-veo`` family (``^veo-``) instead of a
hardcoded slug list. New ``veo-N`` slugs inherit the param shape;
authoritative liveness comes from ``client.models.get(model=slug)``
via the family probe.

**Pricing**: per-second-by-model rates were dropped in 0.3.0. See
``docs/reference/pricing-recipes.md`` for the canonical Veo recipe.

Docs: https://ai.google.dev/gemini-api/docs/video
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from genblaze_core._utils import local_file_url
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.asset import Asset, AudioMetadata, Track, VideoMetadata
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers import (
    BaseProvider,
    DiscoverySupport,
    ModelRegistry,
    ModelSpec,
    ProviderCapabilities,
    RetryPolicy,
)
from genblaze_core.providers.retry import retry_after_from_response
from genblaze_core.runnable.config import RunnableConfig

from genblaze_google._client import GoogleClientMixin
from genblaze_google._errors import map_google_error
from genblaze_google._families import GOOGLE_VEO_FAMILY, GOOGLE_VEO_LEGACY_FAMILY

_FALLBACK = ModelSpec(model_id="*", modality=Modality.VIDEO)


class VeoProvider(GoogleClientMixin, BaseProvider):
    """Provider adapter for Google Veo video generation.

    Models match the ``google-veo`` family (``^veo-``). Current GA
    examples: ``veo-2.0-generate-001``, ``veo-3.0-generate-001``,
    ``veo-3.0-fast-generate-001``.

    Supports both Gemini API (``api_key``) and Vertex AI
    (``project``/``location``) auth.

    Args:
        api_key: Gemini API key. Falls back to GEMINI_API_KEY env var.
        project: GCP project ID for Vertex AI auth (mutually exclusive with api_key).
        location: GCP region for Vertex AI (default "us-central1").
        poll_interval: Seconds between operation polls (default 10).
        output_dir: Directory for locally-saved video files. Both auth modes
            materialize the generated video to a local ``file://`` asset —
            Vertex returns bytes inline; Gemini downloads them from the Files
            API (issue #263) — so ObjectStorageSink can upload to B2 without
            Google auth (default: system temp).
        models: Optional custom ``ModelRegistry`` — overrides the class default.
        retry_policy: Optional retry policy override.
        probe_cache_ttl: Per-instance probe-cache TTL.
        probe_cache_max_entries: Per-instance probe-cache size cap.
    """

    name = "google-veo"
    discovery_support = DiscoverySupport.PARTIAL
    """google-genai has no per-modality catalog endpoint that filters
    Veo cleanly. The family probe (``client.models.get``) is the
    authoritative liveness check; preflight surfaces dead slugs as
    ``NOT_FOUND`` before the operation submission."""

    @classmethod
    def create_registry(cls) -> ModelRegistry:
        # Order is load-bearing: legacy first, modern catch-all second.
        # ``ModelRegistry.match_family`` is first-match-wins, so a
        # ``veo-2.0-*`` slug must match ``GOOGLE_VEO_LEGACY_FAMILY``
        # (no audio) before falling through to ``GOOGLE_VEO_FAMILY``
        # (which carries ``extras["has_audio"]=True``).
        return ModelRegistry(
            provider_families=(GOOGLE_VEO_LEGACY_FAMILY, GOOGLE_VEO_FAMILY),
            fallback=_FALLBACK,
        )

    def get_capabilities(self) -> ProviderCapabilities:
        """Veo: video generation from text prompts with configurable resolution and duration."""
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["text"],
            max_duration=8.0,
            resolutions=["720p", "1080p", "4k"],
            models=self._models.known(),
            output_formats=["video/mp4"],
        )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        project: str | None = None,
        location: str = "us-central1",
        poll_interval: float = 10.0,
        output_dir: str | Path | None = None,
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
        self._project = project
        self._location = location
        self._output_dir = Path(output_dir) if output_dir else None
        self._client: Any = None

    def normalize_params(self, params: dict, modality: Any = None) -> dict:
        """Map standard params to Veo-native names.

        Kept for backward compatibility with direct callers; ``prepare_payload``
        also performs the alias via the model spec.
        """
        p = dict(params)
        if "duration" in p and "duration_seconds" not in p:
            p["duration_seconds"] = p.pop("duration")
        return p

    def _build_config(self, payload: dict[str, Any], step: Step) -> Any:
        """Build a GenerateVideosConfig from the prepared payload."""
        from google.genai import types

        config_kwargs: dict = {}

        if "aspect_ratio" in payload:
            config_kwargs["aspect_ratio"] = payload["aspect_ratio"]
        if "resolution" in payload:
            config_kwargs["resolution"] = payload["resolution"]
        if "duration_seconds" in payload:
            config_kwargs["duration_seconds"] = payload["duration_seconds"]
        if "person_generation" in payload:
            config_kwargs["person_generation"] = payload["person_generation"]
        if "number_of_videos" in payload:
            config_kwargs["number_of_videos"] = int(payload["number_of_videos"])
        if "enhance_prompt" in payload:
            config_kwargs["enhance_prompt"] = bool(payload["enhance_prompt"])
        if step.seed is not None:
            config_kwargs["seed"] = step.seed

        return types.GenerateVideosConfig(**config_kwargs) if config_kwargs else None

    @staticmethod
    def _as_operation(prediction_id: Any) -> Any:
        """Wrap a bare operation-name string for ``client.operations.get()``.

        ``submit()`` returns ``operation.name`` (a plain ``str``, so
        ``resume()`` works without any in-memory state), but google-genai's
        ``operations.get()`` reads ``.name`` off its argument — it expects an
        operation object, not a string, and raises ``AttributeError`` on a
        bare str (issue #136). Real operation objects (e.g. a cached poll
        result) are passed through unchanged.
        """
        if isinstance(prediction_id, str):
            from google.genai import types

            # ``name`` is a real field (inherited from the ``Operation``
            # mixin) and works at runtime; the SDK's type stubs just don't
            # surface it on this subclass's synthesized __init__.
            return types.GenerateVideosOperation(name=prediction_id)  # type: ignore[call-arg]
        return prediction_id

    def _video_output_path(self, step: Step, i: int) -> Path:
        """Pick the local path for the ``i``-th generated video.

        Shared by both auth modes so the Vertex (inline-bytes) and Gemini
        (Files API download) paths can't drift on where the file lands
        (issue #263). When ``output_dir`` is set, index by loop position
        (matches ImagenProvider) so ``number_of_videos > 1`` doesn't collide
        on one path; otherwise fall back to a unique tempfile.

        ``step.step_id`` only *defaults* to a UUID (``Step.step_id`` is a
        plain ``str``) — a ``Step`` built or deserialized with an explicit
        ``step_id`` could otherwise carry ``../`` traversal or an absolute
        path straight into the filename (issue #284). Parse it as a UUID and
        build the name from the canonical parsed value, then verify the
        result still resolves directly under ``output_dir``, so a crafted
        ``step_id`` can't escape the configured directory.
        """
        if self._output_dir:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            try:
                parsed_id = uuid.UUID(step.step_id)
            except (ValueError, AttributeError, TypeError) as exc:
                raise ProviderError(
                    f"Invalid step_id for video output path: {step.step_id!r}",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                ) from exc
            candidate = self._output_dir / f"{parsed_id}_{i}.mp4"
            resolved_dir = self._output_dir.resolve()
            if candidate.resolve().parent != resolved_dir:
                raise ProviderError(
                    "Resolved video output path escapes output_dir",
                    error_code=ProviderErrorCode.INVALID_INPUT,
                )
            return candidate
        fd, tmp = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        return Path(tmp)

    @staticmethod
    def _write_new_video_file(path: Path, data: bytes, *, exclusive: bool = True) -> None:
        """Write ``data`` to ``path`` without following symlinks or overwriting.

        Used instead of ``Path.write_bytes`` for video output: that call
        follows existing symlinks and silently overwrites the resolved
        target, which combined with an attacker-controlled ``step_id`` is the
        cross-job overwrite in issue #284. ``O_EXCL`` refuses a pre-existing
        name (file or symlink); ``O_NOFOLLOW`` refuses to traverse a symlink.

        ``exclusive=False`` is for the tempfile-fallback path only: ``mkstemp``
        has already atomically created that (exclusively-ours) file, so
        ``O_EXCL`` would reject the very path it just made.

        ``O_NOFOLLOW`` is POSIX-only (unavailable on Windows) — read via
        ``getattr`` so this degrades to "no symlink guard on this platform"
        instead of an unhandled ``AttributeError`` that would break video
        output entirely there.
        """
        flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        flags |= os.O_CREAT | os.O_EXCL if exclusive else 0
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise ProviderError(
                f"Refusing to write video output to {path}: {exc}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            ) from exc
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def submit(self, step: Step, config: RunnableConfig | None = None) -> Any:
        """Start a video generation operation."""
        client = self._get_client()
        try:
            payload = self.prepare_payload(step)
            gen_config = self._build_config(payload, step)
            kwargs: dict = {
                "model": step.model,
                "prompt": payload.get("prompt", step.prompt or ""),
            }
            if gen_config is not None:
                kwargs["config"] = gen_config

            operation = client.models.generate_videos(**kwargs)
            # Return the provider-native operation name for resume() compatibility
            return operation.name
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Veo submit failed: {exc}",
                error_code=map_google_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def poll(self, prediction_id: Any, config: RunnableConfig | None = None) -> bool:
        """Check if the video generation operation is done."""
        client = self._get_client()
        try:
            operation = client.operations.get(self._as_operation(prediction_id))
            if operation.done:
                self._cache_poll_result(prediction_id, operation)
                return True
            return False
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Veo poll failed: {exc}",
                error_code=map_google_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc

    def fetch_output(self, prediction_id: Any, step: Step) -> Step:
        """Download generated video(s) and attach asset URLs."""
        client = self._get_client()
        try:
            # Use cached poll result if available, otherwise fetch fresh
            operation = self._get_cached_poll_result(prediction_id)
            if operation is None:
                operation = client.operations.get(self._as_operation(prediction_id))

            # Store provider metadata
            step.provider_payload = {
                "google": {
                    "operation_name": getattr(operation, "name", None),
                    "model": step.model,
                }
            }

            # Check for errors in the operation result
            if hasattr(operation, "error") and operation.error:
                raise ProviderError(
                    str(operation.error),
                    error_code=ProviderErrorCode.UNKNOWN,
                )

            response = operation.response
            if response is None or not hasattr(response, "generated_videos"):
                raise ProviderError("No video generated in response")

            # Audio capability comes from the family's typed ``extras``,
            # not a runtime string check on the slug. Veo 2 routes to
            # ``GOOGLE_VEO_LEGACY_FAMILY`` (no ``has_audio``); Veo 3+
            # routes to ``GOOGLE_VEO_FAMILY`` (``extras["has_audio"]=True``).
            # Future ``veo-N`` slugs inherit modern's audio capability
            # automatically — no provider release required.
            spec = self._models.get(step.model)
            has_audio = bool(spec.extras.get("has_audio"))

            for i, gv in enumerate(response.generated_videos):
                video = gv.video
                video_bytes = getattr(video, "video_bytes", None)
                video_uri: str | None = None
                if video_bytes:
                    # Vertex AI mode: video comes back inline — there's no
                    # Files API on Vertex, so client.files.download() raises
                    # ValueError there (issue #136). Save locally and expose
                    # a file:// asset, matching the local-output convention
                    # used by ImagenProvider / DecartVideoProvider.
                    out_path = self._video_output_path(step, i)
                    self._write_new_video_file(
                        out_path, video_bytes, exclusive=bool(self._output_dir)
                    )
                    video_uri = local_file_url(out_path.resolve())
                elif getattr(video, "uri", None):
                    # Gemini Developer API mode: the asset lives behind a
                    # credentialed Files API URI that ObjectStorageSink/B2
                    # cannot fetch unauthenticated (issue #263). Download the
                    # bytes and write them to a local file, exposing the same
                    # file:// asset the Vertex path produces so the sink can
                    # upload to B2 without any Google auth.
                    #
                    # Use the byte-returning download form (no ``destination=``):
                    # ``download(file=video)`` both returns the bytes and sets
                    # ``video.video_bytes`` as a side effect. The streaming
                    # ``destination=`` argument was only added in google-genai
                    # 2.21.0, but this connector supports ``google-genai>=1.0``
                    # — passing it would ``TypeError`` on every earlier release.
                    out_path = self._video_output_path(step, i)
                    video_bytes = client.files.download(file=video) or getattr(
                        video, "video_bytes", None
                    )
                    if not video_bytes:
                        raise ProviderError("Veo Gemini download returned no video bytes")
                    self._write_new_video_file(
                        out_path, video_bytes, exclusive=bool(self._output_dir)
                    )
                    video_uri = local_file_url(out_path.resolve())

                if video_uri:
                    vm_kwargs: dict[str, Any] = {"has_audio": has_audio}
                    if "resolution" in step.params:
                        vm_kwargs["resolution"] = step.params["resolution"]
                    asset = Asset(url=video_uri, media_type="video/mp4")
                    asset.video = VideoMetadata(**vm_kwargs)
                    # Multi-track metadata for audio-capable variants
                    # (video + generated audio)
                    if has_audio:
                        asset.tracks = [
                            Track(kind="video", codec="h264"),
                            Track(kind="audio", codec="aac", label="generated-audio"),
                        ]
                        asset.audio = AudioMetadata(codec="aac")
                    step.assets.append(asset)
                else:
                    raise ProviderError("Veo response missing both video_bytes and video URI")

            self._apply_registry_pricing(step)
            return step
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Veo fetch_output failed: {exc}",
                error_code=map_google_error(exc),
                retry_after=retry_after_from_response(exc),
            ) from exc
