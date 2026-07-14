<!-- last_verified: 2026-04-23 -->
# Dev Workflows

## Setup

- [ ] Clone repo
- [ ] Run `make install-dev`
- [ ] Run `make test` to verify setup

## New Feature

- [ ] Create execution plan in `docs/exec-plans/active/`
- [ ] Implement feature in appropriate package (`libs/core/`, `libs/connectors/`, `cli/`)
- [ ] Write tests (unit at minimum)
- [ ] Create feature doc in `docs/features/<feature>.md`
- [ ] Update `ARCHITECTURE.md` if system layout changed
- [ ] Update `docs/app-workflows.md` if user journeys changed
- [ ] If a Pydantic wire model changed: update `libs/spec/schemas/manifest/v1/` and run `make ts-types`
- [ ] Run `make test` and `make lint`
- [ ] Move plan to `docs/exec-plans/completed/`

## Bugfix

- [ ] Add failing test that reproduces the bug
- [ ] Confirm test fails
- [ ] Implement fix
- [ ] Run `make test` — confirm all green
- [ ] Update feature doc if behavior changed

## Refactor

- [ ] Create execution plan in `docs/exec-plans/active/`
- [ ] Run `make test` before starting (baseline)
- [ ] Make changes incrementally
- [ ] Run `make test` after each change
- [ ] Update docs if interfaces changed

## Documentation Update

- [ ] Edit the canonical doc (feature doc, ARCHITECTURE.md, etc.)
- [ ] Update `<!-- last_verified: YYYY-MM-DD -->` header
- [ ] Verify cross-links are correct
- [ ] No duplicate information across docs

## Pull Request

- [ ] All tests pass (`make test`)
- [ ] Linter passes (`make lint`)
- [ ] Docs updated in same PR as code changes
- [ ] Execution plan referenced if applicable
- [ ] PR description summarizes changes

## Testing

### Test types
- **Unit**: Pure logic — models, builders, canonical JSON, policy
- **Golden**: Round-trip verification — PNG embed/extract
- **CLI**: Command integration — extract, verify, replay, index

### Test placement
- Core unit: `libs/core/tests/unit/`
- Core golden: `libs/core/tests/golden/`
- CLI: `cli/tests/`
- Shared fixtures: `libs/core/tests/conftest.py`

### Commands
- Quick (relevant subset): `cd libs/core && pytest tests/unit/<test_file>.py -v`
- Full suite: `make test`
- Coverage: `make coverage` (70% minimum)

### When to run
- After behavior change: run relevant test subset
- Before PR: run full suite (`make test`)
- After refactor: run full suite

### Bugfix rule
1. Add failing test first
2. Confirm failure
3. Implement fix
4. Rerun tests until green

## Pre-release catalog verification

Provider catalogs rotate independently of the SDK (typically once per few months). Before tagging a release wave, manually verify that every slug shipped in `family.example_slugs`, the connector READMEs, and the root `README.md` still resolves on the upstream's published catalog.

This is a human-inspection step, not an automated probe. Upstream catalog pages are the authoritative source; their HTML/JSON is more current than any rate-limited API discovery endpoint.

### Provider catalog pages

| Provider | Where to verify slugs | Notes |
|----------|----------------------|-------|
| GMICloud | https://console.gmicloud.ai/ | Sign-in required. Check the "Models" section for live availability. |
| OpenAI | https://platform.openai.com/docs/models | Public docs; Sora / DALL-E / GPT-Image / TTS sections. |
| Google (Veo, Imagen, Gemini Image) | https://ai.google.dev/gemini-api/docs/models | Plus https://deepmind.google/technologies/veo/ for Veo specifics. |
| Replicate | https://replicate.com/explore | Public catalog; search by slug. |
| Runway | https://docs.dev.runwayml.com/ | API docs list supported `model` values for video generation. |
| Luma | https://docs.lumalabs.ai/ | Dream Machine API docs list supported model strings. |
| Decart | https://platform.decart.ai/ | Sign-in required for full catalog. |
| ElevenLabs | https://elevenlabs.io/docs/api-reference/models/list | Public docs. |
| Stability (audio) | https://platform.stability.ai/docs/api-reference | Stable Audio section. |
| LMNT | https://docs.lmnt.com/ | Public docs; voices + models. |
| NVIDIA NIM | https://build.nvidia.com/explore | Public catalog; live model listing. |

Keep this table aligned with `libs/connectors/*/` — when a new connector lands, add its provider's catalog link here in the same PR.

### Pre-release checklist

Before cutting any release wave that includes connector changes:

1. Open each connector's README quickstart. For every `model="..."` slug:
   - Confirm the slug appears in the corresponding family's `example_slugs` tuple (or `unstable_examples` if intentionally flagged flaky).
   - Click through to the provider's catalog page above. Confirm the slug is still listed.
2. Skim `examples/*_pipeline.py` for any `model=` references; same check.
3. If a slug is gone:
   - Prune it from the README quickstart.
   - Prune (or move to `unstable_examples`) the family's `example_slugs` entry.
   - Update any example scripts.

Total time: ~5 minutes per release.

### Optional programmatic sanity-check

Two probe tools exist in `tools/` if you want a programmatic confirmation alongside the manual click-through. They make real network calls and may incur small upstream cost (a few audit-log entries per run, occasionally a paid generation job if a provider's queue accepts the minimal probe payload — see each tool's docstring). Run manually before a release wave when you want an extra signal:

```bash
# Provider-agnostic: probes every registered provider whose
# GENBLAZE_PROBE_<NAME>_API_KEY env var is set; skips the rest.
python tools/probe_models.py

# GMICloud-specific: slug casing + wire-key + PixVerse coercer matrix.
export GMI_API_KEY="gmi-..."
python tools/probe_gmicloud_wire.py
```

Reports land in `docs/reference/`. `not_found` means the slug is dead and should be pruned; `unknown` means transient (re-run, don't prune).

### Discipline rule for README + example_slugs

Every slug used in any README quickstart, provider docstring, or `examples/*_pipeline.py` script **must also appear in its family's `example_slugs` tuple** (or `unstable_examples` for known-flaky slugs). Keeps the manual check above tractable — one source of truth per family.

Connector package READMEs are also PyPI long descriptions. Cross-file links in those READMEs must use absolute GitHub URLs so they resolve on PyPI; repository-relative links such as `../../../docs/...` fail the PyPI metadata gate. In-page anchors (`#usage`) and `mailto:` links are allowed.

## Doc Update Mapping

| Change Type | Update Location |
|-------------|-----------------|
| Feature logic/inputs/outputs | `docs/features/<feature>.md` |
| User journeys | `docs/app-workflows.md` |
| System layout/integrations | `ARCHITECTURE.md` |
| Dev/testing process | `docs/dev-workflows.md` |
| Setup or tech stack | `README.md` |
| Active work | `docs/exec-plans/active/` |
| Known tech debt | `docs/exec-plans/tech-debt-tracker.md` |
