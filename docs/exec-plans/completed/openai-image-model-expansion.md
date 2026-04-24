<!-- completed: 2026-04-24 -->
# OpenAI Image Model Expansion (`gpt-image-2` family + edits)

> **Shipped.** Full model lineup and `/v1/images/edits` live in `libs/connectors/openai/genblaze_openai/dalle.py`. `_ImageModelSpec` registry at `dalle.py:79-203`; edit endpoint routing at `dalle.py:546`. See CHANGELOG [0.1.0] "genblaze-openai" entries.

## Summary

Expand `genblaze-openai` image support to cover OpenAI's full image model lineup
(`gpt-image-2`, `gpt-image-1.5`, `gpt-image-1`, `gpt-image-1-mini`, `dall-e-3`,
`dall-e-2`) and add the `/v1/images/edits` endpoint. Refactor `dalle.py` internals
to the GMICloud-style registry pattern: one `dict[str, _ImageModelSpec]` replaces
the three disjoint lookup tables and drives capabilities, validation, pricing,
response-format handling, and endpoint routing.

## Motivation

OpenAI's April 2026 cookbook (*Image Gen Models Prompting Guide*) surfaced new
models and `/images/edits` capabilities. Current `DalleProvider` supports only
3 models and `/images/generations` — users can't hit `gpt-image-2` at all and
can't do reference-image editing through the SDK.

## Non-Goals

- No rename of `DalleProvider` class or `"openai-dalle"` identifier (user-visible
  break; defer to a separate PR if desired)
- No changes to `libs/core/` — all per-model variation fits in the connector
- No Responses API integration (`"type": "image_generation"` tool) — separate
  feature, not in this scope
- No inline pricing for `gpt-image-2` — OpenAI has not published public per-image
  rates; `cost_usd` stays `None` with a TODO until rates are disclosed
- No streaming/`partial_images` support — our `SyncProvider` lifecycle doesn't
  yield intermediates; add separately if prioritized

## Model Matrix (authoritative, sourced from developers.openai.com + openai-python SDK reference, 2026-04-22)

| Model | Edits (per SDK ref) | Response | Sizes | Quality | input_fidelity | transparent bg | mask |
|---|---|---|---|---|---|---|---|
| `gpt-image-2` | ✓ | b64_json | free-form | low/medium/high/auto | no-op (native HF) | ✗ | ✓ |
| `gpt-image-1.5` | ✓ | b64_json | fixed 3-set + auto | low/medium/high/auto | low/high | ✓ | ✓ |
| `gpt-image-1-mini` | ✓ | b64_json | fixed 3-set + auto | low/medium/high/auto | — | ✓ | ✓ |
| `gpt-image-1` | ✓ | b64_json | fixed 3-set + auto | low/medium/high/auto | low/high | ✓ | ✓ |
| `dall-e-3` | server rejects | URL | 1024/1792 | standard, hd | — | — | — |
| `dall-e-2` | ✓ (historical) | URL | 256/512/1024 | standard | — | — | — |

**Note on `chatgpt-image-latest`**: OpenAI's SDK reference lists this as an
accepted alias for edits. We do **not** hard-code it — `_DEFAULT_SPEC`
passthrough handles any unknown/alias model (cost=None, no size gate).
Aliases rotate; hard-coding them is a maintenance trap.

### `gpt-image-2` size constraints
- Max edge **< 3840 px**
- Both edges multiples of **16**
- Aspect ratio **≤ 3:1**
- Total pixels **655,360 – 8,294,400**

### Pricing (USD per image)

| Model | Low 1024² | Low 1536 | Med 1024² | Med 1536 | High 1024² | High 1536 |
|---|---|---|---|---|---|---|
| `gpt-image-1` | 0.011 | 0.016 | 0.042 | 0.063 | 0.167 | 0.250 |
| `gpt-image-1.5` | 0.009 | 0.013 | 0.034 | 0.050 | 0.133 | 0.200 |
| `gpt-image-1-mini` | 0.005 | 0.006 | 0.011 | 0.015 | 0.036 | 0.052 |
| `gpt-image-2` | — (undisclosed, `cost_usd=None` until rates published) |
| `dall-e-3` standard 1024² 0.040, 1792 0.080; hd 1024² 0.080, 1792 0.120 |
| `dall-e-2` 256² 0.016, 512² 0.018, 1024² 0.020 |

## Design

### Registry pattern (GMICloud-style)

Replace the three disjoint dicts (`_SIZE_BY_MODEL`, `_B64_ONLY_MODELS`,
`_PRICING`) with one frozen dataclass per model:

```python
@dataclass(frozen=True)
class _ImageModelSpec:
    response_format: Literal["b64_json", "url"]
    valid_qualities: frozenset[str]
    fixed_sizes: frozenset[str] | None             # None => free-form (gpt-image-2)
    supports_input_fidelity: bool                  # advisory only — soft-warns, doesn't reject
    pricing: dict[tuple[str, str], float] | None   # (quality, size) -> USD, None means unknown

_MODELS: dict[str, _ImageModelSpec] = { ... }
_DEFAULT_SPEC = _ImageModelSpec(...)   # permissive fallback — "unknown models pass through"
```

Fields dropped from spec (server is authority; we don't mirror capability matrices):
- `endpoints` — routing is driven by `step.inputs` presence alone; server rejects if unsupported
- `supports_background`, `supports_style` — passed through, server validates

Provider flow:
1. `spec = _MODELS.get(step.model, _DEFAULT_SPEC)` — unknown model → pass through, cost=None (matches GMICloud convention)
2. `_validate_params(step, spec)` — **structural validation only**: size (fixed set *or* `_validate_gpt_image_2_size`), quality against known enum, `output_compression` range (0–100). Soft-warn (log) on `input_fidelity` passed to a model that doesn't support it, per `spec.supports_input_fidelity`. No hard rejects for capability mismatches.
3. Endpoint routing: `step.inputs` present → `client.images.edit(...)`; otherwise `client.images.generate(...)`. No model gate — server is the authority.
4. Response handling: per `spec.response_format`
5. Cost: `spec.pricing[(quality, size)]` or `None`

`get_capabilities()` becomes:

```python
return ProviderCapabilities(
    supported_modalities=[Modality.IMAGE],
    supported_inputs=["text", "image"],
    accepts_chain_input=True,
    models=sorted(_MODELS),
    output_formats=["image/png", "image/jpeg", "image/webp"],
)
```

### Free-form size validator (`gpt-image-2`)

```python
def _validate_gpt_image_2_size(size: str) -> None:
    if size == "auto":
        return
    w, h = _parse_wxh(size)   # raises INVALID_INPUT on malformed
    if max(w, h) >= 3840:     raise ...("max edge must be < 3840px")
    if w % 16 or h % 16:      raise ...("both edges must be multiples of 16")
    ratio = max(w, h) / min(w, h)
    if ratio > 3.0:           raise ...("aspect ratio must be ≤ 3:1")
    pixels = w * h
    if not 655_360 <= pixels <= 8_294_400: raise ...("total pixels must be 655,360–8,294,400")
```

### Edit endpoint path

- Detect via `step.inputs` (first input becomes the `image` file; validate via existing `validate_chain_input_url`; support file:// and https://)
- `step.params["mask"]` (optional) — same URL scheme validation
- `step.params["input_fidelity"]` — soft-warn if `spec.supports_input_fidelity=False`, still pass to server
- For file:// inputs, open the file and pass a file handle to the SDK (`client.images.edit(image=open(...), ...)`); for https:// inputs, stream-download to temp then pass handle — reuses nothing currently; implement minimal `_open_image_source(url)` helper in `dalle.py`
- Multi-image (array) edits: `step.inputs` length > 1 → pass list of file handles to all models. Server rejects if unsupported for that model — no client-side model gate.

### New parameter passthroughs

Added to `params` dict forwarded to SDK:

- `output_format` — `png`/`jpeg`/`webp` (all gpt-image-\*)
- `output_compression` — 0–100 (jpeg/webp only; structural range check)
- `moderation` — `auto`/`low`
- `mask` — edit path only (file://, https://)
- `input_fidelity` — soft-warn on unsupported models, server-authoritative

Asset `media_type` now follows `output_format` (default `image/png`). File suffix
updated to match (`.png`/`.jpg`/`.webp`).

### Backward compatibility

- `DalleProvider` class name, `name = "openai-dalle"`, constructor signature, and `generate()` return shape unchanged
- Existing tests that pass `size`, `quality`, `style` to `dall-e-3` continue to pass
- Warning-log on `dall-e-*` temporary-URL expiry stays (URL response path)

## Implementation Steps

1. **Registry + new models** (dalle.py, ~120 LOC)
   - `_ImageModelSpec` dataclass
   - `_MODELS` dict with 6 entries + `_DEFAULT_SPEC`
   - Replace `_SIZE_BY_MODEL`, `_B64_ONLY_MODELS`, `_PRICING`, `_VALID_QUALITIES`
   - Refactor `_validate_params(step)` → `_validate_params(step, spec)`
2. **Free-form size validator** — `_parse_wxh`, `_validate_gpt_image_2_size`
3. **Edit endpoint** — detect `step.inputs`; `_open_image_source(url)`; `_maybe_open_mask(params)`; route via `client.images.edit` or `client.images.generate`; temp-file cleanup in a `finally`
4. **Output format handling** — set suffix + media_type from `output_format` param
5. **Capabilities update** — `accepts_chain_input=True`, `supported_inputs=["text","image"]`, `models=sorted(_MODELS)`, formats list
6. **Tests** (test_dalle_provider.py, ~150 LOC added)
   - Parametrize over `_MODELS` for quality/size validation
   - `gpt-image-2` free-form: 6+ constraint violations, 3 valid sizes
   - `input_fidelity` soft-warn (logged) on gpt-image-1-mini / gpt-image-2, no exception
   - Edit path: single image, multi-image, mask, file:// and https:// inputs
   - Edit path with `dall-e-3`: no client rejection; server error surfaces through
   - `output_format=webp` → `.webp` suffix + media_type `image/webp`
   - `output_compression` out-of-range rejection (structural)
   - Unknown-model passthrough (cost_usd None, no validation errors, edit route works)
   - Pricing matrix for each (model, quality, size) tuple in `_MODELS`
7. **Docs**
   - `README.md`: OpenAI row → `DALL-E / gpt-image family (2/1.5/1/1-mini)`
   - `ARCHITECTURE.md` line 8 and line 57: update connector summaries
   - `CHANGELOG.md`: new entry under unreleased
   - `docs/features/provider-system.md`: brief note that `openai-dalle` now supports image inputs via edit endpoint

## Risks / Mitigations

- **Unverified model IDs**: `gpt-image-2-2026-04-21` snapshot and `gpt-image-1.5-2025-12-16` snapshot were pulled from developers.openai.com. Spot-check once more before merging. Mitigation: registry keyed by base IDs only; snapshot IDs handled via unknown-model passthrough.
- **Contradictory edit-endpoint docs**: The guide matrix marks edits as ✗ for `gpt-image-1` / `gpt-image-1-mini`, but both the per-model pages AND the openai-python SDK reference's `images.edit` model enum list all four gpt-image-\* models as supported. We side with the SDK reference (canonical) and adopt server-authoritative routing: no client-side model→endpoint gate at all. If the server rejects, the 4xx propagates via our existing `INVALID_INPUT` mapping.
- **gpt-image-2 pricing unknown**: explicit TODO; `cost_usd=None` is a known acceptable outcome elsewhere in the SDK. Caller-side budgeting unaffected (they've had to handle None for unknown models).
- **Temp file leaks on edit path**: wrap in `try/finally` with `os.unlink` for downloaded https:// inputs; file:// inputs remain untouched.
- **Param-validation churn for existing tests**: `_validate_params` signature changes — tests that instantiate validators directly will need the new `spec` arg. Confirmed only internal use via grep.

## Sign-off checklist

- [ ] Exec plan reviewed
- [ ] Model matrix spot-checked against developers.openai.com
- [ ] `make test` green for `libs/connectors/openai/`
- [ ] `make lint` green
- [ ] `make test` green across all packages
- [ ] README + ARCHITECTURE + CHANGELOG + provider-system.md updated in same PR
- [ ] Move this file to `docs/exec-plans/completed/` on merge

## References

- [OpenAI Image Generation Guide](https://developers.openai.com/api/docs/guides/image-generation)
- [gpt-image-2 model](https://developers.openai.com/api/docs/models/gpt-image-2)
- [gpt-image-1.5 model](https://developers.openai.com/api/docs/models/gpt-image-1.5)
- [gpt-image-1 model](https://developers.openai.com/api/docs/models/gpt-image-1)
- [gpt-image-1-mini model](https://developers.openai.com/api/docs/models/gpt-image-1-mini)
- [Image gen prompting guide](https://developers.openai.com/cookbook/examples/multimodal/image-gen-models-prompting-guide) (originating source for this work)
- [openai-python `images.edit` reference](https://developers.openai.com/api/reference/python/resources/images/methods/edit) (canonical model list for edits)
- [OpenAI API changelog](https://developers.openai.com/api/docs/changelog) (`input_fidelity` support matrix)
