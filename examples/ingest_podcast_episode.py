#!/usr/bin/env python3
"""Example: podcast hosting via `Pipeline.ingest`.

Pulls a list of episode URLs (in a real app you'd parse them from an
RSS feed), persists them to Backblaze B2 with content-addressable dedup,
and produces a manifest documenting the ingest event. Each episode is
recorded with its source URL, ingest timestamp, and SHA-256 hash —
ready for downstream transcription / classification chains.

The full "download → transcribe → store" chain the storage-tranche
plan describes depends on Wave 6's `WhisperProvider`. This example
ships the ingest half end-to-end; the transcribe step is sketched at
the bottom.

Requirements:
    pip install genblaze-s3

Usage:
    export B2_KEY_ID="your-key-id"
    export B2_APP_KEY="your-app-key"
    python examples/ingest_podcast_episode.py
"""

from __future__ import annotations

from genblaze_core import Asset, KeyStrategy, ObjectStorageSink, Pipeline
from genblaze_s3 import S3StorageBackend


def fetch_feed_entries() -> list[Asset]:
    """Stand-in for an RSS-feed parser.

    A real app would use ``feedparser`` (or similar) to read the feed
    and produce one ``Asset`` per ``<enclosure>`` URL. For this
    example we hard-code a couple of representative episodes.
    """
    return [
        Asset(
            url="https://traffic.libsyn.com/example-podcast/ep-042.mp3",
            media_type="audio/mp3",
        ),
        Asset(
            url="https://traffic.libsyn.com/example-podcast/ep-043.mp3",
            media_type="audio/mp3",
        ),
    ]


def main() -> None:
    # --- Storage setup ---
    # CONTENT_ADDRESSABLE strategy: identical episodes (e.g. re-ingested
    # over multiple feed pulls) hash-collapse into a single backend
    # object. The reverse-lookup index still records each ingest event
    # separately, so provenance for "we saw this episode N times" is
    # preserved even when bytes dedupe.
    backend = S3StorageBackend.for_backblaze(
        "podcast-archive",
        # auto_lifecycle=True applies recommended bucket-wide rules
        # (cancel orphaned multipart uploads, expire noncurrent
        # versions). Default in 0.3.0+ is False — opt in explicitly.
        auto_lifecycle=True,
    )
    sink = ObjectStorageSink(
        backend,
        prefix="podcasts",
        key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
    )

    # --- Ingest ---
    # One Pipeline.ingest call per feed pull. Each episode becomes a
    # Step in the resulting Run with step_type=INGEST, provider=None,
    # and metadata recording the source attribution.
    episodes = fetch_feed_entries()
    feed_url = "https://feeds.example.com/example-podcast.xml"

    result = Pipeline.ingest(
        assets=episodes,
        source="rss",
        source_metadata={
            "feed_url": feed_url,
            "show_title": "Example Podcast",
        },
        sink=sink,
        name="podcast-rss-pull",
    )

    # --- Inspect the result ---
    print(f"Imported {len(result.run.steps)} episodes")
    print(f"Run id:       {result.run.run_id}")
    print(f"Manifest hash: {result.manifest.canonical_hash}")
    print(f"Manifest URI:  {result.manifest.manifest_uri}")
    print()

    for step in result.run.steps:
        # Each step has exactly one asset (the episode).
        asset = step.assets[0]
        print(f"  {asset.url}")
        print(f"    sha256={asset.sha256[:16]}...  size={asset.size_bytes} bytes")
        print(f"    source={step.metadata['source']!r}")
    print()

    # --- Reverse lookup ---
    # Given just an asset_id (e.g. from a job-queue row), recover the
    # manifest that introduced it. Useful for downstream workers that
    # need to know "where did this byte stream come from?"
    first_asset_id = episodes[0].asset_id
    recovered = sink.read_manifest_for_asset(first_asset_id)
    if recovered is not None:
        print(f"Reverse lookup for asset {first_asset_id[:8]}...:")
        print(f"  manifest hash: {recovered.canonical_hash}")
        print(f"  source: {recovered.run.steps[0].metadata['source']!r}")

    # --- Composing with downstream generation (Wave 6 dependency) ---
    #
    # Once `WhisperProvider` lands in genblaze-openai, you'd chain
    # transcription on top of the ingested episodes:
    #
    #     transcribed = (
    #         Pipeline("transcribe-batch")
    #         .step(
    #             WhisperProvider(),
    #             model="whisper-1",
    #             input_from=[step.step_index for step in result.run.steps],
    #             modality=Modality.TEXT,
    #         )
    #         .run(sink=sink)
    #     )
    #
    # Until Wave 6 ships, the ingest manifest stands on its own as a
    # provenance record of the import event.


if __name__ == "__main__":
    main()
