<!-- last_verified: 2026-04-25 -->
# GMI registry reconciliation

**Status:** in-progress · **Owner:** core · **Target release:** `genblaze-gmicloud 0.2.4`
· **Shape:** F (fix-in-place) + B (some default IDs removed) · **Feedback ref:** P0-03, item #4

## Problem

Field reports from a 10-agent app build show the GMICloud model registry is
materially misaligned with the live request queue:

- **Audio:** every default TTS/music model id 404s on `POST /requests`
  (`Inworld-TTS-1.5-Mini`, `MiniMax-TTS-Speech-2.6-Turbo`, `ElevenLabs-TTS-v3`,
  `MiniMax-Voice-Clone-Speech-2.6-HD`, `MiniMax-Music-2.5`). The audio
  surface ships with **zero** working defaults.
- **Video:** ~30% of registered ids 404 (`kling-text2video-v2.1-master`,
  `veo3-fast`, `vidu-q1`, `minimax-hailuo-2.3-fast`).
- **Image:** Bria inpaint models (`bria-genfill`, `bria-eraser`) are
  registered but the shared `_COMMON_ALLOWLIST` strips `mask` / `mask_url`,
  rendering them unusable through the typed path.
- **Video:** Pixverse models (`pixverse-v5.6-*`) require `quality` per the
  upstream API, but the shared allowlist strips it — the model is unusable.
- **All providers:** `GMICloudBase.__init__` does not forward the documented
  `models=` kwarg, so `GMICloudVideoProvider(models=reg)` raises `TypeError`
  (P0-03).

## Resolution

Three coordinated changes, shipping together in `gmicloud 0.2.4`:

1. **Per-model `ParamSurface`** (depends on `provider-standardization-tranche`
   Phase 0) — replace `_COMMON_ALLOWLIST` with composable per-model
   allowlists. Pixverse gets `quality`; Bria inpaint gets `mask`/`mask_url`;
   voice-clone gets `language`/`pitch`/`emotion`.
2. **Reconcile defaults against the live API** — every default `model_id` in
   `models/{audio,image,video}.py` must round-trip through `probe_model()`.
   Failing IDs move to `deprecated_aliases` (if there's a known rename) or
   are removed from defaults (with a one-release deprecation note in
   CHANGELOG).
3. **Forward `models=` through `GMICloudBase.__init__`** — one-line fix; the
   cross-provider conformance test from Phase 0 prevents regression.

## Files touched

```
libs/connectors/gmicloud/genblaze_gmicloud/_base.py            # accept models=
libs/connectors/gmicloud/genblaze_gmicloud/models/audio.py     # ParamSurface, dead-ID purge
libs/connectors/gmicloud/genblaze_gmicloud/models/image.py     # ParamSurface, Bria mask, Reve params
libs/connectors/gmicloud/genblaze_gmicloud/models/video.py     # ParamSurface, Pixverse quality
libs/connectors/gmicloud/genblaze_gmicloud/CHANGELOG.md        # deprecation notes
libs/connectors/gmicloud/tests/                                # update fixtures
```

Note: `voices.py` and `preflight_auth` / `probe_model` implementations live
in `provider-standardization-tranche.md` Phase 3 and ship in the same release
but track separately because they're additive features, not reconciliation.

## Reconciliation table

The following defaults must be probed before release. Defaults that 404 are
either renamed (move to `deprecated_aliases`) or removed (drop with note).

| Model id | Modality | Live status (last observed) | Action |
|---|---|---|---|
| `ElevenLabs-TTS-v3` | audio | 404 | **remove** — no rename available; document in CHANGELOG |
| `MiniMax-TTS-Speech-2.6-Turbo` | audio | 404 | **remove** pending upstream confirm |
| `MiniMax-Voice-Clone-Speech-2.6-HD` | audio | 404 | **remove** pending upstream confirm |
| `Inworld-TTS-1.5-Mini` | audio | 404 | **remove** pending upstream confirm |
| `MiniMax-Music-2.5` | audio | 404 | **remove** pending upstream confirm |
| `kling-text2video-v2.1-master` | video | 404 | **remove** — only image2video is live |
| `veo3-fast` | video | 404 | **remove** — only `veo3` is live |
| `vidu-q1` | video | 404 | **remove** pending upstream confirm |
| `minimax-hailuo-2.3-fast` | video | 404 | **remove** pending upstream confirm |
| `pixverse-v5.6-*` | video | 200 (with `quality`) | **keep**, add `quality` to allowlist |
| `bria-genfill`, `bria-eraser` | image | 200 (with `mask_url`) | **keep**, add `mask`/`mask_url` |

> **Probe re-run:** before merging this plan's PR, the maintainer reruns
> `tools/probe_models.py --provider gmicloud` against staging credentials and
> updates the table above. Status `404` rows that flip to `200` move to the
> "keep" actions instead of removal.

## Acceptance criteria

- [ ] `tools/probe_models.py --provider gmicloud` passes (no `NOT_FOUND` for
      any default `model_id`).
- [ ] `GMICloudVideoProvider(models=reg)` constructs without error
      (covered by Phase 0 conformance test).
- [ ] `Pipeline.step(GMICloudVideoProvider(), model="pixverse-v5.6-t2v",
      quality="720p", ...)` no longer drops `quality` (covered by new unit test).
- [ ] `Pipeline.step(GMICloudImageProvider(), model="bria-genfill",
      mask_url="https://...", ...)` forwards `mask_url` (covered by new unit
      test).
- [ ] `make test` green for `genblaze-gmicloud`.
- [ ] CHANGELOG entry under `### Removed` lists every default model id that
      was dropped, with rename hint when one exists.

## Sequencing

This plan **blocks on** Phase 0 of `provider-standardization-tranche.md`
landing — `ParamSurface` and `BaseProvider.probe_model()` are upstream
dependencies. Once Phase 0 ships in `genblaze-core 0.3.0`, this plan lands as
`genblaze-gmicloud 0.2.4`. They co-release.

---

## Wire-conformance probe (added 2026-04-25)

A second-batch sample-app feedback report flagged three GMICloud-specific drift
points that the existing reconciliation table doesn't surface — they're about
**param wire-keys and slug case**, not about whether the model id resolves at
all. The existing `tools/probe_models.py` only confirms 200/404 on submit; it
doesn't compare param-name vocabulary or test multiple slug casings.

**Feedback ref:** F-2026-04-25-08, F-2026-04-25-12, F-2026-04-25-13.

### Drift points to verify

| F-id | Family | Reporter claim | Current registry | Live-API verdict |
|---|---|---|---|---|
| F-2026-04-25-08 | `pixverse-v5.6-*` | `duration` is a string enum (`"5"`/`"8"`/`"10"`) | `_VIDEO_BASE.with_coercers(duration=int)` forces int | **TBD** — probe must submit with both `duration=5` (int) and `duration="5"` (string) and compare |
| F-2026-04-25-12 | `kling-image2video-v2.1-master`, `wan2.6-i2v`, `pixverse-v5.6-i2v` | Each wants a different image wire-key (`image` / `img_url` / `image_url`) | `_VIDEO_BASE` allowlists both `image` and `image_url`, no per-model rename | **TBD** — probe must submit with each candidate key against each i2v variant |
| F-2026-04-25-13 | `Kling-Image2Video-V2.1-*`, `Kling-Text2Video-V2.1-*`, `Veo3`, `Veo3-Fast`, `Sora-2-Pro`, `Luma-Ray-2`, `Minimax-Hailuo-2.3-Fast` | Live API only accepts PascalCase (R-06 rewrote them lowercase) | All seven canonicalized to lowercase (CHANGELOG [0.2.2]) | **TBD** — probe must submit with both casings for each named family |

### Tool: `tools/probe_gmicloud_wire.py`

A targeted probe distinct from the existing `tools/probe_models.py`. Inputs:
`GMI_API_KEY` (required) and a small fixture asset URL (an existing public B2
object — keeps the probe runnable from CI without uploading new assets). Outputs:
JSON + markdown report under `docs/reference/gmicloud-wire-probe-{date}.{json,md}`.

**Probe matrix:**

```
For each model_id in PROBE_TARGETS:
  For each casing in (canonical_lower, deprecated_pascal):
    For each image_key in (image, img_url, image_url) [i2v only]:
      For each duration_value in (int, string) [pixverse only]:
        submit minimal payload, capture status code + error message
```

Each cell records: HTTP status, GMICloud error code if any, response time, error
body excerpt. The script aggregates the matrix into a table that maps each
model variant to the wire-key/casing/coercer-type it actually accepts. CI
artifact is the JSON; the markdown is for human review and links into this plan.

**Output structure (excerpt):**

```json
{
  "probed_at": "2026-04-25T...",
  "results": [
    {
      "model": "kling-image2video-v2.1-master",
      "casings": {
        "kling-image2video-v2.1-master": {"status": 200, "error": null},
        "Kling-Image2Video-V2.1-Master": {"status": 200, "error": null}
      },
      "image_keys": {
        "image": {"status": 200, "error": null},
        "img_url": {"status": 400, "error": "unknown parameter"},
        "image_url": {"status": 400, "error": "unknown parameter"}
      }
    },
    ...
  ]
}
```

### Resolution paths (post-probe)

- **If F-2026-04-25-08 confirmed:** add `param_schemas={"duration": EnumSchema(["5", "8", "10"])}`
  to `_PIXVERSE`; remove `duration` from `_VIDEO_BASE.with_coercers(...)` and apply per-model
  (Pixverse strings, others int) so Pixverse's enum is validated and routed correctly.
- **If F-2026-04-25-12 confirmed:** add `param_aliases={"image": "img_url"}` (or
  `"image_url"`) per model in the spec definitions. Caller-facing kwarg stays `image=`,
  the wire payload routes to the right key.
- **If F-2026-04-25-13 confirmed:** for each affected family, swap the canonical and
  alias direction — declare PascalCase as canonical, lowercase as the deprecated alias.
  This partially reverts R-06 for the affected slug subset only; other slugs (Seedance,
  etc.) keep lowercase canonical. CHANGELOG callout required.
- **If any item refuted:** close out the F-id in `feedback.md` with the probe report
  as evidence, ping the reporter with the artifact URL.

### Acceptance gates (probe)

- [ ] `tools/probe_gmicloud_wire.py` runs to completion against staging GMICloud creds
      with no Python errors.
- [ ] Output JSON committed at `docs/reference/gmicloud-wire-probe-2026-04-25.json`.
- [ ] Markdown summary committed at `docs/reference/gmicloud-wire-probe-2026-04-25.md`.
- [ ] Each of F-2026-04-25-08, -12, -13 has a deterministic CONFIRMED/REFUTED verdict in
      the markdown report, with the matrix cell that proves it.
- [ ] Reconciliation table above this section is updated with the verdicts.
- [ ] If any item is CONFIRMED, the spec changes ship in the same `genblaze-gmicloud`
      release as the table update.
