"""Signer protocol for Mode 2 authenticated integrity."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SignatureBundle:
    algorithm: str
    public_key_hex: str
    signature_b64: str
    signed_at: str
    manifest_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "public_key_hex": self.public_key_hex,
            "signature_b64": self.signature_b64,
            "signed_at": self.signed_at,
            "manifest_sha256": self.manifest_sha256,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> SignatureBundle:
        data = json.loads(raw)
        return cls(
            algorithm=data["algorithm"],
            public_key_hex=data["public_key_hex"],
            signature_b64=data["signature_b64"],
            signed_at=data["signed_at"],
            manifest_sha256=data["manifest_sha256"],
        )


class Signer(Protocol):
    """Sign a manifest payload dict (without signature field)."""

    def sign_manifest(self, manifest: dict[str, Any], signed_at: str) -> SignatureBundle: ...


class ManifestSigner(ABC):
    @abstractmethod
    def sign_manifest(self, manifest: dict[str, Any], signed_at: str) -> SignatureBundle:
        raise NotImplementedError
