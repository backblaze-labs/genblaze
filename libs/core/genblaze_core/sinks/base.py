"""Base sink ABC."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from genblaze_core.models.asset import Asset
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step


class BaseSink(ABC):
    """Abstract base for event/manifest sinks."""

    @abstractmethod
    def write_run(self, run: Run, manifest: Manifest) -> None:
        """Persist a completed run and its manifest."""
        ...

    def on_step_complete(  # noqa: B027 — intentionally empty default; sinks opt in.
        self,
        step: Step,
        *,
        run_id: str,
        tenant_id: str | None,
        date_str: str,
    ) -> None:
        """Pipeline hook — called after each step finishes execution.

        Default is a no-op. Sinks that support eager asset transfer
        (overlapping upload with subsequent step generation) override
        this to submit transfers to a background pool. The pipeline
        calls ``write_run`` at the end regardless; eager sinks simply
        have less work to do in that final call.

        Called for every completed step, including failed ones — sinks
        decide what (if anything) to do based on ``step.status``.
        """

    # ------------------------------------------------------------------
    # Standalone asset writes (Plan 4 Phase 1).
    #
    # ``put_asset`` is for non-generative workflows (DAM, archival,
    # podcast hosting, UGC ingest) that have an :class:`Asset` to
    # persist *without* a surrounding ``Run`` / ``Pipeline`` wrapper.
    # Default impls raise ``NotImplementedError`` so non-storage-backed
    # sinks (e.g. :class:`ParquetSink`) keep working without changes.
    # ------------------------------------------------------------------

    def put_asset(
        self,
        asset: Asset,
        *,
        manifest_uri: str | None = None,
    ) -> Asset:
        """Write a single asset's bytes to the sink and return it updated.

        The asset is **mutated in place**: ``url`` is rewritten to the
        durable URL on the sink's backend, and ``sha256`` / ``size_bytes``
        / ``media_type`` are populated when missing. The same object is
        returned for fluent-style use.

        ``manifest_uri`` is an optional pointer to a manifest that
        references this asset. When supplied, the sink may write a
        reverse-lookup index entry so :meth:`read_manifest_for_asset`
        can find the manifest later.

        Args:
            asset: An :class:`Asset` whose ``url`` points to the
                source bytes. Supports ``file://`` (allowlisted dirs
                only) and ``https://`` (SSRF-protected). The URL is
                **rewritten** to the sink backend's durable URL after
                upload.
            manifest_uri: Optional manifest pointer to record for
                reverse lookup.

        Returns:
            The same ``asset`` instance, mutated.

        Raises:
            NotImplementedError: by default. Storage-backed sinks
                override.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement put_asset()")

    def put_assets(
        self,
        assets: Sequence[Asset],
        *,
        manifest_uri: str | None = None,
    ) -> list[Asset]:
        """Bulk variant of :meth:`put_asset`.

        Storage-backed sinks parallelize the per-asset writes via an
        internal thread pool; the order of returned assets matches
        the input order regardless of completion order.

        Default impl raises :class:`NotImplementedError`.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement put_assets()")

    def read_manifest_for_asset(self, asset_id: str) -> Manifest | None:
        """Reverse-lookup: given an ``asset_id``, return the manifest
        that references it (if known to the sink).

        Returns ``None`` when no index entry exists for ``asset_id``.
        Storage-backed sinks maintain the index by sidecar files
        written during :meth:`put_asset` calls that supplied
        ``manifest_uri=``. Manifests for assets put without
        ``manifest_uri=`` are not discoverable via this method.

        Default impl raises :class:`NotImplementedError`.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement read_manifest_for_asset()"
        )

    @abstractmethod
    def close(self) -> None:
        """Release any held resources."""
        ...
