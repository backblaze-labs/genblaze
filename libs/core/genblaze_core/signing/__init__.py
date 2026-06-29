"""Pluggable manifest signing (Genblaze Mode 2)."""

from genblaze_core.signing.base import SignatureBundle, Signer
from genblaze_core.signing.ed25519 import Ed25519Signer, verify_signature_bundle

__all__ = [
    "Ed25519Signer",
    "SignatureBundle",
    "Signer",
    "verify_signature_bundle",
]
