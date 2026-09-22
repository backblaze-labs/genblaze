"""Pluggable manifest signing (Genblaze Mode 2)."""

from genblaze_core.signing.base import ManifestSigner, SignatureBundle
from genblaze_core.signing.ed25519 import Ed25519Signer, verify_signature_bundle

__all__ = [
    "Ed25519Signer",
    "ManifestSigner",
    "SignatureBundle",
    "verify_signature_bundle",
]
