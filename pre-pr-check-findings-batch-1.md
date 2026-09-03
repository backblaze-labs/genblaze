# Pre-PR Check — Batch 1

Issue: #233 — genblaze-google README/examples quickstart uses delisted `imagen-3.0-*` slugs; GeminiImageProvider undocumented

**Acceptance criterion:** The README quickstart and `examples/imagen_pipeline.py` must reference a catalog-listed Imagen slug (not the delisted `imagen-3.0-*`) so pipeline preflight no longer raises `NOT_FOUND`/`DEAD`, and `GeminiImageProvider` must be discoverable from the README and `docs/features/provider-system.md`.

**Tier:** Full 6-check sequence (first pass).

## Check results

1. Claude — code review: no findings.
2. Claude — security review: no findings (docs/example/test-only diff; no new injection, auth, secret, or SSRF surface).
3. Claude — test/build verification: PASS
   - `cd libs/connectors/google && pytest tests/ -q` → 140 passed, 10 skipped
   - `ruff check libs/ cli/ examples/` → All checks passed
   - `ruff format --check` → 17 pre-existing drift files, confirmed present on `main` before this change; no new drift
   - `deptry .` (google package) → Success, no dependency issues
4. Codex — review (standard pass): 1 finding (below)
5. Codex — security-focused review: no findings
6. Codex — adversarial review: 2 findings grouped under one root cause (below), plus 1 low-severity/deferred item

## BLOCKING findings

### Finding 1
- **Timestamp:** 2026-09-03
- **Found by:** Check 4 (Codex — standard review)
- **Classification:** BLOCKING
- **Severity:** MEDIUM
- **File:** `docs/features/provider-system.md:112,118`
- **Root-cause signature:** `provider-system.md` / connector-inventory prose / mentions the *modality* "Gemini-image" informally but never names the actual class `GeminiImageProvider`, so a reader searching this doc for the class name won't find it.
- **Description:** The `PARTIAL` tier list and the `map_google_error` consumer list both say "Gemini-image" but not `GeminiImageProvider`. The acceptance criterion explicitly requires `GeminiImageProvider` to be discoverable from this file; a generic modality label doesn't satisfy "discoverable" for someone grepping/searching for the class.
- **Resolution criterion:** `docs/features/provider-system.md` contains the literal string `GeminiImageProvider` at least once, in both the `PARTIAL` tier list and the `map_google_error` consumer list (or a single unambiguous reference that covers both).

### Finding 2 (two symptoms, one root cause)
- **Timestamp:** 2026-09-03
- **Found by:** Check 6 (Codex — adversarial review)
- **Classification:** BLOCKING
- **Severity:** MEDIUM
- **File:** `libs/connectors/google/tests/test_catalog_decoupling.py:315-339` (`TestExampleScriptUsesLiveSlugs.test_imagen_example_uses_current_family_slugs`)
- **Root-cause signature:** The new doc-drift guard test does a full-source substring search for *any* live slug from *either* family, rather than parsing/asserting the actual `model=` argument passed to `ImagenProvider` in the example. This makes the test both too loose (passes if a Gemini-image slug appears anywhere, even if the Imagen call itself is still stale) and easily defeated (a live slug left in a comment/docstring would satisfy it even if the real invocation used a different, delisted slug — the negative assertions only catch the two specific `imagen-3.0-*` strings already known today).
- **Description (symptom a):** `live_slugs` unions `GOOGLE_IMAGEN_FAMILY.example_slugs` and `GOOGLE_GEMINI_IMAGE_FAMILY.example_slugs`, so the assertion can pass on a Gemini-image slug even though the acceptance criterion is specifically about the Imagen quickstart using a live Imagen slug.
- **Description (symptom b):** The assertion is `any(f'model="{slug}"' in source for slug in live_slugs)` — a raw substring scan of the whole file, not tied to the actual `.step(ImagenProvider(...), model=...)` call site. It doesn't verify that *this specific provider's* invocation uses a live slug.
- **Resolution criterion:** The test parses (or otherwise reliably locates) the `model=` keyword argument passed to `ImagenProvider(...)` specifically in `examples/imagen_pipeline.py`, and asserts that value is a member of `GOOGLE_IMAGEN_FAMILY.example_slugs` — not a same-file substring match against the union of both families.

## Deferred / Invalid (not blocking)

- **Codex adversarial, `test_catalog_decoupling.py:330`** — the new guard test only covers `examples/imagen_pipeline.py`, not the README quickstart, even though the README was part of the original breakage. **Severity: LOW** → does not meet the BLOCKING bar (HIGH/MEDIUM only) per pre-pr-check classification rules. **Resolved anyway** — folded into the Finding 2 fix below at no extra cost (same test function, added a second test for the README quickstart).
- **Pre-existing, out of scope (noted before this check ran):** `docs/reference/model-matrix.md:107-108` and `docs/reference/pricing-recipes.md:667-668` still list `imagen-3.0-*` in pricing tables. Not part of issue #233's stated acceptance criteria (Imagen pricing was already phased out of the registry — `TestPricingPhaseOut`). Deferred to a follow-up issue.
- **Codex adversarial (recheck pass), `test_catalog_decoupling.py:371`** — `_extract_imagen_model_slug` returns the *first* matching `ImagenProvider` `.step(...)` call without checking for a second one; an earlier dummy call using a live slug could theoretically mask a later stale quickstart call. **Severity: LOW**, in-scope but below the BLOCKING bar. Not applicable today (each file has exactly one `ImagenProvider` quickstart), logged for awareness only.

## Resolution — Fixes Applied

1. **Finding 1** — `docs/features/provider-system.md` now names the classes explicitly: `` `VeoProvider` / `ImagenProvider` / `GeminiImageProvider` `` in both the `PARTIAL` tier list and the `map_google_error` consumer list.
2. **Finding 2** — Rewrote `TestExampleScriptUsesLiveSlugs` in `libs/connectors/google/tests/test_catalog_decoupling.py`: added `_extract_imagen_model_slug()`, an AST-based helper that locates the specific `.step(<ImagenProvider instance>, model=...)` call (resolving both an inline `ImagenProvider(...)` and a variable bound earlier) and asserts that exact slug is a member of `GOOGLE_IMAGEN_FAMILY.example_slugs` — not a same-file substring scan across both families. Split into two tests: one for `examples/imagen_pipeline.py`, one for the README's Imagen quickstart code fence (closing the deferred low-severity README-coverage gap for free).

## Recheck — Full 6-check sequence (2nd pass, tier: full — diff exceeded quick-recheck size)

1. Claude — code review: no new findings.
2. Claude — security review: no findings (`ast.parse` doesn't execute code; no new auth/secret/injection/SSRF surface).
3. Claude — test/build verification: PASS
   - `cd libs/connectors/google && pytest tests/ -q` → 141 passed, 10 skipped
   - `ruff check` / `ruff format --check` (touched files) → clean
   - Confirmed both new tests fail against `main`'s pre-fix content (extract `imagen-3.0-generate-002`, not in `GOOGLE_IMAGEN_FAMILY.example_slugs`) — not tautological.
4. Codex — review: confirmed both prior findings fixed; no new findings.
5. Codex — security-focused review: no findings.
6. Codex — adversarial review: 1 new LOW-severity note (logged above, not blocking).

Zero BLOCKING findings in this pass.

RESOLVED: all clear
