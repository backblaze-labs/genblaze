<!-- last_verified: 2026-04-23 -->
# genblaze

Umbrella metapackage for genblaze — a provider-agnostic SDK for AI media generation with built-in provenance (manifests, SHA-256 hashing, B2/S3 durable storage).

This package installs `genblaze-core` and `genblaze-s3` by default so `pip install genblaze` gives you a working pipeline that can persist runs to a Backblaze B2 (or any S3-compatible) bucket out of the box. Provider adapters are opt-in via extras so you only pull what you use.

## Install

```bash
# Core + B2/S3 storage
pip install genblaze

# Add a provider
pip install "genblaze[gmicloud]"
pip install "genblaze[openai,google]"

# Curated bundles
pip install "genblaze[video]"     # GMICloud + Google + Runway + Luma + Decart
pip install "genblaze[image]"     # GMICloud + OpenAI + Google
pip install "genblaze[audio]"     # ElevenLabs + LMNT + Stability Audio + GMICloud

# Everything
pip install "genblaze[all]"
```

## Import

The Python import name is `genblaze_core` (underscore, not hyphen):

```python
from genblaze_core import Pipeline
from genblaze_core.storage import ObjectStorageSink
```

See the [main repo README](https://github.com/backblaze-labs/genblaze#readme) for a complete package-to-import mapping and quickstart.

## What's inside

- `genblaze-core` — pipeline orchestration, manifests, models, storage abstractions
- `genblaze-s3` — S3-compatible storage backend with first-class Backblaze B2 support

Each provider adapter (GMICloud, OpenAI, Google, etc.) is its own installable package to keep base installs lightweight. Install only the ones you need.

## Links

- Main repo: https://github.com/backblaze-labs/genblaze
- Documentation: https://github.com/backblaze-labs/genblaze#readme
- Issues: https://github.com/backblaze-labs/genblaze/issues
