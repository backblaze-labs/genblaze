#!/usr/bin/env python3
"""Example: AssemblyAI speech-to-text transcription pipeline.

AssemblyAI is the inverse of every other genblaze connector: it *consumes* an
audio URL and *produces* a hash-verified TEXT transcript (plus word-level
timings). This example transcribes AssemblyAI's own public quickstart sample
(the "Canadian wildfires" clip) with the ``universal-3-pro`` speech model and
verifies the manifest end-to-end.

This is a live smoke test against the real API — it makes a real transcription
call and bills your account per minute of input audio.

Models (sent on the SDK's plural ``speech_models`` field — the legacy
singular ``speech_model`` field and the ``best`` / ``nano`` aliases were
retired by the live API and now error):
    - universal-3-pro: Highest accuracy (default here).
    - universal-2:     Prior-generation universal model.

Usage:
    export ASSEMBLYAI_API_KEY=...
    python examples/transcribe.py
"""

from __future__ import annotations

from genblaze_core import Modality, Pipeline

# AssemblyAI's public quickstart sample (302-redirects to a ~3-min mp3 hosted
# on the AssemblyAI-Community GitHub). https-only, so it passes the provider's
# SSRF validator. Swap in any public https:// audio URL to transcribe your own.
AUDIO_URL = "https://assembly.ai/wildfires.mp3"


def main() -> None:
    from genblaze_assemblyai import AssemblyAIProvider

    # api_key falls back to ASSEMBLYAI_API_KEY in the environment.
    provider = AssemblyAIProvider()

    run, manifest = (
        Pipeline("assemblyai-transcribe-demo", project_id="examples")
        .step(
            provider,
            model="universal-3-pro",
            prompt=AUDIO_URL,  # resolved as the audio URL to transcribe
            modality=Modality.TEXT,
        )
        # Transcription of a ~3-min clip completes well inside this; give it
        # generous headroom since it's a real network job.
        .run(timeout=300, max_retries=1, raise_on_failure=True)
    )

    step = run.steps[0]

    print(f"Run ID:    {run.run_id}")
    print(f"Status:    {step.status}")
    print(f"Hash:      {manifest.canonical_hash}")
    print(f"Verified:  {manifest.verify()}")

    asset = step.assets[0]
    text = asset.metadata["text"]
    word_timings = asset.audio.word_timings if asset.audio else []
    audio_duration = step.provider_payload.get("audio_duration")

    # --- assertions (the actual smoke test) ---
    assert text, "transcript text is empty"
    assert word_timings, "word_timings is empty"
    assert audio_duration is not None, "audio_duration missing from provider_payload"
    assert manifest.verify() is True, "manifest failed verification"
    # NOTE: deliberately no cost_usd assertion — this provider ships no pricing
    # (per-minute-of-input-audio is user-registered; see pricing-recipes.md).

    print()
    print(f"Asset:           {asset.url}")
    print(f"Audio duration:  {audio_duration}s")
    print(f"Word count:      {len(word_timings)}")
    print()
    print("Transcript:")
    print(f"  {text}")
    print()
    print("First word timings (word: start–end s, confidence):")
    for w in word_timings[:8]:
        conf = f"{w.confidence:.2f}" if w.confidence is not None else "n/a"
        print(f"  {w.word!r}: {w.start:.2f}–{w.end:.2f}s  conf={conf}")

    print()
    print("✅ Smoke test passed.")


if __name__ == "__main__":
    main()
