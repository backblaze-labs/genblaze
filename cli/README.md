<!-- last_verified: 2026-04-22 -->
# genblaze-cli

**Command-line toolkit for inspecting, verifying, and indexing genblaze AI-generated-media provenance manifests.**

`genblaze-cli` is the companion CLI for [genblaze](https://github.com/backblaze-labs/genblaze) — the Python SDK for generative AI pipelines across video, image, and audio. It lets anyone (not just Python developers) audit AI-generated media: extract the embedded provenance manifest from an MP4 / PNG / MP3, verify its manifest hash and output sha256 coverage, replay a run, or index manifests into Parquet for downstream analytics.

## Why genblaze-cli

- **Audit AI-generated media in one command** — `genblaze verify video.mp4` confirms a file's manifest hash hasn't been tampered with and all output assets declare sha256.
- **Works on any genblaze output** — PNG, JPEG, WebP, MP4, MP3, WAV with embedded manifests.
- **Analytics-ready** — `genblaze index` emits partitioned Parquet tables (runs, steps, assets) for BI tools and data warehouses.
- **Zero provider dependencies** — reads manifests; doesn't call any AI API.
- **Shell-friendly** — non-zero exit codes on verification failure, pipeable JSON output.

## Install

```bash
pip install genblaze-cli
```

Installs the `genblaze` console script.

## Usage

```bash
genblaze --help

genblaze extract video.mp4                # Extract embedded manifest → stdout (JSON)
genblaze extract video.mp4 -o m.json      # …or to a file

genblaze verify video.mp4                 # Verify manifest hash + output sha256 declarations
genblaze verify manifest.json             # Or verify a standalone manifest file

genblaze replay manifest.json             # Show what a replay would do (dry run)

genblaze index manifest.json -o data/     # Index into partitioned Parquet tables
```

Exit codes are non-zero on verification failure — safe to drop into CI pipelines, release checks, or content-moderation workflows. The command does not fetch remote asset URLs; consumers that dereference `asset.url` must hash those bytes separately and compare them with the manifest's `asset.sha256`.

## Typical flow

```bash
# 1. Someone ships you an AI-generated video
genblaze extract delivered-ad.mp4 -o manifest.json

# 2. Confirm it hasn't been tampered with
genblaze verify delivered-ad.mp4

# 3. Index into your analytics warehouse
genblaze index manifest.json -o s3://analytics/genblaze/
```

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **CLI reference**: https://github.com/backblaze-labs/genblaze/tree/main/cli

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK that produces these manifests
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze), AWS S3, Cloudflare R2, MinIO

## License

MIT
