<!-- last_verified: 2026-08-01 -->
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

# Optional: Parquet-backed manifest sink (ParquetSink)
pip install "genblaze[parquet]"
```

> **A GitHub Release tag (e.g. `v0.7.0`) is not a `genblaze` version — don't pin `genblaze==<wave tag>`.**
> The tag names a CHANGELOG *wave*; every package in that wave versions independently, so
> wave tags and the umbrella's PyPI versions are separate sequences that happen to look
> alike. A pin on a wave tag either:
> - **fails outright** (no such version was published), or
> - **resolves silently to an unrelated umbrella build from a different wave** — no
>   error, just stale code (e.g. `genblaze==0.4.0` on PyPI predates the `v0.4.0` wave).
>
> Pin the exact umbrella version instead (from that wave's "Released package versions"
> list in its [release notes](https://github.com/backblaze-labs/genblaze/releases)) — but
> note the umbrella pins ranges (e.g. `genblaze-core>=0.3.8,<0.4`), not exact versions, so
> even that isn't fully reproducible on its own. For a locked install, generate a lockfile
> (`pip freeze`, `uv lock`, or a constraints file) once your stack works.

## Import

`pip install genblaze` gives you both import paths:

```python
from genblaze import Pipeline                        # umbrella re-export
from genblaze_core import Pipeline                   # canonical (used throughout docs)
from genblaze_core.storage import ObjectStorageSink  # submodules -> genblaze_core
```

Both forms resolve to the same object. The top-level `genblaze` module mirrors
`genblaze_core.__all__` lazily, so only the symbols you actually use get
loaded. For nested submodules (`genblaze_core.media`, `genblaze_core.canonical`)
and provider adapters (`genblaze_openai`, `genblaze_google`, …) keep using
their own names — adapters install as extras (`pip install "genblaze[openai]"`).

See the [main repo README](https://github.com/backblaze-labs/genblaze#readme) for a complete package-to-import mapping and quickstart.

## What's inside

- `genblaze-core` — pipeline orchestration, manifests, models, storage abstractions
- `genblaze-s3` — S3-compatible storage backend with first-class Backblaze B2 support

Each provider adapter (GMICloud, OpenAI, Google, etc.) is its own installable package to keep base installs lightweight. Install only the ones you need.

## Links

- Main repo: https://github.com/backblaze-labs/genblaze
- Documentation: https://github.com/backblaze-labs/genblaze#readme
- Issues: https://github.com/backblaze-labs/genblaze/issues
