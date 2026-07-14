"""Tests for ``Pipeline.ingest`` — non-generative bulk imports.

Coverage by use case (matches the plan's spec):

* RSS feed source (``source="rss"`` + ``source_metadata={"feed_url": …}``)
* UGC upload (``source="ugc-upload"`` + per-uploader metadata)
* DAM bulk import (``source="dam-bulk"``)
* Manifest re-verifies after canonical-hash compute
* **Canonical-hash determinism** across permuted asset orders within
  the same Run (the plan's critical correctness gate)
* Step.provider validator: None forbidden for non-INGEST/IMPORT step
  types
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest
from genblaze_core.exceptions import GenblazeError
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, RunStatus, StepStatus, StepType
from genblaze_core.models.step import Step
from genblaze_core.pipeline.ingest import _modality_from_media_type, ingest_assets
from genblaze_core.pipeline.pipeline import Pipeline


def _ingestable_asset(
    *,
    asset_id: str | None = None,
    url: str = "https://cdn.example.com/episode.mp3",
    media_type: str = "audio/mp3",
) -> Asset:
    """Build an Asset suitable for ingestion (no sha256 / size yet —
    those get populated by the sink during put_asset)."""
    if asset_id is not None:
        return Asset(asset_id=asset_id, url=url, media_type=media_type)
    return Asset(url=url, media_type=media_type)


def _stub_sink_that_records_calls() -> MagicMock:
    """A sink mock with the put_asset / write_run / manifest_url_for
    surface the ingest factory expects. Each method records its call
    args for assertion."""

    def _put_asset(asset: Asset, **kwargs) -> Asset:
        asset.sha256 = "0" * 64
        asset.size_bytes = 1
        return asset

    sink = MagicMock()
    sink.put_asset = MagicMock(side_effect=_put_asset)
    sink.write_run = MagicMock()
    sink.manifest_url_for = MagicMock(return_value="https://mem/run/manifest.json")
    return sink


def _stub_sink_that_assigns_content_derived_hashes() -> MagicMock:
    """A sink mock that assigns a DISTINCT sha256 per asset, derived from
    its (pre-upload) source URL — unlike ``_stub_sink_that_records_calls``,
    which hardcodes the same sha256 for every asset and therefore can't
    distinguish assets by content. Mimics a real content-addressed sink
    that only learns each asset's hash once it fetches/uploads the bytes,
    i.e. after the assets have already been queued for ingest — the
    scenario the sort-by-content fix must handle."""

    def _put_asset(asset: Asset, **kwargs) -> Asset:
        asset.sha256 = hashlib.sha256(asset.url.encode()).hexdigest()
        asset.size_bytes = len(asset.url)
        return asset

    sink = MagicMock()
    sink.put_asset = MagicMock(side_effect=_put_asset)
    sink.write_run = MagicMock()
    sink.manifest_url_for = MagicMock(return_value="https://mem/run/manifest.json")
    return sink


# ---------------------------------------------------------------------------
# Step.provider validator — Wave 4 enum subset
# ---------------------------------------------------------------------------


class TestStepProviderValidator:
    def test_none_provider_allowed_for_ingest(self):
        """Ingest steps may have provider=None (no upstream service)."""
        step = Step(
            provider=None,
            model="rss",
            step_type=StepType.INGEST,
            status=StepStatus.SUCCEEDED,
        )
        assert step.provider is None
        assert step.step_type == StepType.INGEST

    def test_none_provider_allowed_for_import(self):
        step = Step(
            provider=None,
            model="cross-tenant",
            step_type=StepType.IMPORT,
            status=StepStatus.SUCCEEDED,
        )
        assert step.provider is None

    def test_none_provider_rejected_for_generate(self):
        with pytest.raises(ValueError, match="provider is required"):
            Step(
                provider=None,
                model="m",
                step_type=StepType.GENERATE,
            )

    def test_none_provider_rejected_for_default_step_type(self):
        """Default step_type is GENERATE → provider=None still rejected."""
        with pytest.raises(ValueError, match="provider is required"):
            Step(provider=None, model="m")

    def test_real_provider_still_works_for_generate(self):
        """Existing call sites that pass provider="..." for generative steps
        continue to work unchanged."""
        step = Step(provider="replicate", model="flux-schnell")
        assert step.provider == "replicate"


# ---------------------------------------------------------------------------
# Modality inference helper
# ---------------------------------------------------------------------------


class TestModalityInference:
    @pytest.mark.parametrize(
        "media_type,expected",
        [
            ("audio/mp3", Modality.AUDIO),
            ("audio/wav", Modality.AUDIO),
            ("video/mp4", Modality.VIDEO),
            ("video/quicktime", Modality.VIDEO),
            ("image/png", Modality.IMAGE),
            ("image/jpeg", Modality.IMAGE),
            ("text/plain", Modality.TEXT),
            ("text/html", Modality.TEXT),
            ("application/pdf", Modality.IMAGE),  # default fallback
            ("", Modality.IMAGE),
        ],
    )
    def test_modality_from_media_type(self, media_type, expected):
        assert _modality_from_media_type(media_type) is expected


# ---------------------------------------------------------------------------
# Core ingest behavior
# ---------------------------------------------------------------------------


class TestIngestAssets:
    def test_rss_feed_source(self):
        """Plan example: pipeline.ingest(assets=[Asset(...)], source='rss')
        produces a manifest documenting the import."""
        assets = [
            _ingestable_asset(url="https://feed.example.com/ep1.mp3"),
            _ingestable_asset(url="https://feed.example.com/ep2.mp3"),
        ]
        sink = _stub_sink_that_records_calls()
        result = Pipeline.ingest(
            assets=assets,
            source="rss",
            source_metadata={"feed_url": "https://example.com/feed.xml"},
            sink=sink,
            name="podcast-import",
        )

        # Run shape
        assert result.run.name == "podcast-import"
        assert result.run.status == RunStatus.COMPLETED
        assert len(result.run.steps) == 2

        # Every step is INGEST + provider=None + model=source.
        for step in result.run.steps:
            assert step.step_type == StepType.INGEST
            assert step.provider is None
            assert step.model == "rss"
            assert step.modality == Modality.AUDIO  # inferred from audio/mp3
            assert step.status == StepStatus.SUCCEEDED
            assert step.metadata["source"] == "rss"
            assert step.metadata["feed_url"] == "https://example.com/feed.xml"

        # Manifest was computed and re-verifies.
        assert result.manifest.canonical_hash != ""
        assert result.manifest.verify()

    def test_ugc_upload_source(self):
        sink = _stub_sink_that_records_calls()
        result = Pipeline.ingest(
            assets=[_ingestable_asset(media_type="image/jpeg")],
            source="ugc-upload",
            source_metadata={"uploader_id": "user-42", "ip": "203.0.113.5"},
            sink=sink,
        )
        step = result.run.steps[0]
        assert step.model == "ugc-upload"
        assert step.modality == Modality.IMAGE
        assert step.metadata == {
            "source": "ugc-upload",
            "uploader_id": "user-42",
            "ip": "203.0.113.5",
        }

    def test_dam_bulk_import(self):
        """Bulk DAM import: many assets, one source, one ingest call."""
        assets = [
            _ingestable_asset(url=f"https://dam.example.com/asset-{i}.png") for i in range(10)
        ]
        sink = _stub_sink_that_records_calls()
        result = Pipeline.ingest(
            assets=assets,
            source="dam-bulk",
            sink=sink,
        )
        assert len(result.run.steps) == 10
        # put_asset was called once per asset.
        assert sink.put_asset.call_count == 10
        assert all("manifest_uri" not in call.kwargs for call in sink.put_asset.call_args_list)

    def test_tenant_ingest_requests_tenant_scoped_reverse_index(self):
        sink = _stub_sink_that_records_calls()
        Pipeline.ingest(
            assets=[_ingestable_asset(media_type="image/png")],
            source="ugc-upload",
            sink=sink,
            tenant_id="tenant-a",
        )

        assert sink.put_asset.call_args.kwargs == {
            "manifest_uri": "https://mem/run/manifest.json",
            "tenant_id": "tenant-a",
        }

    def test_tenant_ingest_normalizes_tenant_id_before_indexing(self):
        sink = _stub_sink_that_records_calls()
        result = Pipeline.ingest(
            assets=[_ingestable_asset(media_type="image/png")],
            source="ugc-upload",
            sink=sink,
            tenant_id="  tenant-a  ",
        )

        assert result.run.tenant_id == "tenant-a"
        assert sink.put_asset.call_args.kwargs == {
            "manifest_uri": "https://mem/run/manifest.json",
            "tenant_id": "tenant-a",
        }

    def test_tenant_ingest_treats_whitespace_tenant_id_as_unset(self):
        sink = _stub_sink_that_records_calls()
        result = Pipeline.ingest(
            assets=[_ingestable_asset(media_type="image/png")],
            source="ugc-upload",
            sink=sink,
            tenant_id="   ",
        )

        assert result.run.tenant_id is None
        assert sink.put_asset.call_args.kwargs == {}

    def test_source_metadata_cannot_clobber_canonical_source(self):
        """If caller's source_metadata has a 'source' key, the
        canonical 'source' from the source= argument wins."""
        result = Pipeline.ingest(
            assets=[_ingestable_asset()],
            source="rss",
            source_metadata={"source": "WRONG", "extra": "kept"},
        )
        step = result.run.steps[0]
        assert step.metadata["source"] == "rss"
        assert step.metadata["extra"] == "kept"

    def test_default_step_type_is_ingest(self):
        result = Pipeline.ingest(assets=[_ingestable_asset()], source="x")
        assert result.run.steps[0].step_type == StepType.INGEST

    def test_step_type_import_for_cross_system_transfers(self):
        result = Pipeline.ingest(
            assets=[_ingestable_asset()],
            source="cross-tenancy",
            step_type=StepType.IMPORT,
        )
        assert result.run.steps[0].step_type == StepType.IMPORT


# ---------------------------------------------------------------------------
# Critical correctness: canonical-hash determinism across input orders
# ---------------------------------------------------------------------------


class TestCanonicalHashDeterminism:
    """Plan correctness gate: same asset set in different orders →
    byte-identical manifest hash. The ingest factory sorts steps by
    content (``asset_provenance_key``) after the sink has populated
    each asset's hash — not by ``asset_id``, and not before hashing —
    so permuted callers always converge."""

    def test_hash_invariant_across_permuted_input_order(self):
        a = _ingestable_asset(asset_id="01-aaaa", url="https://x/a.mp3")
        b = _ingestable_asset(asset_id="02-bbbb", url="https://x/b.mp3")
        c = _ingestable_asset(asset_id="03-cccc", url="https://x/c.mp3")

        # Run 1: input order [a, b, c]
        # Run 2: input order [c, b, a]
        # Run 3: input order [b, a, c]
        # All three must produce manifests with the same canonical hash.
        # Run.name IS in the canonical hash (it's caller-supplied
        # provenance, not a per-execution random) — use the same
        # name for all three so we're isolating the input-order axis.
        runs = [
            Pipeline.ingest(assets=[a, b, c], source="t", name="ingest"),
            Pipeline.ingest(assets=[c, b, a], source="t", name="ingest"),
            Pipeline.ingest(assets=[b, a, c], source="t", name="ingest"),
        ]

        # Hash equality across all permutations.
        hashes = {r.manifest.canonical_hash for r in runs}
        assert len(hashes) == 1, f"hashes diverged across permutations: {hashes}"

    def test_hash_invariant_across_permuted_order_with_sink_assigned_hashes(self):
        """Regression for issue #76 (sink-populated-hash path).

        Assets typically have no ``sha256`` yet when passed to
        ``Pipeline.ingest`` — the sink assigns it during ``put_asset``,
        *after* the caller's input order is already fixed. Sorting before
        the sink runs would tie on the shared pre-hash placeholder content
        and silently fall back to caller input order, reproducing the bug
        one layer deeper. The fix sorts steps by content after the sink has
        populated real, distinct hashes, so permuted input orders must
        still converge on one canonical hash.

        Each permutation gets FRESH ``Asset`` instances (not the same
        mutated objects reused across calls) — ``put_asset`` mutates in
        place, so reusing objects across iterations would carry over
        already-populated hashes from a prior call and mask exactly the
        bug this test targets.
        """

        def _fresh_batch() -> list[Asset]:
            return [
                _ingestable_asset(url="https://x/a.mp3"),
                _ingestable_asset(url="https://x/b.mp3"),
                _ingestable_asset(url="https://x/c.mp3"),
            ]

        hashes = set()
        for perm in ([0, 1, 2], [2, 1, 0], [1, 0, 2]):
            batch = _fresh_batch()
            result = Pipeline.ingest(
                assets=[batch[i] for i in perm],
                source="t",
                name="ingest",
                sink=_stub_sink_that_assigns_content_derived_hashes(),
            )
            hashes.add(result.manifest.canonical_hash)
        assert len(hashes) == 1, f"hashes diverged across permutations: {hashes}"

    def test_hash_changes_when_asset_set_changes(self):
        """Sanity: same hash logic CAN distinguish actually-different inputs."""
        a = _ingestable_asset(asset_id="aaaa", url="https://x/a.mp3")
        b = _ingestable_asset(asset_id="bbbb", url="https://x/b.mp3")

        r1 = Pipeline.ingest(assets=[a], source="t")
        r2 = Pipeline.ingest(assets=[a, b], source="t")
        assert r1.manifest.canonical_hash != r2.manifest.canonical_hash

    def test_hash_stable_across_fresh_asset_batches_with_same_content(self):
        """Regression for issue #76.

        ``asset_id`` defaults to a random UUID and is excluded from the hash
        payload (schema 1.4+), so sorting ingested assets by ``asset_id``
        reshuffles same-content batches on every call and produces different
        canonical hashes. Rebuilding fresh ``Asset`` instances (new random
        asset_id each call, same url/media_type/sha256/size_bytes) must
        converge on ONE canonical_hash across repeated ingests.
        """

        def _fresh_batch() -> list[Asset]:
            # Distinct content per asset (different sha256/size) so a
            # content-based sort actually orders them — unlike the
            # permutation test above, ties here would mask the bug.
            return [
                Asset(
                    url="https://x/a.mp3", media_type="audio/mp3", sha256="a" * 64, size_bytes=100
                ),
                Asset(
                    url="https://x/b.mp3", media_type="audio/mp3", sha256="b" * 64, size_bytes=200
                ),
                Asset(
                    url="https://x/c.mp3", media_type="audio/mp3", sha256="c" * 64, size_bytes=300
                ),
            ]

        hashes = {
            Pipeline.ingest(
                assets=_fresh_batch(), source="t", name="ingest"
            ).manifest.canonical_hash
            for _ in range(10)
        }
        assert len(hashes) == 1, f"hash unstable across fresh asset_id batches: {hashes}"


# ---------------------------------------------------------------------------
# Edge cases + error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_empty_assets_raises(self):
        with pytest.raises(GenblazeError, match="assets list is empty"):
            Pipeline.ingest(assets=[], source="rss")

    def test_invalid_step_type_raises(self):
        with pytest.raises(GenblazeError, match="step_type must be INGEST or IMPORT"):
            Pipeline.ingest(
                assets=[_ingestable_asset()],
                source="t",
                step_type=StepType.GENERATE,
            )

    def test_ingest_without_sink_skips_uploads(self):
        """sink=None means manifest-only — no put_asset / write_run calls."""
        result = ingest_assets(assets=[_ingestable_asset()], source="t", sink=None)
        # Manifest is still produced (in memory); no manifest_uri set.
        assert result.manifest.manifest_uri is None
        assert len(result.run.steps) == 1

    def test_sink_without_put_asset_falls_through_to_warning(self):
        """A sink that raises NotImplementedError on put_asset (e.g.
        ParquetSink) doesn't crash the ingest — just logs a warning
        and proceeds with manifest-only."""

        class _NoPutAssetSink:
            def put_asset(self, asset, **kwargs):
                raise NotImplementedError("test fixture")

            def write_run(self, run, manifest):
                pass

            def manifest_url_for(self, run):
                return None

        result = ingest_assets(
            assets=[_ingestable_asset()],
            source="t",
            sink=_NoPutAssetSink(),
        )
        assert result.manifest.canonical_hash != ""
