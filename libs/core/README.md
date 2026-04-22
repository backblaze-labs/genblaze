<!-- last_verified: 2026-04-22 -->
# genblaze-core

Core SDK for [genblaze](https://github.com/backblaze-labs/genblaze) — pipeline orchestration for AI-generated video, audio, and images with built-in provenance.

This package ships the provider/storage/tracer interfaces, the SHA-256–verified provenance manifest, and the pipeline runner. Provider and storage backends live in separate `genblaze-*` packages and register via entry points.

## Install

```bash
pip install genblaze-core
```

Optional extras:

- `genblaze-core[parquet]` — Parquet manifest export
- `genblaze-core[audio]` — audio metadata embedding

## Documentation

Full documentation, provider matrix, and examples live in the monorepo: https://github.com/backblaze-labs/genblaze

## License

MIT
