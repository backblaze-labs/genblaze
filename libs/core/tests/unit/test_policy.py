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


def test_private_prompt_redacted():
    m = _make_manifest()
    policy = EmbedPolicy(prompt_visibility=PromptVisibility.PRIVATE)
    result = json.loads(m.to_embed_json(policy))
    step = result["run"]["steps"][0]
    assert step["prompt"] is None
    assert step["negative_prompt"] is None
    assert step["prompt_visibility"] == "redacted"


def test_pointer_mode():
    m = _make_manifest()
    policy = EmbedPolicy(embed_mode="pointer")
    result = json.loads(m.to_embed_json(policy))
    assert "manifest_uri" in result
    assert "canonical_hash" in result
    assert "run" not in result


def test_exclude_params():
    m = _make_manifest()
    policy = EmbedPolicy(include_params=False)
    result = json.loads(m.to_embed_json(policy))
    assert result["run"]["steps"][0]["params"] == {}


def test_exclude_seed():
    m = _make_manifest()
    policy = EmbedPolicy(include_seed=False)
    result = json.loads(m.to_embed_json(policy))
    assert result["run"]["steps"][0]["seed"] is None


def test_pointer_mode_without_uri_raises():
    """Pointer mode with no manifest_uri should raise ManifestError."""
    step = Step(provider="test", model="m", prompt="p")
    run = Run(steps=[step])
    m = Manifest(run=run)
    m.compute_hash()
    policy = EmbedPolicy(embed_mode="pointer")
    with pytest.raises(ManifestError, match="manifest_uri"):
        m.to_embed_json(policy)
