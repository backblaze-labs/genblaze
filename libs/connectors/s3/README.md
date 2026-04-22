<!-- last_verified: 2026-04-22 -->
# genblaze-s3

S3-compatible storage backend for [genblaze](https://github.com/backblaze-labs/genblaze). Works with [Backblaze B2](https://www.backblaze.com/sign-up/ai-cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) (recommended default), Cloudflare R2, MinIO, and AWS S3.

## Install

```bash
pip install genblaze-s3
```

## Usage

Registers the `s3` storage backend via entry points. Point `genblaze-core` at an S3-compatible endpoint and credentials; the backend handles uploads, presigning, and (on Backblaze B2) Object Lock retention for manifests.

## Documentation

Full configuration, endpoint recipes, and Object Lock notes live in the monorepo: https://github.com/backblaze-labs/genblaze

## License

MIT
