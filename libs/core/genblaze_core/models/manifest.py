"""Manifest model — hash-verified generation manifest."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from genblaze_core.canonical.json import canonical_hash, canonical_json
from genblaze_core.exceptions import ManifestError
from genblaze_core.models.enums import PromptVisibility
from genblaze_core.models.run import Run

if TYPE_CHECKING:
    from genblaze_core.models.policy import EmbedPolicy

SCHEMA_VERSION = "1.6"

# Operational fields excluded from canonical hash — these are non-deterministic
# (timestamps, status) or potentially sensitive (error messages, provider payloads).
# Excluding them makes the hash a stable provenance identifier for the same inputs.
# NOTE: Step.metadata and Run.metadata are intentionally INCLUDED — they represent
# user-supplied provenance tags (e.g. project labels, lineage annotations).
# Runtime metrics should go in provider_payload, not metadata.
_STEP_HASH_EXCLUDE = frozenset(
    {
        "step_id",  # Random UUID per execution — not provenance
        "run_id",  # Set by RunBuilder, random per execution
        "status",
        "error",
        "error_code",
        "retries",
        "cost_usd",
        "started_at",
        "completed_at",
        "provider_payload",
        "step_index",
    }
)
_RUN_HASH_EXCLUDE = frozenset(
    {
        "run_id",  # Random UUID per execution — not provenance
        "status",
        "created_at",
        "started_at",
        "completed_at",
        "idempotency_key",
        "parent_run_id",
    }
)
_ASSET_HASH_EXCLUDE = frozenset(
    {
        "asset_id",  # Random UUID per execution — not provenance
        "url",  # Transport hint when sha256 is present; varies across
        # re-uploads, presigning, and CDN→durable rewrites. Provenance
        # identity is sha256 + media_type + size_bytes. In schema 1.6+,
        # _strip_asset_for_hash() keeps a URL-only marker so unhashed
        # assets cannot collapse to the same canonical payload.
    }
)
_UNHASHED_ASSET_MARKER = "url_only_unverified"
_UNHASHED_ASSET_URL_FIELD = "unverified_asset_url"
_UNHASHED_ASSET_MARKER_SCHEMA = (1, 6)

# Schema versions that included random IDs in the canonical hash
_LEGACY_SCHEMA_VERSIONS = frozenset({"1.0", "1.1", "1.2", "1.3"})

# Pre-1.4 exclusion sets (IDs were included in the hash)
_STEP_HASH_EXCLUDE_V1_3 = _STEP_HASH_EXCLUDE - {"step_id", "run_id"}
_RUN_HASH_EXCLUDE_V1_3 = _RUN_HASH_EXCLUDE - {"run_id"}


def _version_tuple(schema_version: str) -> tuple[int, ...]:
    """Return a numeric schema tuple for feature gates."""
    try:
        return tuple(int(part) for part in schema_version.split("."))
    except ValueError:
        return (0,)


def _uses_unhashed_asset_markers(schema_version: str) -> bool:
    return _version_tuple(schema_version) >= _UNHASHED_ASSET_MARKER_SCHEMA


def _strip_asset_for_hash(asset: dict, *, mark_unhashed: bool) -> None:
    """Strip operational asset fields while marking URL-only assets."""
    url = asset.get("url")
    has_sha256 = bool(asset.get("sha256"))
    for key in _ASSET_HASH_EXCLUDE:
        asset.pop(key, None)
    if mark_unhashed and not has_sha256 and url is not None:
        asset["asset_integrity"] = _UNHASHED_ASSET_MARKER
        asset[_UNHASHED_ASSET_URL_FIELD] = url


def _unhashed_output_asset_ids(run: Run) -> list[str]:
    """Return output asset IDs that cannot be verified against content bytes."""
    return [asset.asset_id for step in run.steps for asset in step.assets if not asset.sha256]


def _hash_payload(schema_version: str, run: Run) -> dict:
    """Build the hash payload with operational fields stripped.

    Version-aware: schemas <= 1.3 included random IDs in the hash.
    Schema 1.4+ excludes them for deterministic provenance.
    """
    run_data = run.model_dump(mode="python")

    # Select exclusion sets based on schema version
    use_legacy = schema_version in _LEGACY_SCHEMA_VERSIONS
    step_exclude = _STEP_HASH_EXCLUDE_V1_3 if use_legacy else _STEP_HASH_EXCLUDE
    run_exclude = _RUN_HASH_EXCLUDE_V1_3 if use_legacy else _RUN_HASH_EXCLUDE
    mark_unhashed_assets = _uses_unhashed_asset_markers(schema_version)

    # Strip run-level operational fields
    for key in run_exclude:
        run_data.pop(key, None)
    # Strip step-level operational fields and asset IDs
    for step in run_data.get("steps", []):
        for key in step_exclude:
            step.pop(key, None)
        if not use_legacy:
            for asset in step.get("assets", []):
                _strip_asset_for_hash(asset, mark_unhashed=mark_unhashed_assets)
            for inp in step.get("inputs", []):
                _strip_asset_for_hash(inp, mark_unhashed=mark_unhashed_assets)
    return {"schema_version": schema_version, "run": run_data}


class Manifest(BaseModel):
    """A hash-verified, canonical JSON document capturing full provenance."""

    model_config = ConfigDict(validate_assignment=True)

    schema_version: str = Field(default=SCHEMA_VERSION, description="Schema version identifier.")
    run: Run = Field(description="The run this manifest describes.")
    canonical_hash: str = Field(default="", description="SHA-256 hash of canonical JSON payload.")
    # NOTE: manifest_uri, encryption_scheme, and signature are intentionally
    # excluded from the canonical hash — they are transport/storage metadata
    manifest_uri: str | None = Field(
        default=None, description="URI for pointer-mode embedding. Not included in hash."
    )
    encryption_scheme: str | None = Field(
        default=None,
        description="Encryption scheme (reserved). Not included in hash.",
    )
    signature: str | None = Field(
        default=None,
        description="Cryptographic signature (reserved). Not included in hash.",
    )
    transfer_failures: list[str] = Field(
        default_factory=list,
        description=(
            "Asset IDs that failed to transfer to storage during sink.write_run(). "
            "Populated by ObjectStorageSink on partial failures. Not included in hash — "
            "these are transport-layer diagnostics, not provenance."
        ),
    )

    def __repr__(self) -> str:
        h = self.canonical_hash[:12] if self.canonical_hash else "(unhashed)"
        run_id = self.run.run_id[:8]
        return f"Manifest(version={self.schema_version}, hash={h}..., run={run_id}...)"

    @classmethod
    def from_run(cls, run: Run) -> Manifest:
        """Create a manifest from a run, computing its canonical hash."""
        m = cls(run=run)
        m.compute_hash()
        return m

    def compute_hash(self) -> str:
        """Compute and set the canonical hash from provenance-relevant run data.

        Operational fields (status, timestamps, errors, provider_payload)
        are excluded so the hash is a stable provenance identifier.
        """
        payload = _hash_payload(self.schema_version, self.run)
        self.canonical_hash = canonical_hash(payload)
        return self.canonical_hash

    def to_canonical_json(self) -> str:
        """Return the full manifest as canonical JSON (including hash)."""
        if not self.canonical_hash:
            self.compute_hash()
        return canonical_json(self.model_dump(mode="python"))

    def verify(self) -> bool:
        """Verify the manifest hash and output asset byte binding.

        In schema 1.6+, URL-only output assets are included in the canonical
        payload as metadata, but they do not prove byte integrity. A current
        manifest containing output assets without ``sha256`` therefore does
        not verify as asset-integrity provenance.
        """
        if not self.verify_hash():
            return False
        if (
            _uses_unhashed_asset_markers(self.schema_version)
            and self.unverified_output_asset_ids()
        ):
            return False
        return True

    def verify_hash(self) -> bool:
        """Verify only that ``canonical_hash`` matches the canonical payload."""
        payload = _hash_payload(self.schema_version, self.run)
        return self.canonical_hash == canonical_hash(payload)

    def unverified_output_asset_ids(self) -> list[str]:
        """Return output asset IDs missing byte hashes under current semantics."""
        if not _uses_unhashed_asset_markers(self.schema_version):
            return []
        return self.output_asset_ids_missing_sha256()

    def output_asset_ids_missing_sha256(self) -> list[str]:
        """Return output asset IDs missing byte hashes, regardless of schema."""
        return _unhashed_output_asset_ids(self.run)

    def to_embed_json(self, policy: EmbedPolicy) -> str:
        """Return canonical JSON for embedding per policy.

        - ``embed_mode='pointer'`` returns ``{schema_version, canonical_hash,
          manifest_uri}`` only. The full manifest stays at ``manifest_uri``;
          consumers fetch and :meth:`verify` it there.
        - ``embed_mode='full'`` with no redaction returns the full canonical
          manifest unchanged — ``verify()`` round-trips.
        - ``embed_mode='full'`` combined with ANY redaction (``PRIVATE``
          prompt, ``include_params=False``, ``include_seed=False``) raises
          :class:`ManifestError`. Writing the pre-redaction
          ``canonical_hash`` next to redacted content produces a manifest
          that can never ``verify()`` against its own payload, which silently
          breaks the provenance guarantee. Use ``embed_mode='pointer'`` for
          privacy — pointer mode preserves verifiability while keeping the
          sensitive fields off-media.
        """
        if not self.canonical_hash:
            self.compute_hash()

        if policy.embed_mode == "pointer":
            if self.manifest_uri is None:
                raise ManifestError("embed_mode='pointer' requires manifest_uri to be set")
            pointer = {
                "schema_version": self.schema_version,
                "canonical_hash": self.canonical_hash,
                "manifest_uri": self.manifest_uri,
            }
            return canonical_json(pointer)

        # Full mode: reject any redaction that would desynchronize hash and payload.
        if (
            policy.prompt_visibility == PromptVisibility.PRIVATE
            or not policy.include_params
            or not policy.include_seed
        ):
            raise ManifestError(
                "Redaction with embed_mode='full' produces a manifest whose "
                "canonical_hash cannot verify against its redacted payload. "
                "Use embed_mode='pointer' to embed {hash, manifest_uri} and "
                "keep the full (verifiable) manifest at manifest_uri."
            )

        return canonical_json(self.model_dump(mode="python"))


def _migrate_v1_0_to_v1_1(data: dict) -> dict:
    """Migrate a v1.0 manifest dict so it parses under the v1.1 model.

    Adds cost_usd=None to steps that lack it. Does NOT change schema_version
    so that verify() can reproduce the original hash.
    """
    for step in data.get("run", {}).get("steps", []):
        step.setdefault("cost_usd", None)
    return data


def _migrate_v1_1_to_v1_2(data: dict) -> dict:
    """Migrate a v1.1 manifest dict so it parses under the v1.2 model.

    No structural changes needed — the only difference is step_type now
    allows "edit". Does NOT change schema_version so verify() reproduces
    the original hash.
    """
    return data


def _migrate_v1_2_to_v1_3(data: dict) -> dict:
    """Migrate a v1.2 manifest dict so it parses under the v1.3 model.

    Adds video=None and audio=None to assets that lack them.
    Does NOT change schema_version so verify() reproduces the original hash.
    """
    for step in data.get("run", {}).get("steps", []):
        for asset in step.get("assets", []):
            asset.setdefault("video", None)
            asset.setdefault("audio", None)
        for inp in step.get("inputs", []):
            inp.setdefault("video", None)
            inp.setdefault("audio", None)
    return data


def _migrate_v1_3_to_v1_4(data: dict) -> dict:
    """Migrate a v1.3 manifest dict so it parses under the v1.4 model.

    No structural changes needed — the only difference is that v1.4
    excludes random IDs from the canonical hash. Does NOT change
    schema_version so verify() reproduces the original hash.
    """
    return data


def _migrate_v1_4_to_v1_5(data: dict) -> dict:
    """Migrate a v1.4 manifest dict so it parses under the v1.5 model.

    v1.5 adds an optional top-level ``transfer_failures`` field (non-hashed
    transport-layer diagnostics). Hash payload semantics are unchanged.
    Does NOT change schema_version so verify() reproduces the original hash.
    """
    data.setdefault("transfer_failures", [])
    return data


def parse_manifest(data: dict) -> Manifest:
    """Parse a manifest dict, migrating from older schema versions if needed.

    Preserves original schema_version so verify() reproduces the correct hash.
    """
    version = data.get("schema_version", "1.0")
    if version == "1.0":
        data = _migrate_v1_0_to_v1_1(data)
        data = _migrate_v1_1_to_v1_2(data)
        data = _migrate_v1_2_to_v1_3(data)
        data = _migrate_v1_3_to_v1_4(data)
        data = _migrate_v1_4_to_v1_5(data)
    elif version == "1.1":
        data = _migrate_v1_1_to_v1_2(data)
        data = _migrate_v1_2_to_v1_3(data)
        data = _migrate_v1_3_to_v1_4(data)
        data = _migrate_v1_4_to_v1_5(data)
    elif version == "1.2":
        data = _migrate_v1_2_to_v1_3(data)
        data = _migrate_v1_3_to_v1_4(data)
        data = _migrate_v1_4_to_v1_5(data)
    elif version == "1.3":
        data = _migrate_v1_3_to_v1_4(data)
        data = _migrate_v1_4_to_v1_5(data)
    elif version == "1.4":
        data = _migrate_v1_4_to_v1_5(data)

    manifest = Manifest.model_validate(data)

    # Validate encrypted prompt constraint
    for step in manifest.run.steps:
        if step.prompt_visibility == PromptVisibility.ENCRYPTED and not manifest.encryption_scheme:
            raise ManifestError(
                "prompt_visibility='encrypted' requires encryption_scheme to be set"
            )
    return manifest
