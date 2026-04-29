#!/usr/bin/env python3
"""Quickstart: Generate a video, persist to Backblaze B2, verify provenance.

End-to-end example — mirrors the Quickstart in the repo README. Generates
a video via GMICloud (Seedance 2.0 — ByteDance's latest multimodal video
model), uploads the asset and its provenance manifest to Backblaze B2,
and prints the durable URLs plus SHA-256 integrity verification.

Usage:
    pip install genblaze-core genblaze-gmicloud genblaze-s3

    export GMI_API_KEY="gmi-..."
    export B2_KEY_ID="..."
    export B2_APP_KEY="..."

    python examples/quickstart.py

Replace "my-bucket" below with the name of a B2 bucket you own. The
backend auto-detects the bucket's region on first use, so passing
``region=`` is just an optimization hint (default ``us-west-004``).
If your bucket lives in ``us-east-005`` or ``eu-central-003``, pass
it explicitly to skip the redirect round-trip.

For a simpler demo without any API keys, see quickstart_local.py.
"""

from genblaze_core import KeyStrategy, Modality, ObjectStorageSink, Pipeline
from genblaze_gmicloud import GMICloudVideoProvider
from genblaze_s3 import S3StorageBackend

# Progress is shown automatically by Pipeline.run() when stderr is a TTY —
# a compact spinner with provider:model and elapsed time. Opt out with
# ``.run(progress=False)``, or pass your own ``on_progress=`` callback.


def main() -> None:
    storage = ObjectStorageSink(
        # Default region is us-west-004. Override with region="us-east-005"
        # (or whatever region your bucket was created in) to skip the
        # one-time redirect when the hint doesn't match.
        # auto_lifecycle=True opts in to recommended lifecycle rules
        # (cancel orphaned multipart uploads, expire noncurrent versions);
        # default in 0.3.0+ is False so callers managing lifecycle
        # out-of-band aren't surprised by hidden bucket mutations.
        S3StorageBackend.for_backblaze("my-bucket", auto_lifecycle=True),
        key_strategy=KeyStrategy.HIERARCHICAL,
    )

    result = (
        Pipeline("my-first-pipeline")
        .step(
            GMICloudVideoProvider(),
            model="seedance-2-0-260128",
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
