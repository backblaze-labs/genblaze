<!-- created: 2026-04-29 -->
# PyPI and docs coherence

**Status:** Phases 1A/1B/1C/2 complete (2026-04-30); Phase 3 dropped (no hosted docs site); Phase 4 mostly absorbed elsewhere — see red-team below · **Owner:** docs subagent · **Target releases:** every published Python package patch-bumped; docs-only npm `@genblaze/spec` no-op · **Shape:** D (docs) + A (small additive code: version helper, umbrella `__getattr__` fix) · **Feedback refs:** P1-18 (docs cliff), P3-14 (PyPI metadata, partly R-10), version-drift bug (8 reports), umbrella `__getattr__` walks into pyarrow

## Goal

Eliminate every "first-impression trap" between `pip install genblaze` and a working pipeline. Single source of `__version__`, no version skew. Every PyPI page renders the README cleanly with full metadata. `from genblaze import ParquetSink` fails with an actionable error rather than a bare ImportError.

**Done when:** `genblaze.__version__ == importlib.metadata.version("genblaze") == user-agent version` for every published package; PyPI pages render long descriptions, project URLs, classifiers, keywords, CHANGELOG link; `from genblaze import ParquetSink` raises `OptionalDependencyError("install genblaze[parquet]")` instead of `ModuleNotFoundError: pyarrow`; `make pypi-metadata-check` is green.

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
- **Pattern-fit**: standard Python packaging idioms (`importlib.metadata` for versions, classifier set every PyPI consumer expects). Don't invent novel infrastructure.

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

## Phase 3 — Hosted docs site **[DROPPED 2026-04-30]**

We will **not** ship a `mkdocs`-hosted docs site at `https://backblaze-labs.github.io/genblaze/`. The README + per-feature docs in `docs/features/*.md` rendered on GitHub remain the canonical user-facing docs surface. All references to `mkdocs.yml`, `docs/index.md`, `docs/connectors/*.md`, `docs/compliance/*.md`, `docs/storage/*.md`, `docs/getting-started/*.md`, `.github/workflows/docs.yml`, and `tools/docs_check.py` (mkdocs build helper) are removed.

The compliance / region / SLA / storage-only-quickstart content that was queued for the docs site can land as new pages under `docs/features/` if and when the demand materializes — but is **not** in scope for this tranche.

## Phase 4 — README + runtime drift sweep **[RED-TEAMED 2026-04-30 — most items dropped]**

The original Phase 4 proposed five workstreams. After auditing the current state of the code and per-package READMEs, four are stale, wrong-premise, or net-negative. Verdicts:

### Item 1 — README code-block audit; fix `access_key_id` → `aws_access_key_id` drift
**VERDICT: DROP.** Already fixed. `S3StorageBackend.__init__` accepts both `aws_access_key_id` and `access_key_id` as aliases (`backend.py:181-207`, landed during Plan 1 storage hardening). Spot-checked: README:92 imports match `examples/quickstart.py:27-29` exactly. Existing `ruff check libs/ cli/ examples/` (`.github/workflows/ci.yml:24`) catches any drift past a public symbol on every PR.

### Item 2 — Inline 5–6 most-referenced recipes into README
**VERDICT: DROP.** README is already 435 lines with ~13 code blocks (Quickstart + Storage + 4 More-examples + Iteration + Embed + CLI + Custom-models). Inlining more would bloat first-screen value and *increase* drift surface. The pattern at README:123 — `> Runnable copy: examples/quickstart.py` — is the right one: keep README scannable, link to runnable files.

### Item 3 — Resolve cross-links to absolute GitHub URLs (so PyPI renders them)
**VERDICT: DROP — wrong premise.** PyPI pages for `genblaze` and `genblaze-core` render `libs/meta/README.md` and `libs/core/README.md` respectively, NOT the root README. Both per-package READMEs already use absolute URLs only (every link verified — all `https://github.com/…`, `https://pypi.org/…`, or `https://www.backblaze.com/…`). The root README's relative links render correctly on GitHub, the only place it's shown.

### Item 4 — Ship `examples/` and `docs/features/` inside the wheel
**VERDICT: DROP — anti-pattern.** No major Python SDK does this (boto3, openai, anthropic, langchain, pydantic — all link to GitHub-hosted content; none bundle docs in wheels). Bloats every install by ~3–400 KB of files 99% of users never read, and freezes docs at install time so they drift from the live README on GitHub — the *opposite* of "coherence." The "P1-18 docs cliff" this cited was about README accuracy + PyPI metadata; both addressed by Phases 1A/1B/2.

### Item 5 — `tools/docs_runtime_drift.py` (inspect.signature drift detector)
**VERDICT: DROP the proposed implementation.** The pipeline (extract markdown blocks → AST parse → resolve imports symbolically → bind literals to parameter names → handle fluent chains) is fragile and high-maintenance, for a class of drift that already gets caught by `ruff check libs/ cli/ examples/` on every PR (since `examples/*.py` are real Python files). Optional cheap follow-up — file under "nice-to-have, not blocking":

| File | Optional change (only if a real failure motivates it) |
|------|--------------------------------------------------------|
| `Makefile` | New `smoke-examples` target: `python -m py_compile examples/*.py` plus run the three offline examples (`quickstart_local.py`, `streaming_local.py`, `agent_loop_local.py`) which require no network access |

That's the entire delta worth considering. **Recommendation:** close Phase 4 with no further work unless a concrete README/docs drift incident surfaces.

## Cross-plan dependencies

- **Phase 1 (code coherence) blocks on** master-plan Wave 0.2 (minimal-install CI smoke). _Done._
- **Phase 1 user-agent helper used by** Plan 1's `StorageConfig.user_agent_extra` default. _Done._

## Acceptance gates

- [x] `tools/check_pypi_metadata.py` passes for all 16 published pyprojects
- [x] `from genblaze import ParquetSink` raises `OptionalDependencyError` not `ModuleNotFoundError`
- [x] `genblaze.__version__ == importlib.metadata.version("genblaze")` for every package (asserted by `test_version_coherence.py` for core + umbrella + 13 connectors)
- [ ] PyPI pages render the per-package README content (manual check across all 14 pages — pending next release)
- [x] `make test && make lint && make typecheck` green

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| 14-package metadata drifts again | CI gate `make pypi-metadata-check` enforces consistency on every PR |
| `OptionalDependencyError` is a behavioral break for callers expecting `ImportError` | `OptionalDependencyError(ImportError)` — subclass of `ImportError`, so `except ImportError:` still catches |
| Future contributor adds a 14th connector with hardcoded `__version__` | `TestConnectorVersionCoherence` parametrizes over every `genblaze-*` distribution — adding a connector that drifts fails CI |

## Out of scope

- Hosted docs site (mkdocs / gh-pages) — explicitly dropped above
- API reference auto-generation
- Translated docs
- B2 management API docs — depend on the future `b2-management-surface.md` plan
