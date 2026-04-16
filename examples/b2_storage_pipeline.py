#!/usr/bin/env python3
"""Example: Backblaze B2 storage pipeline.

Demonstrates genblaze with Backblaze B2 object storage. Assets are downloaded
from provider CDNs, content-hashed (SHA-256), and uploaded to B2 with dedup.
Manifests are stored alongside assets for full provenance tracking.

Requirements:
    pip install genblaze-s3 genblaze-replicate

Usage:
    export B2_KEY_ID="your-key-id"
    export B2_APP_KEY="your-app-key"
    export REPLICATE_API_TOKEN="r8_..."
    python examples/b2_storage_pipeline.py
"""

import os

from genblaze_core import KeyStrategy, ObjectStorageSink, Pipeline
from genblaze_s3 import S3StorageBackend


def main() -> None:
    from genblaze_replicate import ReplicateProvider

    # --- Configure Backblaze B2 storage ---
    backend = S3StorageBackend(
        bucket="my-genblaze-bucket",
        endpoint_url="https://s3.us-west-004.backblazeb2.com",
        region="us-west-004",
        # B2 friendly URLs for public access (no pre-signed URLs needed)
        public_url_base="https://f004.backblazeb2.com/file/my-genblaze-bucket",
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APP_KEY"],
    )

    # Content-addressable keys deduplicate identical assets automatically.
    # Bucket structure:
    #   genblaze-assets/assets/{sha[:2]}/{sha[2:4]}/{sha}.ext
    #   genblaze-assets/manifests/{run_id}.json
    sink = ObjectStorageSink(
        backend,
        prefix="genblaze-assets",
        key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
    )

    # --- Generate and store ---
    provider = ReplicateProvider()
    result = (
        Pipeline("b2-demo", project_id="examples")
        .step(
            provider,
            model="black-forest-labs/flux-schnell",
            prompt="a photorealistic cat wearing a tiny spacesuit, floating in zero gravity",
        )
        .run(sink=sink, timeout=120)
    )

    print(f"Run ID:     {result.run.run_id}")
    print(f"Hash:       {result.manifest.canonical_hash}")
    print(f"Verified:   {result.manifest.verify()}")
    for step in result.run.steps:
        for asset in step.assets:
            print(f"  Asset:    {asset.url}")
            print(f"  SHA-256:  {asset.sha256}")

    backend.close()


if __name__ == "__main__":
    main()
