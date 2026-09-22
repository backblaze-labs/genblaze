"""Signer interface for Mode 2 authenticated integrity.

See ``docs/features/trust-modes.md`` — Mode 2 signs the manifest's existing
``canonical_hash`` (the same value :meth:`Manifest.verify_hash` checks) rather
than re-deriving its own notion of "the manifest", so a signature always
tracks Mode 1's integrity guarantee instead of drifting from it.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from genblaze_core.canonical.json import canonical_json
from genblaze_core.models.manifest import Manifest, parse_manifest


def _coerce_manifest(manifest: Manifest | dict[str, Any]) -> Manifest:
    """Validate a raw manifest dict into a :class:`Manifest`, or pass through.

    Signing/verification always operates on a real ``Manifest`` — accepting a
    dict is a convenience for callers holding a freshly-deserialized payload,
    not a second code path with its own semantics.
    """
    if isinstance(manifest, Manifest):
        return manifest
    return parse_manifest(manifest)


@dataclass(frozen=True)
class SignatureBundle:
    """Minimal wire shape for a Mode 2 signature.

    Stored as JSON in ``Manifest.signature`` (a field already reserved and
    excluded from the canonical hash — see ``models/manifest.py``).
    """

    algorithm: str
    public_key_hex: str
    canonical_hash: str
    signature_b64: str
    signed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "public_key_hex": self.public_key_hex,
            "canonical_hash": self.canonical_hash,
            "signature_b64": self.signature_b64,
            "signed_at": self.signed_at,
        }

    def to_json(self) -> str:
        # Reuse the repo's canonical JSON formatting rather than a second,
        # bespoke json.dumps() — the exact duplication this module exists to
        # eliminate. Fields are all plain ASCII, so output is unaffected by
        # canonical_json()'s extra NFC-normalization/ensure_ascii=False.
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, raw: str) -> SignatureBundle:
        data = json.loads(raw)
        return cls(
            algorithm=data["algorithm"],
            public_key_hex=data["public_key_hex"],
            canonical_hash=data["canonical_hash"],
            signature_b64=data["signature_b64"],
            signed_at=data["signed_at"],
        )


class ManifestSigner(ABC):
    """Pluggable signer — sign a manifest's canonical hash."""

    @abstractmethod
    def sign_manifest(
        self, manifest: Manifest | dict[str, Any], signed_at: str
    ) -> SignatureBundle:
        raise NotImplementedError
