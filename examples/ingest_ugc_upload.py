#!/usr/bin/env python3
"""Example: user-generated content upload via `Pipeline.ingest`.

A web app accepts a file from a user, persists it to durable storage,
and records who uploaded it. The resulting manifest documents the
upload event: who (uploader_id), when (run created_at), what
(SHA-256 + media_type), and from where (source attribution).

The pattern composes with :class:`ModerationHook` for content checks
before serving the asset back downstream — a classic UGC moderation
flow with provenance baked in.

Requirements:
    pip install genblaze-s3

Usage:
    export B2_KEY_ID="your-key-id"
    export B2_APP_KEY="your-app-key"
    python examples/ingest_ugc_upload.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from genblaze_core import Asset, KeyStrategy, ObjectStorageSink, Pipeline
from genblaze_s3 import S3StorageBackend


def write_demo_upload(tmp_dir: Path) -> Path:
    """Stand-in for a real upload handler.

    A web app would receive bytes from a multipart form, validate
    content-type, scan with a virus checker, and stage to a temp
    file. For this example we synthesize a tiny PNG.
    """
    upload_path = tmp_dir / "user-uploaded-photo.png"
    # Minimal valid PNG (1×1 transparent pixel).
    upload_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xfc\xff?\x03\x00\x00\x05\x00"
        b"\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return upload_path


def main() -> None:
    # --- Storage setup ---
    backend = S3StorageBackend.for_backblaze(
        "ugc-uploads",
        auto_lifecycle=True,
    )
    sink = ObjectStorageSink(
        backend,
        prefix="ugc",
        # CONTENT_ADDRESSABLE: if a user re-uploads the same image,
        # the dedup is automatic at the storage layer; the manifest
        # still records the new upload event so we know who uploaded
        # what and when, even when the bytes coincide.
        key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
    )

    # --- Simulate a user upload landing on disk ---
    with tempfile.TemporaryDirectory() as tmp:
        upload_path = write_demo_upload(Path(tmp))

        asset = Asset(
            url=f"file://{upload_path}",
            media_type="image/png",
        )

        # --- Ingest with full uploader attribution ---
        # source_metadata captures everything a UGC pipeline needs
        # for moderation, abuse-reporting, and audit. The values
        # land in step.metadata and are part of the canonical hash,
        # so tampering is detectable.
        result = Pipeline.ingest(
            assets=[asset],
            source="ugc-upload",
            source_metadata={
                "uploader_id": "user-12345",
                "session_id": "session-abc",
                "ip": "203.0.113.5",
                "user_agent": "Mozilla/5.0 (example)",
            },
            sink=sink,
            name="ugc-upload-event",
        )

    # --- Inspect ---
    step = result.run.steps[0]
    asset = step.assets[0]
    print(f"Uploaded asset id:     {asset.asset_id}")
    print(f"Storage URL:           {asset.url}")
    print(f"SHA-256:               {asset.sha256}")
    print(f"Size:                  {asset.size_bytes} bytes")
    print(f"Uploader (from meta):  {step.metadata['uploader_id']!r}")
    print(f"Manifest hash:         {result.manifest.canonical_hash}")
    print(f"Manifest URI:          {result.manifest.manifest_uri}")

    # --- Reverse lookup demo ---
    # Later, if we have just the asset_id (e.g. from a moderation queue
    # row), we can recover the full provenance manifest:
    recovered = sink.read_manifest_for_asset(asset.asset_id)
    if recovered is not None:
        print(f"\nReverse lookup recovered manifest hash: {recovered.canonical_hash}")
        # Same uploader metadata is preserved in the recovered manifest.
        assert recovered.run.steps[0].metadata["uploader_id"] == step.metadata["uploader_id"]

    # --- Optional: moderation hook ---
    #
    # For UGC apps, run moderation against the persistable asset before
    # serving it to other users. The check runs against the asset's
    # durable URL — your moderation provider downloads from it directly:
    #
    #     from genblaze_core.pipeline.moderation import ModerationHook
    #
    #     class MyImageModerationHook(ModerationHook):
    #         def check_output(self, assets):
    #             for asset in assets:
    #                 # call into Rekognition / OpenAI moderation / etc.
    #                 ...
    #             return ModerationResult(allow=..., reasons=...)
    #
    #     hook = MyImageModerationHook()
    #     decision = hook.check_output([asset])
    #     if not decision.allow:
    #         # quarantine — keep the manifest as audit, but don't serve
    #         pass


if __name__ == "__main__":
    main()
