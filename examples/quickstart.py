#!/usr/bin/env python3
"""Quickstart: Generate a video, persist to Backblaze B2, verify provenance.

End-to-end example — mirrors the Quickstart in the repo README. Generates
a video via GMICloud (Seedance 1.0 Pro), uploads the asset and its
provenance manifest to Backblaze B2, and prints the durable URLs plus
SHA-256 integrity verification.

Usage:
    pip install genblaze-core genblaze-gmicloud genblaze-s3

    export GMI_API_KEY="gmi-..."
    export B2_KEY_ID="..."
    export B2_APP_KEY="..."

    python examples/quickstart.py

Replace "my-bucket" below with the name of a B2 bucket you own. The
backend auto-detects the bucket's region on first use.

For a simpler demo without any API keys, see quickstart_local.py.
"""

from genblaze_core import KeyStrategy, Modality, ObjectStorageSink, Pipeline
from genblaze_gmicloud import GMICloudVideoProvider
from genblaze_s3 import S3StorageBackend


def main() -> None:
    storage = ObjectStorageSink(
        S3StorageBackend.for_backblaze("my-bucket"),
        key_strategy=KeyStrategy.HIERARCHICAL,
    )

    result = (
        Pipeline("my-first-pipeline")
        .step(
            GMICloudVideoProvider(),
            model="Seedance-1.0-Pro",
            prompt="A drone shot soaring over a coastal city at golden hour",
            modality=Modality.VIDEO,
            duration=10,
            aspect_ratio="16:9",
        )
        .run(sink=storage, timeout=600)
    )

    asset = result.run.steps[0].assets[0]
    print(f"Asset URL: {asset.url}")  # B2 durable URL — no signature, never expires
    print(f"SHA-256:   {asset.sha256}")
    print(f"Manifest:  {result.manifest.manifest_uri}")  # Provenance JSON in B2
    print(f"Hash:      {result.manifest.canonical_hash}")
    print(f"Verified:  {result.manifest.verify()}")


if __name__ == "__main__":
    main()
