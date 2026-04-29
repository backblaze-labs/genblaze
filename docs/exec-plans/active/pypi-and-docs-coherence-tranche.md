<!-- created: 2026-04-29 -->
# PyPI and docs coherence

**Status:** active · **Owner:** docs subagent (can start NOW, parallel with all code plans) · **Target releases:** every published Python package patch-bumped; docs-only npm `@genblaze/spec` no-op · **Shape:** D (docs) + A (small additive code: version helper, umbrella `__getattr__` fix) · **Feedback refs:** P1-18 (docs cliff), P3-14 (PyPI metadata, partly R-10), version-drift bug (8 reports), umbrella `__getattr__` walks into pyarrow

## Goal

Eliminate every "first-impression trap" between `pip install genblaze` and a working pipeline. Single source of `__version__`, no version skew. Every PyPI page renders the README. Hosted docs site at `https://backblaze-labs.github.io/genblaze/`. Every README/docs example matches runtime. B2 SLA / region / cost / compliance copy on the storage page.

**Done when:** `genblaze.__version__ == importlib.metadata.version("genblaze") == user-agent version` for every published package; PyPI pages render long descriptions, project URLs, classifiers, CHANGELOG link; `mkdocs serve` runs locally and `gh-pages` is auto-deployed via GitHub Actions; `from genblaze import ParquetSink` raises `OptionalDependencyError("install genblaze[parquet]")` instead of `ModuleNotFoundError: pyarrow`; `make docs-check` parses every example in the docs.

## Subagent brief

### Engineering posture

You are an expert technical writer + Python packaging specialist working on a 14-package monorepo. Production-grade documentation: every code example is runnable; every PyPI page renders cleanly on mobile and desktop; every link resolves. No marketing fluff — concrete, accurate, current. The reader is a senior engineer evaluating adoption.

### Required reading (in order)

1. `AGENTS.md`, `ARCHITECTURE.md`, `CLAUDE.md`, `README.md`, `CHANGELOG.md`
2. `docs/exec-plans/feedback.md` — "P1-18", "P3-14", "P3-04", "P3-06", "P3-07", "P3-09", "P3-13"
3. Every package's `pyproject.toml` (14: `libs/core/`, `libs/connectors/*` ×11, `libs/meta/`, `cli/`, `libs/spec/` though spec is npm-only)
4. `libs/core/genblaze_core/__init__.py` — current `__version__` definition
5. `libs/connectors/s3/genblaze_s3/backend.py:22` — hardcoded user-agent base
6. `libs/meta/genblaze/__init__.py` — umbrella package; the lazy `__getattr__` is the pyarrow leak
7. `docs/features/object-storage.md`, `docs/features/manifest-provenance.md` — current feature docs
8. README quickstart sections for each connector
9. `examples/*.py` — every example must run after the docs sweep

### Success bar (review gate)

- **Bugs**: every code block in every doc parses with `ast.parse`; every link resolves (file path on disk or HTTP 200); every example imports cleanly under `pip install genblaze` (umbrella) without provider-specific extras unless explicitly noted.
- **Duplication**: do not fork content between README and docs site — README is the canonical short version, docs site links to expanded recipes. CHANGELOG is single source for release notes.
- **Performance**: docs site builds in <30s; PyPI pages don't pull in giant assets.
- **Scalability**: doc structure scales to 20+ connectors without restructuring; per-connector pages template from a shared layout.
- **Pattern-fit**: mkdocs-material is standard for Python OSS (LangChain, FastAPI, Pydantic). Don't invent a custom static-site generator.

## Phase 1 — Code coherence (Wk 1, parallelizable with all code work) [SMALL]

### Single source of `__version__`

| File | Change |
|------|--------|
| `libs/core/genblaze_core/_version.py` | **NEW.** `__version__: str = importlib.metadata.version("genblaze-core")`; falls back to `"0.0.0+unknown"` if metadata absent (editable install during dev). |
| `libs/core/genblaze_core/__init__.py` | Replace inline version constant with `from ._version import __version__`. |
| `libs/connectors/*/genblaze_*/__init__.py` (×11) | Same pattern, reading own dist version. |
| `libs/connectors/s3/genblaze_s3/_user_agent.py` | **NEW.** `build_user_agent(*, base: str \| None = None, extra: str \| None = None) -> str` reads from `genblaze_core._version`. Used by `S3StorageBackend` (replaces hardcoded `_USER_AGENT` at `backend.py:22`). Composes with Plan 1's `StorageConfig.user_agent_extra`. |
| `libs/meta/genblaze/__init__.py` | `__version__ = importlib.metadata.version("genblaze")` (umbrella's own version). Re-export top-level surface from `genblaze_core` (closes feedback P2-01). |
| `libs/core/tests/unit/test_version_coherence.py` | **NEW.** Asserts `__version__` matches `importlib.metadata.version(...)` for every installed package. Asserts user-agent built from same source. |

### Umbrella `__getattr__` doesn't walk into optional deps

| File | Change |
|------|--------|
| `libs/core/genblaze_core/_optional.py` | **NEW.** `OptionalDependencyError(ImportError)`; `require(extra: str, module: str)` helper. Reused across packages. |
| `libs/meta/genblaze/__init__.py` | Lazy `__getattr__` raises `OptionalDependencyError("install genblaze[parquet] for ParquetSink")` when `pyarrow` absent — not `ModuleNotFoundError`. Same for any optional-dep-gated symbol. |
| `libs/core/tests/unit/test_optional_imports.py` | **NEW.** `from genblaze import ParquetSink` raises `OptionalDependencyError` (subclass of `ImportError`, so legacy `except ImportError:` still catches) when pyarrow absent. |

## Phase 2 — PyPI metadata audit (Wk 1) [DOCS]

For each of 14 published packages (`genblaze-core`, `genblaze` umbrella, `genblaze-s3`, `genblaze-cli`, 11 connectors, `genblaze-langsmith`):

| Field | Required value |
|-------|----------------|
| `description` | One sentence, ≤120 chars, what the package does |
| `readme` | Per-package `README.md` (audit which ones link to root); populate with package-specific quickstart + overview |
| `long_description_content_type` | `"text/markdown"` |
| `authors` | `[{name = "Backblaze, Inc.", email = "..."}]` |
| `license` | `{text = "MIT"}` (matches LICENSE) |
| `requires-python` | `">=3.11"` |
| `classifiers` | Include `License :: OSI Approved :: MIT License`, `Programming Language :: Python :: 3.11/3.12/3.13`, `Topic :: Multimedia`, `Topic :: Software Development :: Libraries`, `Development Status :: 4 - Beta` |
| `project_urls` | `Homepage`, `Documentation` (hosted docs site, not README), `Source`, `Issues`, `Changelog` (link to `CHANGELOG.md` anchor) |
| `keywords` | Provider-relevant + `provenance`, `manifest`, `c2pa-ready`, `genai`, `pipeline` |

| File | Change |
|------|--------|
| `libs/{core,meta,cli,connectors/*}/pyproject.toml` (×14) | Apply table above |
| `tools/check_pypi_metadata.py` | **NEW.** CI script asserts every published package has the required fields, consistent shape |
| `.github/workflows/ci.yml` | Add `pypi-metadata-check` job |

## Phase 3 — Hosted docs site (Wk 2) [DOCS]

| File | Change |
|------|--------|
| `mkdocs.yml` | **NEW.** mkdocs-material config; nav covers Getting Started → Concepts → Features → Connectors → API Reference → Migrations → Changelog |
| `docs/index.md` | **NEW.** Landing — value prop, quickstart, links into the rest |
| `docs/getting-started/{install,first-pipeline,storage-only}.md` | **NEW.** Includes new "storage-only quickstart" — use `genblaze-s3` as a standalone B2 client without any Pipeline |
| `docs/connectors/{openai,google,…}.md` (×11) | Per-connector page: PyPI install, env vars, supported models, modalities, capability matrix, example |
| `docs/compliance/{hipaa,sse-kms,phi-baa}.md` | **NEW.** B2 BAA availability; SSE-C/SSE-KMS configuration recipes; PHI handling guidance; what genblaze does and doesn't do for compliance |
| `docs/storage/{regions,sla,cost-guidance}.md` | **NEW.** B2 vs S3 region matrix; B2 SLA; B2 lacks S3 storage classes (no Glacier/IA tiers — different cost model); throughput/cost guidance |
| `.github/workflows/docs.yml` | **NEW.** Build mkdocs on every PR; deploy to `gh-pages` on merge to main |
| `tools/docs_check.py` | **NEW.** Parses every Python code block in `docs/**/*.md` with `ast.parse` to catch silent breakage; runs in CI |

## Phase 4 — README + runtime drift sweep (Wk 2) [DOCS]

| File | Change |
|------|--------|
| `README.md` | Audit every code block against runtime; fix `access_key_id` → `aws_access_key_id` drift; inline 5–6 most-referenced recipes (overlaps with master Wave 7B — coordinate); resolve cross-links to absolute GitHub URLs (so PyPI renders them) |
| `libs/core/MANIFEST.in` (or `pyproject.toml` `include`) | Ship `examples/` and `docs/features/` inside the wheel per master P1-18 |
| `tools/docs_runtime_drift.py` | **NEW.** Walks every example in `examples/` + every code block in README/docs, ensures `inspect.signature` matches the call sites |

## Cross-plan dependencies

- **Phase 1 (code coherence) blocks on** master-plan Wave 0.2 (minimal-install CI smoke).
- **Phase 1 user-agent helper used by** Plan 1's `StorageConfig.user_agent_extra` default.
- **Phase 4 `examples/` wheel inclusion overlaps with** master Wave 7B — coordinate to land once.
- **Compliance/SSE docs (Phase 3)** reference Plan 1's symmetric `Encryption` value object — write the docs after Plan 1 ships, or reference the planned shape with a "0.3.0+" callout.

## Acceptance gates

- [ ] `tools/check_pypi_metadata.py` passes for all 14 packages
- [ ] `tools/docs_check.py` passes (every code block parses)
- [ ] `tools/docs_runtime_drift.py` passes (every signature matches runtime)
- [ ] `mkdocs serve` runs cleanly; site deployed to `gh-pages`
- [ ] `from genblaze import ParquetSink` raises `OptionalDependencyError` not `ModuleNotFoundError`
- [ ] `genblaze.__version__ == importlib.metadata.version("genblaze")` for every package (test asserts)
- [ ] PyPI pages render README content (manual check across all 14 pages)
- [ ] `make test && make lint && make typecheck` green

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| 14-package metadata drifts again | CI gate `pypi-metadata-check` enforces consistency on every PR |
| Hosted docs site becomes stale | `last_verified` headers + monthly `verify-docs` skill run |
| `OptionalDependencyError` is a behavioral break for callers expecting `ImportError` | `OptionalDependencyError(ImportError)` — subclass of ImportError so `except ImportError:` still catches |
| README inline-recipe copy diverges from docs site | Single source: code blocks in `examples/` are canonical; both render from there |
| Subagent over-writes existing docs unnecessarily | Review gate: only edit pages flagged in the audit; do not rewrite stable pages without justification |

## Out of scope

- Hosted docs custom domain — `gh-pages` default URL is fine for v1
- API reference auto-generation — Phase 5 follow-up via `mkdocstrings`
- Translated docs — single English version is enough
- B2 management API docs — depend on the future `b2-management-surface.md` plan
