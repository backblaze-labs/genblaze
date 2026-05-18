<!-- last_verified: 2026-05-12 -->
# Genblaze Documentation

Genblaze is a Python SDK for generative-media pipelines with cryptographic
provenance manifests, pluggable provider connectors, and S3-compatible
storage sinks (Backblaze B2 first).

New here? Read [README.md](../README.md) → [ARCHITECTURE.md](../ARCHITECTURE.md) →
[AGENTS.md](../AGENTS.md) → the relevant feature doc below.

---

## Getting Started

- [App workflows](app-workflows.md) — end-user flows: build a pipeline, run it,
  save with provenance, verify, replay.
- [Dev workflows](dev-workflows.md) — contributor setup, adding features,
  running tests, releasing.
- [Adding a new provider](guides/new-provider.md) — canonical contract for
  contributing a provider adapter (start with `/scaffold-provider`).
- [Migrating to 0.3.0](guides/migrating-to-0.3.md) — upgrade guide from
  `genblaze-core` 0.2.x.

## Core Concepts

- [Pipeline](features/pipeline.md) — fluent API for multi-step generative
  workflows with automatic manifest creation.
- [Provider system](features/provider-system.md) — pluggable adapter pattern
  with standardized lifecycle, error classification, retry tracking.
- [Model registry](features/model-registry.md) — declarative, per-model
  configuration surface; add models / override pricing / register families.
- [Manifest provenance](features/manifest-provenance.md) — hash-verified
  canonical JSON capturing full run provenance.
- [Trust modes](features/trust-modes.md) — what the manifest does and does
  not prove; pick the right verification posture.

## Generation & Iteration

- [Agents](features/agents.md) — generate → evaluate → refine loops linked
  through `parent_run_id`.
- [Iteration & lineage](features/iteration.md) — `parent_run_id` chains for
  prompt refinement and reproducibility.
- [Prompt templates](features/prompt-templates.md) — `{variable}` placeholders
  for reusable, parameterized prompts.
- [Pipeline templates](features/pipeline-templates.md) — declarative,
  serializable pipeline definitions.
- [LLM calls](features/llm-calls.md) — thin standalone chat wrappers (not
  pipeline-integrated).

## Media, Assets & Policy

- [Media embedding](features/media-embedding.md) — embed manifests into PNG,
  JPEG, WebP, MP4, MP3, WAV, AAC, FLAC, with sidecar fallback.
- [Embed policy](features/embed-policy.md) — redact prompts, pointer mode,
  strip parameters before embedding.
- [Asset transforms](features/asset-transforms.md) — `FFmpegTransform` for
  resize, crop, overlay, normalize, format conversion.
- [Video & audio parameters](features/video-params.md) — standard
  `step.params` keys video/audio providers map from.
- [Moderation](features/moderation.md) — pre/post-step content screening.

## Workflows & Eventing

- [Ingest workflows](features/ingest-workflows.md) — non-generative flows:
  live ingest, UGC, archival imports, DAM bulk loads, migrations.
- [Streaming](features/streaming.md) — push-style event iterators over
  pipeline execution (typed discriminated union).
- [Queue integration](features/queue-integration.md) — running long-running
  jobs from background workers with checkpoint + resume.
- [Observability](features/observability.md) — pluggable tracers (logging,
  OpenTelemetry, LangSmith, custom).
- [Webhooks](features/webhooks.md) — fire-and-forget HTTP notifications for
  pipeline events.

## Storage & Data

- [Object storage](features/object-storage.md) — upload run assets and
  manifests to B2 / S3 / R2 / MinIO via `ObjectStorageSink`.
- [Parquet sink](features/parquet-sink.md) — partitioned Parquet output for
  analytics and querying.

## Reliability

- [Retry policy](features/retry-policy.md) — user-tunable transient-failure
  handling for the `submit / poll / fetch_output` lifecycle.

## Tooling

- [CLI](features/cli.md) — `genblaze` command: extract, verify, replay, index
  manifests in media files.

## Reference

- [Model matrix](reference/model-matrix.md) — auto-generated table of slugs,
  modalities, pricing, and allowed params for every model in every installed
  connector.
- [Pricing recipes](reference/pricing-recipes.md) — pricing snapshots from
  the 0.3.0 catalog-decoupling migration. **Not maintained — verify with
  upstream before billing.**

## Planning & Tracking

- [Active exec-plans](exec-plans/active/) — in-flight tranches and rollouts.
  See [release-0.3.md](exec-plans/active/release-0.3.md),
  [p0-p1-feedback-execution.md](exec-plans/active/p0-p1-feedback-execution.md),
  [storage-backend-hardening-tranche.md](exec-plans/active/storage-backend-hardening-tranche.md),
  [manifest-signing-and-redaction-tranche.md](exec-plans/active/manifest-signing-and-redaction-tranche.md),
  [pipeline-replay-and-cost-ledger-tranche.md](exec-plans/active/pipeline-replay-and-cost-ledger-tranche.md),
  [ingest-sink-and-non-generative-pipelines-tranche.md](exec-plans/active/ingest-sink-and-non-generative-pipelines-tranche.md),
  [multimodal-chat-provider.md](exec-plans/active/multimodal-chat-provider.md),
  [retry-policy-unification.md](exec-plans/active/retry-policy-unification.md),
  [framework-dx-recommendations.md](exec-plans/active/framework-dx-recommendations.md).
- [Completed exec-plans](exec-plans/completed/) — shipped initiatives,
  archived for reference.
- [SDK feedback tracker](exec-plans/feedback.md) — triaged inbox for real-user
  and sample-app feedback.
- [Tech-debt tracker](exec-plans/tech-debt-tracker.md) — known debt with
  impact, resolution shape, and priority.
- [Feature doc template](features/_template.md) — boilerplate for new feature
  docs.

---

_Audit docs freshness with `/verify-docs`._
