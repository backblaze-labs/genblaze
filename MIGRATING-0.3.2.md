# Migrating to genblaze 0.3.2

**Wave:** `0.3.2 — storage ergonomics & GMI catalog hygiene` (2026-05-26).
**Per-package bumps:** `genblaze-core` 0.3.0 → **0.3.2**, `genblaze-s3` 0.3.1 → **0.3.2**, `genblaze-gmicloud` 0.3.0 → **0.3.1**. All other packages unchanged.

This wave is **additive-only** — no existing import paths, kwargs, or behaviors break. Most existing call sites need no changes. Read the per-area sections below for the ones that do.

## TL;DR — most callers do nothing

```bash
pip install --upgrade genblaze-core genblaze-s3 genblaze-gmicloud
```

Existing code keeps working. Some users will see new INFO/WARN log lines (described below) — those are nudges toward canonical forms, not errors.

> **Exception — monorepo contributors:** if you previously ran `pip install -e .` from the repo root, switch to `make install-dev` (the stub `[project]` block was stripped from the root `pyproject.toml`; see the "Repo dev install" section below).

## genblaze-core 0.3.2

### `URLPolicy` relocated to core (back-compat preserved)

`URLPolicy` and `URLPolicyError` moved from `genblaze_s3.url_policy` to `genblaze_core.storage.url_policy`. The S3 module is now a thin re-export.

- **Old `from genblaze_s3.url_policy import URLPolicy` — keeps working.**
- New canonical: `from genblaze_core import URLPolicy` (or `from genblaze_core.storage import URLPolicy`).
- No call-site changes required.

### `ObjectStorageSink(asset_url_policy=...)` kwarg

Optional new constructor kwarg controlling what URL flavor lands in `asset.url`. Default `URLPolicy.AUTO` preserves today's durable-URL behavior — no migration.

- `URLPolicy.AUTO` (default): writes `backend.get_durable_url(key)`. **New:** emits a one-time WARN per `(bucket, policy)` if the backend has a `public_url_base` attribute but it's unset/empty — *"backend has no public_url_base configured … browsers may 403 on private buckets. Configure backend.public_url_base, or read assets via backend.presigned_get_url(key) at fetch time."* The WARN is a nudge, not a failure; behavior is unchanged.
- `URLPolicy.PUBLIC`: opt-in strict mode — raises `URLPolicyError` at sink construction if `public_url_base` is unset.
- `URLPolicy.PRESIGNED`: **rejected** at sink construction. Manifests must not carry SigV4 URLs (they decay before the manifest does). Call `backend.presigned_get_url(key)` directly when you need a per-asset presigned URL at fetch time.

### `ModelFamily.canonical_slug` field

New optional `Callable[[str], str]` field on `ModelFamily`. Connectors set it when the upstream API is case-sensitive AND they want to accept multiple casings from users. Default `None` preserves existing pass-through behavior — **no migration for user-defined families**.

When a family declares `canonical_slug` and a user's input gets rewritten, the registry emits a one-time INFO per `(family, input)`: *"<family-name> canonical-slug rewrite: 'OldForm' → 'newform'. Update call sites to the canonical form to avoid this log line."*

## genblaze-s3 0.3.2

### `presigned_get_url(key)` / `presigned_put_url(key)`

New methods returning raw `str` URLs — convenience companions to `presigned_get(key)` / `presigned_put(key)` (which return a redaction-safe `PresignedURL` value object).

Use the `_url` variants when handing the URL straight to an HTTP client. The wrapped variants remain the safe default — they still redact in logs.

Equivalent to `backend.presigned_get(key).url` — additive only.

### `for_backblaze()` 403 → region probe error messages

When `for_backblaze()` preflight hits a 403 from a B2 endpoint (some B2 regions return 403 instead of 301 for cross-region buckets), the SDK now probes the other published B2 regions in parallel and surfaces a specific region in the error:

- One match → *"Bucket 'X' lives in `us-east-005` — pass `region='us-east-005'`."*
- All-404 → *"Bucket 'X' does not exist in any known B2 region."*
- Mixed → today's generic message + the endpoint URL we tried.

**No migration.** Errors that were already raising still raise; just with more useful messages.

## genblaze-gmicloud 0.3.1

### Slug casing rewritten to GMI's published wire forms

The connector now bridges GMI's per-slug casing inconsistency via `ModelFamily.canonical_slug`. Pre-0.3.1 callers continue to work; their input gets rewritten on the wire with a one-time INFO nudge.

| Family | Pre-0.3.1 in our code | Canonical wire form (per GMI 2026 catalog) |
|--------|----------------------|--------------------------------------------|
| Audio TTS | `ElevenLabs-TTS-v3`, `MiniMax-TTS-*`, `Inworld-TTS-*` | `elevenlabs-tts-v3`, `minimax-tts-speech-2.6-turbo`, `inworld-tts-1.5-mini` |
| Audio Voice-Clone | `MiniMax-Voice-Clone-Speech-2.6-HD` | `minimax-audio-voice-clone-speech-2.6-hd` (note added `-audio-`) |
| Audio Music | `MiniMax-Music-2.5` | `minimax-music-2.5` |
| Video Veo | `veo3`, `veo3-fast` | `Veo3`, `Veo3-Fast` |
| Video Kling V2.1 | (fell through to fallback) | `Kling-Text2Video-V2.1-Master`, `Kling-Image2Video-V2.1-Master` |

**Migration:** call sites work as-is. If you want to silence the INFO log lines, switch your `model="..."` strings to the canonical forms shown above. Connector READMEs + `examples/gmicloud_*_pipeline.py` already use the canonical forms.

Sources: [GMI 2026-03-10 blog](https://www.gmicloud.ai/en/blog/the-most-popular-ai-models-available-today), [2026-04-14 blog](https://www.gmicloud.ai/en/blog/real-time-video-generation-platforms), and `console.gmicloud.ai` per-model playground URLs.

## Repo dev install (monorepo contributors only)

The root `pyproject.toml`'s stub `[project]` block was removed. If you previously ran `pip install -e .` from the repo root, that command now errors with `Missing 'project' metadata table`. Use `make install-dev` instead — already documented in `CONTRIBUTING.md`.

Affects monorepo contributors only. PyPI installs (`pip install genblaze`) are unaffected.

## Catalog drift checklist (maintainers only)

`docs/dev-workflows.md` gains a "Pre-release catalog verification" section with links to every provider's official model catalog page (GMICloud, OpenAI, Google, Replicate, Runway, Luma, Decart, ElevenLabs, Stability, LMNT, NVIDIA). 5-minute click-through before each release wave.

Affects maintainers only.
