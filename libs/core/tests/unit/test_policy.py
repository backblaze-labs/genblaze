"""Tests for EmbedPolicy and manifest redaction."""

import json

import pytest
from genblaze_core.exceptions import ManifestError
from genblaze_core.models import Manifest, Run, Step
from genblaze_core.models.enums import PromptVisibility
from genblaze_core.models.policy import EmbedPolicy


def _make_manifest(prompt="hello", seed=42) -> Manifest:
    step = Step(
        provider="test",
        model="test-model",
        prompt=prompt,
        seed=seed,
        params={"width": 1024},
    )
    run = Run(steps=[step])
    m = Manifest(run=run, manifest_uri="https://example.com/manifest.json")
    m.compute_hash()
    return m


def test_full_mode_includes_all():
    m = _make_manifest()
    policy = EmbedPolicy(embed_mode="full")
    result = json.loads(m.to_embed_json(policy))
    assert result["run"]["steps"][0]["prompt"] == "hello"
    assert result["run"]["steps"][0]["seed"] == 42
    assert result["run"]["steps"][0]["params"]["width"] == 1024


def test_private_prompt_full_mode_raises():
    """Redacting the prompt under embed_mode='full' must raise.

    Otherwise we'd write the pre-redaction canonical_hash next to
    redacted steps; verify() would never succeed on the embedded payload.
    """
    m = _make_manifest()
    policy = EmbedPolicy(prompt_visibility=PromptVisibility.PRIVATE)
    with pytest.raises(ManifestError, match="embed_mode='pointer'"):
        m.to_embed_json(policy)


def test_exclude_params_full_mode_raises():
    m = _make_manifest()
    policy = EmbedPolicy(include_params=False)
    with pytest.raises(ManifestError, match="embed_mode='pointer'"):
        m.to_embed_json(policy)


def test_exclude_seed_full_mode_raises():
    m = _make_manifest()
    policy = EmbedPolicy(include_seed=False)
    with pytest.raises(ManifestError, match="embed_mode='pointer'"):
        m.to_embed_json(policy)


def test_pointer_mode():
    m = _make_manifest()
    policy = EmbedPolicy(embed_mode="pointer")
    result = json.loads(m.to_embed_json(policy))
    assert "manifest_uri" in result
    assert "canonical_hash" in result
    assert "run" not in result


def test_pointer_mode_is_the_redaction_escape_hatch():
    """Pointer mode pairs redaction policy with verifiability.

    The caller sets prompt_visibility=PRIVATE and embed_mode='pointer';
    the embedded pointer carries only {hash, uri}, and the full manifest
    at manifest_uri still verifies against that hash.
    """
    m = _make_manifest()
    policy = EmbedPolicy(embed_mode="pointer", prompt_visibility=PromptVisibility.PRIVATE)
    pointer = json.loads(m.to_embed_json(policy))
    assert pointer["canonical_hash"] == m.canonical_hash
    # The full manifest (held server-side at manifest_uri) still verifies.
    assert m.verify()


def test_full_mode_no_redaction_round_trips_and_verifies():
    """The default embed_mode='full' with no redaction must round-trip
    through to_embed_json → model_validate → verify() cleanly."""
    m = _make_manifest()
    policy = EmbedPolicy(embed_mode="full")
    embed_json = m.to_embed_json(policy)
    reparsed = Manifest.model_validate(json.loads(embed_json))
    assert reparsed.verify()
    assert reparsed.canonical_hash == m.canonical_hash


def test_pointer_mode_without_uri_raises():
    """Pointer mode with no manifest_uri should raise ManifestError."""
    step = Step(provider="test", model="m", prompt="p")
    run = Run(steps=[step])
    m = Manifest(run=run)
    m.compute_hash()
    policy = EmbedPolicy(embed_mode="pointer")
    with pytest.raises(ManifestError, match="manifest_uri"):
        m.to_embed_json(policy)
