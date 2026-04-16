# Documentation Quality Checklist

Run through every item. Mark `[x]` when verified, `[!]` when a problem is found.

## Core Docs Accuracy
- [ ] `README.md` — install commands work, quickstart is accurate
- [ ] `ARCHITECTURE.md` — matches actual module structure
- [ ] `AGENTS.md` — invariants are all still enforced
- [ ] `CONTRIBUTING.md` — process matches actual workflow
- [ ] `CHANGELOG.md` — latest version entry is current
- [ ] `CLAUDE.md` — read order and commands are correct

## Feature Docs (docs/features/)
- [ ] `pipeline.md` — matches Pipeline class behavior
- [ ] `provider-system.md` — matches BaseProvider interface
- [ ] `manifest-provenance.md` — matches Manifest model
- [ ] `media-embedding.md` — covers all supported formats
- [ ] `embed-policy.md` — matches EmbedPolicy model
- [ ] `object-storage.md` — matches StorageBackend interface
- [ ] `parquet-sink.md` — matches ParquetSink behavior
- [ ] `iteration.md` — matches parent_run_id behavior
- [ ] `cli.md` — matches actual CLI commands
- [ ] `prompt-templates.md` — matches PromptTemplate model
- [ ] `pipeline-templates.md` — matches PipelineTemplate
- [ ] `asset-transforms.md` — matches FFmpegTransform
- [ ] `moderation.md` — matches ModerationHook
- [ ] `webhooks.md` — matches WebhookNotifier
- [ ] `queue-integration.md` — patterns are accurate
- [ ] `video-params.md` — metadata fields match code

## User-Facing Docs (docs_site/)
- [ ] Installation guide works step-by-step
- [ ] Quickstart guide produces expected output
- [ ] API reference matches current public API
- [ ] Concepts docs match actual behavior
- [ ] Navigation (mkdocs.yml) covers all pages

## Code Examples
- [ ] All examples in `examples/` have docstrings explaining what they do
- [ ] Import paths in examples match actual package exports
- [ ] No deprecated API usage in examples
- [ ] Examples cover all 12 providers
- [ ] Multi-step examples (chain, fan-in) are present and accurate

## Cross-References
- [ ] Internal doc links are not broken:
  ```bash
  grep -rn "\[.*\](.*\.md)" --include="*.md" docs/ | while read line; do
    file=$(echo "$line" | sed 's/.*](\(.*\.md\)).*/\1/')
    # Verify file exists relative to the doc
  done
  ```
- [ ] AGENTS.md doc map is complete (no missing docs)
- [ ] README links point to correct files

## Agent Readability
- [ ] Docs use consistent terminology (same terms as code)
- [ ] Technical terms are defined on first use
- [ ] Error codes/messages documented with remediation steps
- [ ] Configuration options listed with types and defaults
- [ ] API examples show both sync and async usage where applicable

## Exec Plans
- [ ] Active plans are still relevant (not stale)
- [ ] Completed plans are in `completed/` directory
- [ ] Tech-debt-tracker is current
- [ ] No active plan references deleted/moved files
