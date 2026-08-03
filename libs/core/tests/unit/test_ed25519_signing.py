"""Tests for Mode 2 Ed25519 manifest signing."""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from genblaze_core.signing import Ed25519Signer, verify_signature_bundle
from genblaze_core.signing.ed25519 import manifest_sha256


def test_sign_and_verify_manifest():
    signer = Ed25519Signer.generate()
    manifest = {
        "schema_version": "1.5",
        "run": {"run_id": "abc", "steps": []},
        "canonical_hash": "deadbeef",
    }
    bundle = signer.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")
    assert verify_signature_bundle(manifest, bundle)
    assert bundle.manifest_sha256 == manifest_sha256(manifest)


def test_tampered_manifest_fails_verify():
    signer = Ed25519Signer.generate()
    manifest = {"schema_version": "1.5", "run": {"run_id": "abc", "steps": []}}
    bundle = signer.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")
    manifest["canonical_hash"] = "changed"
    assert not verify_signature_bundle(manifest, bundle)


def test_from_hex_seed_deterministic():
    seed = "ab" * 32
    a = Ed25519Signer.from_hex_seed(seed)
    b = Ed25519Signer.from_hex_seed(seed)
    assert a.public_key_hex == b.public_key_hex
