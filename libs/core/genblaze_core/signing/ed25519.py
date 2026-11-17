"""Ed25519 implementation of Mode 2 manifest signing.

Signs the manifest's existing canonical hash (the same value
:meth:`Manifest.verify_hash` recomputes) rather than a bespoke
re-serialization — Mode 2 stays locked to Mode 1's integrity guarantee, and
schema changes to what's excluded from the hash never need a second,
independently-maintained exclude list here.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from genblaze_core._optional import OptionalDependencyError
from genblaze_core.exceptions import GenblazeError, SigningError
from genblaze_core.models.manifest import Manifest
from genblaze_core.signing.base import ManifestSigner, SignatureBundle, _coerce_manifest

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
except ImportError as exc:  # pragma: no cover - guarded by optional extra
    Ed25519PrivateKey = None  # type: ignore[assignment,misc]
    Ed25519PublicKey = None  # type: ignore[assignment,misc]
    InvalidSignature = ValueError  # type: ignore[assignment,misc]
    _CRYPTO_IMPORT_ERROR: ImportError | None = exc
else:
    _CRYPTO_IMPORT_ERROR = None

# Deliberately deferred, unlike sinks/parquet.py's fail-at-import-time
# OptionalDependencyError: genblaze_core/signing/__init__.py imports this
# module unconditionally, and ManifestSigner/SignatureBundle (base.py) need
# no crypto at all. Raising here at import time would make the whole
# `signing` package unusable without cryptography installed. The trade-off:
# Ed25519Signer stays a resolvable attribute either way — only calling it
# (generate/from_hex_seed/from_env/__init__) or verify_signature_bundle()
# raises OptionalDependencyError, via _require_crypto() below.


def _require_crypto() -> None:
    if _CRYPTO_IMPORT_ERROR is not None:
        raise OptionalDependencyError(
            extra="signing", package="cryptography", symbol="Ed25519Signer"
        ) from _CRYPTO_IMPORT_ERROR


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
        try:
            seed = bytes.fromhex(seed_hex)
        except ValueError as exc:
            raise SigningError(f"Ed25519 seed is not valid hex: {exc}") from exc
        if len(seed) != 32:
            raise SigningError("Ed25519 seed must be 32 bytes (64 hex chars)")
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    @classmethod
    def from_env(cls, env_var: str = "GENBLAZE_SIGNING_KEY_HEX") -> Ed25519Signer:
        seed = os.environ.get(env_var, "").strip()
        if not seed:
            raise SigningError(f"Environment variable {env_var} is not set")
        return cls.from_hex_seed(seed)

    @property
    def public_key_hex(self) -> str:
        raw = self._public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return raw.hex()

    def sign_manifest(
        self, manifest: Manifest | dict[str, Any], signed_at: str
    ) -> SignatureBundle:
        """Sign ``manifest``'s canonical hash, recomputed via the manifest's
        own canonical-hash machinery (not a bespoke serialization)."""
        resolved = _coerce_manifest(manifest)
        if not resolved.canonical_hash:
            # Mirrors Manifest.to_canonical_json()/to_embed_json()'s backfill —
            # without it, signing a manifest before compute_hash() has ever
            # run would let Mode 2 verify while Mode 1's verify_hash() still
            # fails on the still-empty canonical_hash field.
            resolved.compute_hash()
        digest = resolved.recompute_canonical_hash()
        # NOTE: only `digest` is signed, not `algorithm` — safe today because
        # this is the only algorithm and verify_signature_bundle() hardcodes
        # Ed25519 regardless of the field's value. If a second algorithm is
        # ever added, bind `algorithm` into the signed bytes too (e.g.
        # sign(f"{algorithm}:{digest}")) so the field can't be swapped
        # post-hoc — otherwise it becomes classic algorithm-confusion.
        signature = self._private_key.sign(digest.encode("utf-8"))
        return SignatureBundle(
            algorithm="ed25519",
            public_key_hex=self.public_key_hex,
            canonical_hash=digest,
            signature_b64=base64.b64encode(signature).decode("ascii"),
            signed_at=signed_at,
        )


def verify_signature_bundle(manifest: Manifest | dict[str, Any], bundle: SignatureBundle) -> bool:
    """Verify ``bundle`` against ``manifest``.

    Recomputes the manifest's canonical hash first and compares it to the
    hash the bundle claims to have signed — a manifest edited after signing
    fails here, before any cryptographic work happens. Only then is the
    Ed25519 signature itself checked.

    Fails closed (returns ``False``) for any malformed *input* — an
    attacker fully controls both ``manifest`` and ``bundle`` (e.g. a bundle
    round-tripped through :meth:`SignatureBundle.from_json` with a null or
    truncated field, or a manifest dict that fails schema validation), so
    those cases must not raise. Genuine bugs elsewhere still propagate.
    """
    _require_crypto()
    if bundle.algorithm != "ed25519":
        return False
    try:
        resolved = _coerce_manifest(manifest)
        if bundle.canonical_hash != resolved.recompute_canonical_hash():
            return False
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(bundle.public_key_hex))
        public_key.verify(
            base64.b64decode(bundle.signature_b64),
            bundle.canonical_hash.encode("utf-8"),
        )
        return True
    except (ValueError, TypeError, GenblazeError, InvalidSignature):
        # ValueError/TypeError: malformed manifest dict, or a bundle field
        # that's the wrong type (None, wrong length) or bad hex/base64.
        # GenblazeError: manifest fails schema validation (e.g.
        # UnsupportedSchemaVersionError from parse_manifest). InvalidSignature:
        # well-formed but forged/mismatched signature. All of these are
        # "verification failed", not bugs — anything else still propagates.
        return False
