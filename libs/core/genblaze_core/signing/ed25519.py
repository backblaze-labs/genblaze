"""Ed25519 implementation of Mode 2 manifest signing."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from genblaze_core.signing.base import ManifestSigner, SignatureBundle

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
except ImportError as exc:  # pragma: no cover - guarded by optional extra
    Ed25519PrivateKey = None  # type: ignore[misc, assignment]
    Ed25519PublicKey = None  # type: ignore[misc, assignment]
    _CRYPTO_IMPORT_ERROR = exc
else:
    _CRYPTO_IMPORT_ERROR = None


def _require_crypto() -> None:
    if _CRYPTO_IMPORT_ERROR is not None:
        raise ImportError(
            "Ed25519 signing requires cryptography. Install with: pip install 'genblaze-core[signing]'"
        ) from _CRYPTO_IMPORT_ERROR


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Canonical JSON for signing — excludes transport fields like signature."""
    payload = {k: v for k, v in manifest.items() if k not in ("signature", "manifest_uri", "encryption_scheme")}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_sha256(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


class Ed25519Signer(ManifestSigner):
    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        _require_crypto()
        self._private_key = private_key
        self._public_key = private_key.public_key()

    @classmethod
    def generate(cls) -> Ed25519Signer:
        _require_crypto()
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_hex_seed(cls, seed_hex: str) -> Ed25519Signer:
        _require_crypto()
        seed = bytes.fromhex(seed_hex)
        if len(seed) != 32:
            raise ValueError("Ed25519 seed must be 32 bytes (64 hex chars)")
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @classmethod
    def from_env(cls, env_var: str = "GENBLAZE_SIGNING_KEY_HEX") -> Ed25519Signer:
        seed = os.environ.get(env_var, "").strip()
        if not seed:
            raise ValueError(f"Environment variable {env_var} is not set")
        return cls.from_hex_seed(seed)

    @property
    def public_key_hex(self) -> str:
        raw = self._public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return raw.hex()

    def sign_manifest(self, manifest: dict[str, Any], signed_at: str) -> SignatureBundle:
        digest = manifest_sha256(manifest)
        signature = self._private_key.sign(digest.encode("utf-8"))
        return SignatureBundle(
            algorithm="ed25519",
            public_key_hex=self.public_key_hex,
            signature_b64=base64.b64encode(signature).decode("ascii"),
            signed_at=signed_at,
            manifest_sha256=digest,
        )


def verify_signature_bundle(manifest: dict[str, Any], bundle: SignatureBundle) -> bool:
    _require_crypto()
    if bundle.algorithm != "ed25519":
        return False
    if bundle.manifest_sha256 != manifest_sha256(manifest):
        return False
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(bundle.public_key_hex))
        public_key.verify(
            base64.b64decode(bundle.signature_b64),
            bundle.manifest_sha256.encode("utf-8"),
        )
        return True
    except Exception:
        return False
