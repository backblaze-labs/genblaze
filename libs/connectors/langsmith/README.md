<!-- last_verified: 2026-11-18 -->
# genblaze-langsmith

**[LangSmith](https://www.langchain.com/langsmith) tracer backend for [genblaze](https://github.com/backblaze-labs/genblaze) — AI-pipeline observability for generative media, with every run, step, and asset traced alongside its SHA-256 provenance manifest.**

`genblaze-langsmith` plugs a `LangSmithTracer` into genblaze's observability stack, forwarding pipeline spans, provider calls, and manifest events to a LangSmith project. Combine it with genblaze's existing `OTelTracer`, `LoggingTracer`, or `CompositeTracer` for multi-destination tracing across your generative AI workflows.

## Why genblaze-langsmith

- **End-to-end AI pipeline traces** — every provider call (Veo, Runway, Flux, ElevenLabs, …) appears as a LangSmith span with prompt, model, params, cost, and manifest hash.
- **Drop-in tracer** — attach to `Pipeline(tracer=…)`; no code changes to providers or steps.
- **Composable** — wrap with `CompositeTracer` to send to LangSmith + OTel + logs simultaneously.
- **Project-aware** — traces land in the LangSmith project you pass as `project_name`.
- **Works offline** — tracer no-ops if LangSmith isn't configured; no production risk.

## Install

```bash
pip install genblaze-langsmith
```

## Quickstart

```bash
pip install genblaze-core genblaze-langsmith
export LANGSMITH_API_KEY="..."
export LANGSMITH_PROJECT="genblaze-prod"
```

```python
import os

from genblaze_core import Modality, Pipeline
from genblaze_core.testing import MockVideoProvider
from genblaze_langsmith import LangSmithTracer

# The langsmith Client reads LANGSMITH_API_KEY; the project must be passed explicitly.
tracer = LangSmithTracer(project_name=os.environ.get("LANGSMITH_PROJECT", "default"))

run, manifest = (
    Pipeline("traced-demo", tracer=tracer)
    .step(MockVideoProvider(), model="mock-v1",
          prompt="a drone shot over a city at dusk",
          modality=Modality.VIDEO)
    .run()
)

# Span with prompt, model, manifest hash, duration now appears in LangSmith
print(manifest.canonical_hash)
```

Compose with other tracers:

```python
from genblaze_core import CompositeTracer, LoggingTracer, OTelTracer
from genblaze_langsmith import LangSmithTracer

tracer = CompositeTracer([LangSmithTracer(), OTelTracer(), LoggingTracer()])
# Pipeline("…", tracer=tracer)…
```

## Credentials

| Env var | Notes |
|---|---|
| `LANGSMITH_API_KEY` | LangSmith API key, read by the `langsmith` Client (or pass `api_key=`) |
| `LANGSMITH_ENDPOINT` | Override the LangSmith endpoint, read by the `langsmith` Client (optional) |

`LangSmithTracer` does not read `LANGSMITH_PROJECT`. Pass `project_name=` explicitly; it defaults to `"default"`.

## Documentation

- **Main repo**: https://github.com/backblaze-labs/genblaze
- **Observability docs**: https://github.com/backblaze-labs/genblaze/tree/main/docs/features

## Related packages

- [`genblaze-core`](https://pypi.org/project/genblaze-core/) — the pipeline SDK
- [`genblaze-s3`](https://pypi.org/project/genblaze-s3/) — durable storage on [Backblaze B2](https://www.backblaze.com/cloud-storage?utm_source=github&utm_medium=referral&utm_campaign=ai_artifacts&utm_content=genblaze) and other S3-compatible backends

## License

MIT
