#!/usr/bin/env python3
"""Example: AWS S3 storage pipeline.

Demonstrates genblaze with standard AWS S3. Assets are downloaded from
provider CDNs, content-hashed, and uploaded to S3. Uses pre-signed URLs
for asset access.

Requirements:
    pip install genblaze-s3 genblaze-replicate

Usage:
    # AWS credentials via env vars, ~/.aws/credentials, or IAM role
    export AWS_ACCESS_KEY_ID="..."
    export AWS_SECRET_ACCESS_KEY="..."
    export REPLICATE_API_TOKEN="r8_..."
    python examples/s3_storage_pipeline.py
"""

from genblaze_core import KeyStrategy, ObjectStorageSink, Pipeline
from genblaze_s3 import S3StorageBackend


def main() -> None:
    from genblaze_replicate import ReplicateProvider

    # --- Configure AWS S3 storage ---
    # No endpoint_url needed for standard AWS S3 (boto3 uses defaults).
    # No public_url_base → get_url() returns pre-signed URLs.
    backend = S3StorageBackend(
        bucket="my-genblaze-bucket",
        region="us-east-1",
    )

    # Hierarchical keys group everything under one run folder.
    # Bucket structure:
    #   genblaze-assets/runs/{date}/{run_id}/manifest.json
    #   genblaze-assets/runs/{date}/{run_id}/assets/{asset_id}.ext
    sink = ObjectStorageSink(
        backend,
        prefix="genblaze-assets",
        key_strategy=KeyStrategy.HIERARCHICAL,
    )

    # --- Generate and store ---
    provider = ReplicateProvider()
    result = (
        Pipeline("s3-demo", project_id="examples")
        .step(
            provider,
            model="black-forest-labs/flux-schnell",
            prompt="a photorealistic sunset over the ocean, golden hour",
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

    # Optional: write structured run data to Parquet for analytics
    # from genblaze_core import ParquetSink
    # parquet = ParquetSink("data/")
    # parquet.write_run(result.run, result.manifest)

    backend.close()


if __name__ == "__main__":
    main()
