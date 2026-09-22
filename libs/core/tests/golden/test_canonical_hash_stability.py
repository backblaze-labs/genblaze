"""Golden test — canonical hash must be stable across versions.

If this test fails, the canonical JSON or hashing algorithm changed in a
backwards-incompatible way. Do NOT update the golden values unless you are
intentionally making a breaking change to the manifest format.
"""

from genblaze_core.canonical.json import canonical_hash, canonical_json

# Pinned input: a representative manifest-like structure
_GOLDEN_INPUT = {
    "schema_version": "1.1",
    "run": {
        "run_id": "00000000-0000-0000-0000-000000000001",
        "name": "golden-test",
        "tenant_id": None,
        "project_id": None,
        "steps": [
            {
                "step_id": "00000000-0000-0000-0000-000000000002",
                "provider": "replicate",
                "model": "black-forest-labs/flux-schnell",
                "prompt": "a golden cat sitting on a throne",
                "params": {"num_outputs": 1, "guidance_scale": 7.5},
                "modality": "image",
                "step_type": "generate",
                "seed": 42,
                "inputs": [],
                "assets": [
                    {
                        "asset_id": "00000000-0000-0000-0000-000000000003",
                        "url": "https://example.com/cat.png",
                        "media_type": "image/png",
                        "sha256": None,
                        "size_bytes": None,
                    }
                ],
                "metadata": {},
            }
        ],
        "metadata": {},
    },
}

# Pinned canonical JSON — if this changes, the manifest format broke
_GOLDEN_JSON = '{"run":{"metadata":{},"name":"golden-test","project_id":null,"run_id":"00000000-0000-0000-0000-000000000001","steps":[{"assets":[{"asset_id":"00000000-0000-0000-0000-000000000003","media_type":"image/png","sha256":null,"size_bytes":null,"url":"https://example.com/cat.png"}],"inputs":[],"metadata":{},"modality":"image","model":"black-forest-labs/flux-schnell","params":{"guidance_scale":7.5,"num_outputs":1},"prompt":"a golden cat sitting on a throne","provider":"replicate","seed":42,"step_id":"00000000-0000-0000-0000-000000000002","step_type":"generate"}],"tenant_id":null},"schema_version":"1.1"}'  # noqa: E501

# Pinned SHA-256 hash — if this changes, the hashing algorithm broke
_GOLDEN_HASH = "868274e5975fc9f01489a8c21082824eaaf0470f319be62998ebe0e20a5963c2"


def test_canonical_json_stability():
    """Canonical JSON output must not change."""
    assert canonical_json(_GOLDEN_INPUT) == _GOLDEN_JSON


def test_canonical_hash_stability():
    """Canonical hash must not change."""
    assert canonical_hash(_GOLDEN_INPUT) == _GOLDEN_HASH


def test_hash_is_hex_sha256():
    """Hash must be a 64-char lowercase hex string (SHA-256)."""
    h = canonical_hash(_GOLDEN_INPUT)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_idempotence():
    """Hashing the same input twice must produce the same result."""
    assert canonical_hash(_GOLDEN_INPUT) == canonical_hash(_GOLDEN_INPUT)


# Manifest hashes pinned before Step.failed_attempts existed (#239). A step
# with no failed attempts must hash byte-identically, or every manifest written
# by an earlier release would stop verifying.
_PRE_ATTEMPTS_HASHES = {
    "1.3": "8322b24363522942a386108b7ec216c61410ae36bfac4602bfd6490e12768786",
    "1.5": "3a47dc2513956ed72fa474d33ef1ad93ecfbc269733647774818e2a4e5cee184",
}


def _pinned_run():
    from datetime import UTC, datetime

    from genblaze_core.models.asset import Asset
    from genblaze_core.models.enums import StepStatus
    from genblaze_core.models.run import Run
    from genblaze_core.models.step import Step

    step = Step(
        step_id="00000000-0000-0000-0000-000000000002",
        provider="replicate",
        model="flux",
        prompt="cat",
        seed=42,
        status=StepStatus.SUCCEEDED,
        metadata={"fallback_from": "primary", "fallback_model": "flux"},
        assets=[
            Asset(
                asset_id="00000000-0000-0000-0000-000000000003",
                url="https://example.com/cat.png",
                media_type="image/png",
                sha256="a" * 64,
            )
        ],
    )
    return Run(
        run_id="00000000-0000-0000-0000-000000000001",
        name="golden",
        steps=[step],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_manifest_hash_unchanged_without_failed_attempts():
    """An empty failed_attempts list must not enter the hash payload."""
    from genblaze_core.models.manifest import Manifest

    run = _pinned_run()
    for version, expected in _PRE_ATTEMPTS_HASHES.items():
        assert Manifest(run=run, schema_version=version).compute_hash() == expected


def test_manifest_without_failed_attempts_omits_key_on_wire():
    """Older readers reject unknown Step keys (extra="forbid"), so the key is
    only serialized when there is something to record."""
    from genblaze_core.models.manifest import Manifest

    assert "failed_attempts" not in Manifest.from_run(_pinned_run()).to_canonical_json()
