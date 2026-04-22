#!/usr/bin/env python3
"""Example: Backblaze B2 storage pipeline — the recommended default sink.

Assets are downloaded from provider CDNs, content-hashed (SHA-256), and
uploaded to B2 with dedup. Manifests are stored alongside assets for full
provenance tracking.

Requirements:
    pip install genblaze-s3 genblaze-replicate

Usage:
    export B2_KEY_ID="your-key-id"
    export B2_APP_KEY="your-app-key"
    export REPLICATE_API_TOKEN="r8_..."
    python examples/b2_storage_pipeline.py
"""

from genblaze_core import KeyStrategy, ObjectStorageSink, Pipeline
from genblaze_s3 import S3StorageBackend


def main() -> None:
    from genblaze_replicate import ReplicateProvider

    # --- Configure Backblaze B2 storage ---
    # for_backblaze() reads B2_KEY_ID / B2_APP_KEY from env and derives the
    # S3 endpoint from the region. region= defaults to "us-west-004"; pass
    # the region your bucket actually lives in (e.g. "us-east-005",
    # "eu-central-003") to skip the one-time redirect. The backend will
    # auto-correct the region on first use either way.
    # Pass public_url_base only for public buckets; otherwise get_url()
    # returns pre-signed URLs.
    backend = S3StorageBackend.for_backblaze(
        "my-genblaze-bucket",
        region="us-west-004",
        public_url_base="https://f004.backblazeb2.com/file/my-genblaze-bucket",
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


if __name__ == "__main__":
    main()
