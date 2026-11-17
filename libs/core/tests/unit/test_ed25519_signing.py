"""Tests for Mode 2 Ed25519 manifest signing.

Signing operates on a real ``Manifest`` and signs its existing
``canonical_hash`` (recomputed via the same machinery ``verify_hash()``
uses) — these tests exercise that end to end, not just hand-built dicts,
since a bespoke serialization path previously passed unit tests while
crashing on an actual ``Manifest`` (datetime/UUID fields aren't JSON
serializable via a raw ``json.dumps``).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime

import pytest

pytest.importorskip("cryptography")

from genblaze_core._optional import OptionalDependencyError
from genblaze_core.exceptions import SigningError
from genblaze_core.models.manifest import Manifest
from genblaze_core.models.run import Run
from genblaze_core.models.step import Step
from genblaze_core.signing import Ed25519Signer, verify_signature_bundle


def _build_manifest() -> Manifest:
    step = Step(provider="test", model="m", prompt="a cat riding a bike")
    return Manifest.from_run(Run(steps=[step]))


class TestSignAndVerify:
    def test_sign_and_verify_real_manifest(self):
        """End-to-end against an actual Manifest, not a hand-built dict —
        the doc example (trust-modes.md) mirrors this exact flow."""
        signer = Ed25519Signer.generate()
        manifest = _build_manifest()

        bundle = signer.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")

        assert bundle.canonical_hash == manifest.canonical_hash
        assert verify_signature_bundle(manifest, bundle)

    def test_sign_accepts_dict_by_validating_into_manifest(self):
        """The dict-accepting path validates into a real Manifest first —
        it isn't a second, parallel serialization."""
        signer = Ed25519Signer.generate()
        manifest = _build_manifest()
        payload = manifest.model_dump(mode="json")

        bundle = signer.sign_manifest(payload, signed_at="2026-06-29T00:00:00Z")

        assert verify_signature_bundle(payload, bundle)
        assert verify_signature_bundle(manifest, bundle)

    def test_sign_backfills_unset_canonical_hash(self):
        """Signing a manifest whose canonical_hash was never computed (built
        via the bare constructor, not from_run()/compute_hash()) must
        backfill the field — otherwise Mode 2 would verify while Mode 1's
        verify_hash() still fails on the empty hash, contradicting 'Mode 2 =
        Mode 1 + signature'."""
        manifest = Manifest(run=Run(steps=[Step(provider="test", model="m", prompt="x")]))
        assert manifest.canonical_hash == ""

        signer = Ed25519Signer.generate()
        bundle = signer.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")

        assert manifest.canonical_hash == bundle.canonical_hash
        assert manifest.verify_hash()
        assert verify_signature_bundle(manifest, bundle)

    def test_doc_example_runs_end_to_end(self):
        """Mirrors docs/features/trust-modes.md's Mode 2 example verbatim
        (modulo GENBLAZE_SIGNING_KEY_HEX, generated here instead of read
        from the environment) — catches doc/code drift."""
        run = Run(steps=[Step(provider="test", model="m", prompt="hello")])

        signer = Ed25519Signer.generate()
        manifest = Manifest.from_run(run)
        bundle = signer.sign_manifest(manifest, signed_at=datetime.now(UTC).isoformat())
        manifest.signature = bundle.to_json()
        assert verify_signature_bundle(manifest, bundle)


class TestTamperDetection:
    def test_content_change_after_signing_fails_verify(self):
        """Editing the manifest after signing changes canonical_hash, so
        verification must fail on the hash-recompute check — before any
        cryptographic work happens."""
        signer = Ed25519Signer.generate()
        manifest = _build_manifest()
        bundle = signer.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")

        original_hash = manifest.canonical_hash
        manifest.run.steps[0].prompt = "a dog riding a skateboard"
        manifest.compute_hash()

        assert manifest.canonical_hash != original_hash
        assert not verify_signature_bundle(manifest, bundle)

    def test_bundle_hash_mismatch_short_circuits_before_crypto_check(self):
        """A bundle claiming a stale/wrong canonical_hash fails even though
        the signature bytes themselves are validly formed."""
        signer = Ed25519Signer.generate()
        manifest = _build_manifest()
        bundle = signer.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")

        stale_bundle = replace(bundle, canonical_hash="0" * 64)
        assert not verify_signature_bundle(manifest, stale_bundle)


class TestForgery:
    def test_corrupted_signature_with_matching_hash_fails(self):
        """A matching canonical_hash alone must not be enough — the actual
        Ed25519 signature bytes have to verify too."""
        signer = Ed25519Signer.generate()
        manifest = _build_manifest()
        bundle = signer.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")

        corrupted = bytearray(bundle.signature_b64.encode())
        corrupted[0] ^= 0xFF
        forged = replace(bundle, signature_b64=corrupted.decode("latin-1"))

        assert not verify_signature_bundle(manifest, forged)

    def test_signature_from_wrong_key_fails(self):
        """A signature produced by a different keypair, spliced onto a
        bundle claiming the victim's public key, must not verify."""
        victim = Ed25519Signer.generate()
        attacker = Ed25519Signer.generate()
        manifest = _build_manifest()

        legit_bundle = victim.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")
        forged_bundle = attacker.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")

        spliced = replace(forged_bundle, public_key_hex=victim.public_key_hex)

        assert not verify_signature_bundle(manifest, spliced)
        # Sanity: each signer's own bundle still verifies against itself.
        assert verify_signature_bundle(manifest, legit_bundle)
        assert verify_signature_bundle(manifest, forged_bundle)


class TestFailsClosedOnMalformedInput:
    """verify_signature_bundle() must return False, never raise, for
    attacker-controlled inputs — both manifest and bundle are untrusted in
    the realistic "verify a manifest+signature received over the wire"
    scenario. A raised exception is a DoS/availability gap for a naive
    `if not verify_signature_bundle(...): reject()` caller, even though it's
    not a false-positive (no forged signature is ever accepted)."""

    def test_bundle_with_null_public_key_hex_fails_closed(self):
        """A bundle round-tripped through from_json() with a null field is
        valid JSON and trivially attacker-suppliable. bytes.fromhex(None)
        raises TypeError, which must be caught, not propagated."""
        signer = Ed25519Signer.generate()
        manifest = _build_manifest()
        bundle = signer.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")

        forged = replace(bundle, public_key_hex=None)
        assert not verify_signature_bundle(manifest, forged)

    def test_bundle_with_null_signature_fails_closed(self):
        signer = Ed25519Signer.generate()
        manifest = _build_manifest()
        bundle = signer.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")

        forged = replace(bundle, signature_b64=None)
        assert not verify_signature_bundle(manifest, forged)

    def test_malformed_manifest_dict_fails_closed(self):
        """A manifest dict missing required fields fails Pydantic validation
        inside _coerce_manifest() — that must also surface as False, not an
        unhandled ValidationError, since the dict-accepting path exists
        specifically for untrusted wire payloads."""
        signer = Ed25519Signer.generate()
        manifest = _build_manifest()
        bundle = signer.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")

        assert not verify_signature_bundle({"schema_version": "1.5"}, bundle)

    def test_unsupported_schema_version_dict_fails_closed(self):
        """parse_manifest() raises UnsupportedSchemaVersionError (a
        GenblazeError, not a ValueError) for an unknown schema_version —
        must also fail closed rather than propagate."""
        signer = Ed25519Signer.generate()
        manifest = _build_manifest()
        bundle = signer.sign_manifest(manifest, signed_at="2026-06-29T00:00:00Z")

        payload = manifest.model_dump(mode="json")
        payload["schema_version"] = "99.0"
        assert not verify_signature_bundle(payload, bundle)


class TestKeyMaterial:
    def test_from_hex_seed_deterministic(self):
        seed = "ab" * 32
        a = Ed25519Signer.from_hex_seed(seed)
        b = Ed25519Signer.from_hex_seed(seed)
        assert a.public_key_hex == b.public_key_hex

    def test_from_hex_seed_wrong_length_raises_signing_error(self):
        with pytest.raises(SigningError):
            Ed25519Signer.from_hex_seed("ab" * 16)

    def test_from_env_missing_raises_signing_error(self, monkeypatch):
        monkeypatch.delenv("GENBLAZE_SIGNING_KEY_HEX", raising=False)
        with pytest.raises(SigningError):
            Ed25519Signer.from_env()


class TestOptionalDependency:
    """Mirrors tests/unit/test_optional_imports.py's simulate-missing-package
    technique for genblaze_core.sinks.parquet."""

    def test_missing_cryptography_raises_optional_dependency_error(self):
        for cached in list(sys.modules):
            if cached.startswith("cryptography") or cached == "genblaze_core.signing.ed25519":
                sys.modules.pop(cached, None)
        sys.modules["cryptography"] = None  # type: ignore[assignment]
        try:
            import genblaze_core.signing.ed25519 as ed25519_reloaded

            with pytest.raises(OptionalDependencyError) as exc_info:
                ed25519_reloaded.Ed25519Signer.generate()
            err = exc_info.value
            assert err.extra == "signing"
            assert err.package == "cryptography"
            assert isinstance(err, ImportError)
        finally:
            sys.modules.pop("cryptography", None)
            sys.modules.pop("genblaze_core.signing.ed25519", None)
            import genblaze_core.signing.ed25519  # noqa: F401  (restore clean state)

    def test_import_genblaze_core_does_not_import_cryptography(self):
        """Runs in a fresh subprocess rather than evicting genblaze_core from
        this process's sys.modules — the latter would leave duplicate class
        objects (Manifest, Run, ...) floating around for the rest of the
        test session, breaking isinstance checks in unrelated tests."""
        script = (
            "import sys; import genblaze_core; "
            "assert 'cryptography' not in sys.modules, "
            "'import genblaze_core must not eagerly import cryptography "
            "(signing is lazy-loaded via _LAZY_IMPORTS)'; "
            "genblaze_core.Ed25519Signer; "
            "assert 'cryptography' in sys.modules, "
            "'accessing the lazy attribute should pull cryptography in'"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
