<!-- last_verified: 2026-04-24 -->
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
