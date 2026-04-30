"""``Pipeline.ingest`` orchestration — non-generative bulk imports.

Plan 4 Phase 2 closes the "non-generative workflow is a second-class
citizen" gap: today, apps doing live ingest, UGC upload, archival,
DAM, or podcast hosting have to fabricate ``SyncProvider`` shims to
seed assets through a generation-shaped pipeline. The ingest factory
makes the import a first-class operation that produces a manifest
with full provenance for the *act of bringing the bytes in*, no
``Provider`` required.

The resulting :class:`PipelineResult` carries:

* one :class:`Step` per ingested asset, with
  ``step_type=StepType.INGEST``, ``provider=None`` (allowed because
  the validator on :class:`Step` permits null provider only for
  INGEST/IMPORT step types), ``model=source`` (e.g. ``"rss"``,
  ``"ugc-upload"``, ``"dam-bulk"``), and ``metadata={"source":
  source, **source_metadata}``;
* the asset itself in ``step.assets`` with its durable URL,
  ``sha256``, ``size_bytes`` populated by ``sink.put_asset``;
* a canonical-hashable :class:`Manifest` whose hash is stable
  across permuted input orders — the ingest factory sorts the input
  assets by ``asset_id`` before building steps so callers can pass
  the same set in any order and get a byte-identical manifest.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from genblaze_core.exceptions import GenblazeError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, RunStatus, StepStatus, StepType
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step
from genblaze_core.pipeline.result import PipelineResult

if TYPE_CHECKING:
    from genblaze_core.sinks.base import BaseSink

logger = logging.getLogger("genblaze.pipeline.ingest")


def _modality_from_media_type(media_type: str) -> Modality:
    """Infer :class:`Modality` from an MIME media type.

    Ingest steps don't *generate* anything, but every Step carries a
    modality field for downstream filtering / observability — pick
    the most accurate one by media-type prefix. Defaults to
    :class:`Modality.IMAGE` for anything unrecognized so the field is
    always populated.
    """
    if media_type.startswith("video/"):
        return Modality.VIDEO
    if media_type.startswith("audio/"):
        return Modality.AUDIO
    if media_type.startswith("text/"):
        return Modality.TEXT
    return Modality.IMAGE


def ingest_assets(
    assets: Sequence[Asset],
    *,
    source: str,
    source_metadata: dict[str, Any] | None = None,
    sink: BaseSink | None = None,
    name: str | None = None,
    tenant_id: str | None = None,
    step_type: StepType = StepType.INGEST,
) -> PipelineResult:
    """Ingest a batch of assets and produce a provenance-complete manifest.

    Args:
        assets: The assets to ingest. Each asset's URL points to source
            bytes (``file://`` allowlisted dirs, or SSRF-protected
            ``https://``); the sink rewrites ``url`` to its backend's
            durable URL after upload. **Must not be empty.**
        source: Free-form attribution string — ``"rss"``, ``"ugc-upload"``,
            ``"dam-bulk"``, ``"manual"``, etc. Recorded as the ``model``
            field on every produced Step *and* in ``step.metadata["source"]``
            for downstream filtering.
        source_metadata: Optional per-source attribution data
            (``feed_url``, ``uploader_id``, ``import_run_id``, etc.).
            Merged into every produced Step's ``metadata`` alongside
            ``"source"``.
        sink: Storage sink to write asset bytes to. When supplied, each
            asset is written via ``sink.put_asset`` with a derived
            ``manifest_uri`` so :meth:`BaseSink.read_manifest_for_asset`
            can later discover the manifest from any asset id. May be
            ``None`` for offline builds (manifest only, no upload).
        name: Optional human-readable run name.
        tenant_id: Optional tenant id for multi-tenant deployments.
        step_type: Either :class:`StepType.INGEST` (default — external
            source) or :class:`StepType.IMPORT` (cross-system transfer).
            Both are non-generative; the field is exposed so callers
            can disambiguate in observability.

    Returns:
        :class:`PipelineResult` with a populated :class:`Run` and
        canonical-hashable :class:`Manifest`.

    Raises:
        GenblazeError: when ``assets`` is empty or ``step_type`` is not
            a non-generative type.
    """
    if step_type not in (StepType.INGEST, StepType.IMPORT):
        raise GenblazeError(
            f"ingest_assets: step_type must be INGEST or IMPORT, got "
            f"{step_type.value!r} — generative step types must go through "
            "Pipeline.step() with a provider."
        )
    asset_list = list(assets)
    if not asset_list:
        raise GenblazeError(
            "ingest_assets: assets list is empty — pass at least one asset, "
            "or skip the call entirely."
        )

    # Sort by asset_id so the canonical hash is stable across permuted
    # input orders. The plan's hash-determinism gate ("same asset set
    # in different orders → byte-identical manifest") relies on this.
    sorted_assets = sorted(asset_list, key=lambda a: a.asset_id)

    metadata_template: dict[str, Any] = {"source": source}
    if source_metadata:
        # Caller's keys win on overlap with the source key — but we
        # always preserve the canonical "source" entry by re-adding it
        # after the merge to prevent accidental clobber.
        metadata_template.update(source_metadata)
        metadata_template["source"] = source

    # Build the run + steps WITHOUT touching the sink yet — gives us a
    # complete in-memory shape so we can compute the manifest_uri (a
    # function of run.run_id) before calling sink.put_asset.
    steps: list[Step] = []
    for asset in sorted_assets:
        step = Step(
            provider=None,
            model=source,
            step_type=step_type,
            modality=_modality_from_media_type(asset.media_type),
            status=StepStatus.SUCCEEDED,
            assets=[asset],
            metadata=dict(metadata_template),
        )
        steps.append(step)

    run = Run(
        name=name,
        tenant_id=tenant_id,
        status=RunStatus.COMPLETED,
        steps=steps,
    )

    # Pre-compute the manifest_uri so put_asset can write the
    # asset_id → manifest_uri sidecar atomically with the asset
    # upload. ``manifest_key_for`` and ``get_durable_url`` exposed on
    # storage sinks are pure functions of run config + run.run_id.
    manifest_uri = _derive_manifest_uri(sink, run)

    # Write asset bytes via the sink. Each call mutates the asset
    # in place: ``url`` → durable backend URL, ``sha256`` /
    # ``size_bytes`` populated.
    if sink is not None:
        for asset in sorted_assets:
            try:
                sink.put_asset(asset, manifest_uri=manifest_uri)
            except NotImplementedError:
                # Sink doesn't support standalone asset writes (e.g.
                # ParquetSink). Skip the asset upload but still emit
                # the manifest; the user explicitly chose this sink.
                logger.warning(
                    "Sink %s doesn't implement put_asset(); skipping asset "
                    "upload for %s. Manifest will reference the source URL.",
                    type(sink).__name__,
                    asset.asset_id,
                )

    manifest = Manifest(run=run)
    manifest.compute_hash()
    if manifest_uri is not None:
        manifest.manifest_uri = manifest_uri

    # Write the manifest itself if the sink supports run-level writes.
    if sink is not None:
        try:
            sink.write_run(run, manifest)
        except NotImplementedError:
            # Sink might be a one-off with put_asset only; the manifest
            # stays in memory on the result.
            pass

    return PipelineResult(run=run, manifest=manifest)


def _derive_manifest_uri(sink: BaseSink | None, run: Run) -> str | None:
    """Best-effort manifest URI prediction.

    Storage-backed sinks expose ``manifest_url_for(run)`` as a pure
    function of run + sink config. Non-storage sinks return None
    (they don't have a backing URL story).
    """
    if sink is None:
        return None
    manifest_url_for = getattr(sink, "manifest_url_for", None)
    if not callable(manifest_url_for):
        return None
    try:
        return manifest_url_for(run)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug(
            "Sink %s.manifest_url_for(run) raised %s; manifest_uri left None.",
            type(sink).__name__,
            exc,
        )
        return None


__all__ = ["ingest_assets"]
