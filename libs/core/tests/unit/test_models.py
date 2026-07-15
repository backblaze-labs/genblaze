"""Tests for data models."""

from datetime import UTC, datetime

import pytest
from genblaze_core.exceptions import ManifestError, UnsupportedSchemaVersionError
from genblaze_core.models import (
    Asset,
    Manifest,
    Modality,
    Run,
    Step,
    StepStatus,
    Track,
    WordTiming,
)
from genblaze_core.models.enums import ProviderErrorCode, RunStatus, StepType
from genblaze_core.models.manifest import parse_manifest
from pydantic import ValidationError


def test_asset_defaults():
    a = Asset(url="https://example.com/img.png", media_type="image/png")
    assert a.asset_id
    assert a.sha256 is None
    assert a.metadata == {}


@pytest.mark.parametrize("sha256", ["not-a-sha", "z" * 64, "abc123", "g" * 64])
def test_asset_tolerates_malformed_sha256_on_load(sha256):
    asset = Asset(url="https://example.com/img.png", media_type="image/png", sha256=sha256)

    assert asset.sha256 == sha256


def test_manifest_verify_rejects_uppercase_output_sha256():
    asset = Asset(url="https://example.com/img.png", media_type="image/png", sha256="A" * 64)
    step = Step(
        provider="mock",
        model="m",
        status=StepStatus.SUCCEEDED,
        assets=[asset],
    )
    manifest = Manifest(run=Run(name="uppercase-sha", steps=[step]))
    manifest.compute_hash()

    assert manifest.verify_hash()
    assert manifest.output_asset_ids_missing_sha256() == [asset.asset_id]
    assert not manifest.verify()


def test_asset_tolerates_malformed_sha256_assignment():
    asset = Asset(url="https://example.com/img.png", media_type="image/png")

    asset.sha256 = "not-a-sha"

    assert asset.sha256 == "not-a-sha"


# --- Issue #78: reject impossible numeric/media-type metadata on construction ---
# NOTE: sha256 is intentionally excluded (see test_asset_tolerates_malformed_sha256_*
# above and the comment on Asset in asset.py) — that field stays tolerant at
# construction by design; verify() is the enforcement boundary.


@pytest.mark.parametrize(
    "kwargs",
    [
        {"size_bytes": -1},
        {"width": -640},
        {"width": 0},
        {"height": 0},
        {"height": -480},
        {"duration": -3.5},
        {"duration": float("nan")},
        {"duration": float("inf")},
    ],
)
def test_asset_rejects_impossible_numeric_fields(kwargs):
    with pytest.raises(ValidationError):
        Asset(url="https://example.com/a.png", media_type="image/png", **kwargs)


def test_asset_accepts_valid_numeric_fields():
    asset = Asset(
        url="https://example.com/a.png",
        media_type="image/png",
        size_bytes=1024,
        width=640,
        height=480,
        duration=3.5,
    )
    assert asset.size_bytes == 1024
    assert asset.width == 640
    assert asset.height == 480
    assert asset.duration == 3.5


@pytest.mark.parametrize("media_type", ["not-a-mime", "image", "/png", "image/", ""])
def test_asset_rejects_malformed_media_type(media_type):
    with pytest.raises(ValidationError, match="media_type"):
        Asset(url="https://example.com/a.png", media_type=media_type)


def test_asset_set_hash_still_works_without_caller_changes():
    """Asset.set_hash() must keep working — it always produces valid sha256/size_bytes."""
    asset = Asset(url="https://example.com/a.png", media_type="image/png")
    asset.set_hash(b"some bytes")
    assert asset.sha256 is not None
    from genblaze_core.models.asset import is_valid_sha256

    assert is_valid_sha256(asset.sha256)
    assert asset.size_bytes == len(b"some bytes")


def test_word_timing_rejects_end_before_start():
    with pytest.raises(ValidationError, match="end"):
        WordTiming(word="hi", start=2.0, end=1.0)


def test_word_timing_rejects_negative_start():
    with pytest.raises(ValidationError):
        WordTiming(word="hi", start=-1.0, end=1.0)


def test_word_timing_rejects_out_of_range_confidence():
    with pytest.raises(ValidationError):
        WordTiming(word="hi", start=0.0, end=1.0, confidence=1.5)


def test_word_timing_accepts_valid_bounds():
    wt = WordTiming(word="hi", start=0.0, end=1.0, confidence=0.9)
    assert wt.start == 0.0
    assert wt.end == 1.0


def test_video_metadata_rejects_impossible_values():
    from genblaze_core.models.asset import VideoMetadata

    with pytest.raises(ValidationError):
        VideoMetadata(frame_rate=-30.0)
    with pytest.raises(ValidationError):
        VideoMetadata(bitrate=0)


def test_audio_metadata_rejects_impossible_values():
    from genblaze_core.models.asset import AudioMetadata

    with pytest.raises(ValidationError):
        AudioMetadata(sample_rate=0)
    with pytest.raises(ValidationError):
        AudioMetadata(channels=-1)
    with pytest.raises(ValidationError):
        AudioMetadata(bitrate=-128000)


def test_asset_with_impossible_metadata_no_longer_reaches_manifest():
    """Regression test for the issue #78 repro — construction now fails fast
    instead of letting impossible metadata become canonical provenance data."""
    with pytest.raises(ValidationError):
        Asset(
            url="https://example.com/a.png",
            media_type="image/png",
            size_bytes=-1,
            width=-640,
            height=0,
            duration=-3.5,
        )


def test_step_defaults():
    s = Step(provider="replicate", model="flux-schnell")
    assert s.step_id
    assert s.status == StepStatus.PENDING
    assert s.modality == Modality.IMAGE
    assert s.assets == []


def test_step_rejects_unknown_kwargs():
    """Unrecognized constructor kwargs must raise, not vanish silently (issue #133).

    Pydantic v2's default ``extra="ignore"`` behavior let callers pass
    provider-specific keys (e.g. ``duration=10``) directly to ``Step(...)``
    and have them disappear with no error and no warning — the same class of
    silent data loss as the ``.step(params={...})`` nesting bug. Unknown keys
    belong in ``params={...}``.
    """
    with pytest.raises(ValidationError, match="duration"):
        Step(provider="replicate", model="flux-schnell", duration=10)


def test_run_with_steps():
    s1 = Step(provider="replicate", model="flux-schnell", prompt="a cat")
    s2 = Step(provider="replicate", model="flux-pro", prompt="enhance")
    r = Run(steps=[s1, s2], name="test-run")
    assert len(r.steps) == 2
    assert r.name == "test-run"
    assert r.run_id


def test_manifest_hash_deterministic():
    s = Step(provider="replicate", model="flux-schnell", prompt="a cat")
    r = Run(steps=[s])
    m1 = Manifest(run=r)
    m1.compute_hash()
    m2 = Manifest(run=r)
    m2.compute_hash()
    assert m1.canonical_hash == m2.canonical_hash
    assert m1.canonical_hash != ""


def test_manifest_verify():
    s = Step(provider="replicate", model="flux-schnell", prompt="hello")
    r = Run(steps=[s])
    m = Manifest(run=r)
    m.compute_hash()
    assert m.verify()


def test_manifest_verify_detects_tampering():
    s = Step(provider="replicate", model="flux-schnell", prompt="hello")
    r = Run(steps=[s])
    m = Manifest(run=r)
    m.compute_hash()
    m.run.steps[0].prompt = "tampered"
    assert not m.verify()


def test_manifest_url_only_output_assets_do_not_verify():
    """URL-only outputs are hashable metadata, not asset-byte integrity."""
    base = dict(provider="mock", model="m", prompt="same prompt", status=StepStatus.SUCCEEDED)
    step_a = Step(
        **base,
        assets=[Asset(url="https://cdn.example.com/output-a.png", media_type="image/png")],
    )
    step_b = Step(
        **base,
        assets=[Asset(url="https://cdn.example.com/output-b.png", media_type="image/png")],
    )

    manifest_a = Manifest.from_run(Run(name="same", steps=[step_a]))
    manifest_b = Manifest.from_run(Run(name="same", steps=[step_b]))

    assert manifest_a.verify_hash()
    assert manifest_b.verify_hash()
    assert manifest_a.canonical_hash == manifest_b.canonical_hash
    assert not manifest_a.verify()
    assert not manifest_b.verify()


def test_manifest_v1_5_url_only_inputs_keep_legacy_hash_rules():
    """Schema 1.5 preserves old URL-stripping rules for existing manifests."""
    base = dict(provider="mock", model="m", prompt="same prompt", status=StepStatus.SUCCEEDED)
    output = Asset(
        url="https://cdn.example.com/output.png",
        media_type="image/png",
        sha256="b" * 64,
    )
    step_a = Step(
        **base,
        assets=[output],
        inputs=[Asset(url="https://cdn.example.com/input-a.png", media_type="image/png")],
    )
    step_b = Step(
        **base,
        assets=[output.model_copy(deep=True)],
        inputs=[Asset(url="https://cdn.example.com/input-b.png", media_type="image/png")],
    )

    manifest_a = Manifest(run=Run(name="same", steps=[step_a]), schema_version="1.5")
    manifest_b = Manifest(run=Run(name="same", steps=[step_b]), schema_version="1.5")
    manifest_a.compute_hash()
    manifest_b.compute_hash()

    assert manifest_a.canonical_hash == manifest_b.canonical_hash
    assert manifest_a.verify_hash()
    assert manifest_a.verify()
    assert manifest_a.output_asset_ids_missing_sha256() == []


def test_manifest_v1_6_url_only_inputs_are_metadata_bound():
    """Schema 1.6 includes URL-only inputs in the metadata hash payload."""
    base = dict(provider="mock", model="m", prompt="same prompt", status=StepStatus.SUCCEEDED)
    output = Asset(
        url="https://cdn.example.com/output.png",
        media_type="image/png",
        sha256="c" * 64,
    )
    step_a = Step(
        **base,
        assets=[output],
        inputs=[Asset(url="https://cdn.example.com/input-a.png", media_type="image/png")],
    )
    step_b = Step(
        **base,
        assets=[output.model_copy(deep=True)],
        inputs=[Asset(url="https://cdn.example.com/input-b.png", media_type="image/png")],
    )

    manifest_a = Manifest(run=Run(name="same", steps=[step_a]), schema_version="1.6")
    manifest_b = Manifest(run=Run(name="same", steps=[step_b]), schema_version="1.6")
    manifest_a.compute_hash()
    manifest_b.compute_hash()

    assert manifest_a.canonical_hash != manifest_b.canonical_hash
    assert manifest_a.verify_hash()
    assert manifest_a.verify()


def test_manifest_v1_6_verify_hash_readable_but_not_serializable():
    step = Step(
        provider="mock",
        model="m",
        prompt="same prompt",
        status=StepStatus.SUCCEEDED,
        assets=[
            Asset(
                url="https://cdn.example.com/output.png",
                media_type="image/png",
                sha256="c" * 64,
            )
        ],
    )
    manifest = Manifest(run=Run(name="same", steps=[step]), schema_version="1.6")
    manifest.compute_hash()

    assert manifest.verify_hash()
    assert manifest.verify()
    with pytest.raises(ManifestError, match="read-supported only"):
        manifest.to_canonical_json()


def test_manifest_v1_5_reports_missing_output_sha_and_fails_verify():
    """Security-facing verification rejects URL-only outputs in legacy schemas."""
    step = Step(
        provider="mock",
        model="m",
        prompt="same prompt",
        status=StepStatus.SUCCEEDED,
        assets=[Asset(url="https://cdn.example.com/output.png", media_type="image/png")],
    )
    manifest = Manifest(run=Run(name="same", steps=[step]), schema_version="1.5")
    manifest.compute_hash()

    assert manifest.verify_hash()
    assert not manifest.verify()
    assert manifest.output_asset_ids_missing_sha256() == [step.assets[0].asset_id]
    report = manifest.verification_report()
    assert report.hash_ok
    assert report.unverified_sha256_ids == (step.assets[0].asset_id,)
    assert not report.ok


def test_manifest_verify_rejects_malformed_output_sha256_if_bypassed():
    asset = Asset(
        url="https://cdn.example.com/output.png",
        media_type="image/png",
        sha256="not-a-sha",
    )
    step = Step(
        provider="mock",
        model="m",
        prompt="same prompt",
        status=StepStatus.SUCCEEDED,
        assets=[asset],
    )
    manifest = Manifest.from_run(Run(name="same", steps=[step]))

    assert manifest.verify_hash()
    assert manifest.output_asset_ids_missing_sha256() == [asset.asset_id]
    assert not manifest.verify()


def test_parse_manifest_tolerates_malformed_sha256_but_verify_rejects():
    asset = Asset(
        url="https://cdn.example.com/output.png",
        media_type="image/png",
        sha256="SHA256:" + "a" * 64,
    )
    step = Step(
        provider="mock",
        model="m",
        prompt="same prompt",
        status=StepStatus.SUCCEEDED,
        assets=[asset],
    )
    manifest = Manifest(run=Run(name="same", steps=[step]))
    manifest.compute_hash()

    parsed = parse_manifest(manifest.model_dump(mode="python"))

    assert parsed.verify_hash()
    assert parsed.output_asset_ids_missing_sha256() == [asset.asset_id]
    assert not parsed.verify()


def test_parse_manifest_tolerates_impossible_asset_metadata_but_verify_rejects():
    """#149: an older/foreign manifest with width=0 (a common "unknown
    dimensions" placeholder) or a nonstandard media_type must still load via
    parse_manifest() instead of raising ValidationError. verify() is the
    enforcement boundary — mirroring the sha256 tolerance pattern.
    """
    # Build the "foreign" asset via the same tolerant path parse_manifest()
    # uses (Asset(...) construction still rejects these values by design).
    asset = Asset.model_validate(
        {
            "url": "https://cdn.example.com/output.png",
            "media_type": "unknown",
            "sha256": "a" * 64,
            "width": 0,
            "height": 0,
        },
        context={"tolerant_load": True},
    )
    step = Step(
        provider="mock",
        model="m",
        prompt="same prompt",
        status=StepStatus.SUCCEEDED,
        assets=[asset],
    )
    manifest = Manifest(run=Run(name="same", steps=[step]))
    manifest.compute_hash()

    parsed = parse_manifest(manifest.model_dump(mode="python"))

    assert parsed.run.steps[0].assets[0].width == 0
    assert parsed.run.steps[0].assets[0].media_type == "unknown"
    assert parsed.verify_hash()
    assert parsed.output_asset_ids_with_invalid_metadata() == [asset.asset_id]
    assert not parsed.verify()


@pytest.mark.parametrize("schema_version", ["0", "1.7", "2.0", "1.x"])
def test_manifest_unsupported_schema_version_is_rejected(schema_version):
    step = Step(provider="mock", model="m", prompt="same prompt")

    with pytest.raises(ValidationError, match="Unsupported schema_version"):
        Manifest(run=Run(name="same", steps=[step]), schema_version=schema_version)


def test_parse_manifest_unsupported_schema_version_has_clear_error():
    step = Step(provider="mock", model="m", prompt="same prompt")
    manifest = Manifest.from_run(Run(name="same", steps=[step]))
    data = manifest.model_dump(mode="python")
    data["schema_version"] = "1.7"

    with pytest.raises(UnsupportedSchemaVersionError, match="Upgrade genblaze-core"):
        parse_manifest(data)


@pytest.mark.parametrize("schema_version", ["1.0", "1.4", "1.5"])
def test_schema_downgrade_cannot_bypass_url_only_output_verification(schema_version):
    step = Step(
        provider="mock",
        model="m",
        prompt="same prompt",
        status=StepStatus.SUCCEEDED,
        assets=[Asset(url="https://cdn.example.com/output.png", media_type="image/png")],
    )
    manifest = Manifest(run=Run(name="same", steps=[step]), schema_version=schema_version)
    manifest.compute_hash()

    assert manifest.verify_hash()
    assert not manifest.verify()


def test_manifest_v1_6_url_only_hash_strips_presign_query_params():
    base = dict(provider="mock", model="m", prompt="same prompt", status=StepStatus.SUCCEEDED)
    step_a = Step(
        **base,
        assets=[
            Asset(
                url="https://cdn.example.com/output.png?X-Amz-Signature=a",
                media_type="image/png",
            )
        ],
    )
    step_b = Step(
        **base,
        assets=[
            Asset(
                url="https://CDN.EXAMPLE.COM/output.png?X-Amz-Signature=b",
                media_type="image/png",
            )
        ],
    )

    manifest_a = Manifest(run=Run(name="same", steps=[step_a]), schema_version="1.6")
    manifest_b = Manifest(run=Run(name="same", steps=[step_b]), schema_version="1.6")
    manifest_a.compute_hash()
    manifest_b.compute_hash()

    assert manifest_a.canonical_hash == manifest_b.canonical_hash
    assert manifest_a.verify_hash()
    assert not manifest_a.verify()


def test_manifest_v1_6_url_only_hash_strips_url_userinfo():
    base = dict(provider="mock", model="m", prompt="same prompt", status=StepStatus.SUCCEEDED)
    step_a = Step(
        **base,
        assets=[
            Asset(
                url="https://alice:secret@cdn.example.com/output.png",
                media_type="image/png",
            )
        ],
    )
    step_b = Step(
        **base,
        assets=[
            Asset(
                url="https://cdn.example.com/output.png",
                media_type="image/png",
            )
        ],
    )

    manifest_a = Manifest(run=Run(name="same", steps=[step_a]), schema_version="1.6")
    manifest_b = Manifest(run=Run(name="same", steps=[step_b]), schema_version="1.6")
    manifest_a.compute_hash()
    manifest_b.compute_hash()

    assert manifest_a.canonical_hash == manifest_b.canonical_hash


def test_manifest_v1_6_url_only_hash_keeps_resource_query_params():
    base = dict(provider="mock", model="m", prompt="same prompt", status=StepStatus.SUCCEEDED)
    step_a = Step(
        **base,
        assets=[
            Asset(
                url="https://cdn.example.com/download?id=benign&X-Amz-Signature=a",
                media_type="image/png",
            )
        ],
    )
    step_b = Step(
        **base,
        assets=[
            Asset(
                url="https://cdn.example.com/download?id=evil&X-Amz-Signature=a",
                media_type="image/png",
            )
        ],
    )

    manifest_a = Manifest(run=Run(name="same", steps=[step_a]), schema_version="1.6")
    manifest_b = Manifest(run=Run(name="same", steps=[step_b]), schema_version="1.6")
    manifest_a.compute_hash()
    manifest_b.compute_hash()

    assert manifest_a.canonical_hash != manifest_b.canonical_hash
    assert manifest_a.verify_hash()
    assert manifest_b.verify_hash()


def test_manifest_v1_6_url_only_hash_preserves_plus_resource_query_params():
    plus = _v1_6_hash_for_output_url("https://cdn.example.com/download?id=a+b&X-Amz-Signature=a")
    encoded_plus = _v1_6_hash_for_output_url(
        "https://cdn.example.com/download?id=a%2Bb&X-Amz-Signature=a"
    )
    space = _v1_6_hash_for_output_url(
        "https://cdn.example.com/download?id=a%20b&X-Amz-Signature=a"
    )

    assert plus == encoded_plus
    assert plus != space


def test_manifest_v1_6_url_only_hash_preserves_bare_query_params():
    bare = _v1_6_hash_for_output_url("https://cdn.example.com/download?flag&X-Amz-Signature=a")
    empty = _v1_6_hash_for_output_url("https://cdn.example.com/download?flag=&X-Amz-Signature=a")

    assert bare != empty


@pytest.mark.parametrize("param", ["token", "sig", "signature", "credential", "policy"])
def test_manifest_v1_6_url_only_hash_keeps_generic_resource_query_params(param):
    base = dict(provider="mock", model="m", prompt="same prompt", status=StepStatus.SUCCEEDED)
    step_a = Step(
        **base,
        assets=[
            Asset(
                url=f"https://cdn.example.com/download?{param}=doc-a",
                media_type="image/png",
            )
        ],
    )
    step_b = Step(
        **base,
        assets=[
            Asset(
                url=f"https://cdn.example.com/download?{param}=doc-b",
                media_type="image/png",
            )
        ],
    )

    manifest_a = Manifest(run=Run(name="same", steps=[step_a]), schema_version="1.6")
    manifest_b = Manifest(run=Run(name="same", steps=[step_b]), schema_version="1.6")
    manifest_a.compute_hash()
    manifest_b.compute_hash()

    assert manifest_a.canonical_hash != manifest_b.canonical_hash


def _v1_6_hash_for_output_url(url: str) -> str:
    step = Step(
        provider="mock",
        model="m",
        prompt="same prompt",
        status=StepStatus.SUCCEEDED,
        assets=[Asset(url=url, media_type="image/png")],
    )
    manifest = Manifest(run=Run(name="same", steps=[step]), schema_version="1.6")
    manifest.compute_hash()
    return manifest.canonical_hash


def test_manifest_v1_6_url_only_hash_strips_cloudfront_signed_query_params():
    a = _v1_6_hash_for_output_url(
        "https://d111.cloudfront.net/output.png?Policy=policy-a&Signature=sig-a&Key-Pair-Id=K123"
    )
    b = _v1_6_hash_for_output_url(
        "https://d111.cloudfront.net/output.png?Policy=policy-b&Signature=sig-b&Key-Pair-Id=K123"
    )
    other_resource = _v1_6_hash_for_output_url(
        "https://d111.cloudfront.net/other.png?Policy=policy-b&Signature=sig-b&Key-Pair-Id=K123"
    )

    assert a == b
    assert a != other_resource


def test_manifest_v1_6_url_only_hash_strips_azure_sas_query_params():
    a = _v1_6_hash_for_output_url(
        "https://acct.blob.core.windows.net/container/output.png?"
        "sv=2024-11-04&se=2026-01-01T00:00:00Z&sp=r&sr=b&sig=sig-a"
    )
    b = _v1_6_hash_for_output_url(
        "https://acct.blob.core.windows.net/container/output.png?"
        "sv=2025-05-05&se=2026-02-01T00:00:00Z&sp=r&sr=b&sig=sig-b"
    )
    other_resource = _v1_6_hash_for_output_url(
        "https://acct.blob.core.windows.net/container/other.png?"
        "sv=2025-05-05&se=2026-02-01T00:00:00Z&sp=r&sr=b&sig=sig-b"
    )

    assert a == b
    assert a != other_resource


def test_manifest_v1_6_url_only_hash_strips_gcs_v2_signed_query_params():
    a = _v1_6_hash_for_output_url(
        "https://storage.googleapis.com/bucket/output.png?"
        "GoogleAccessId=signer-a&Expires=1800000000&Signature=sig-a"
    )
    b = _v1_6_hash_for_output_url(
        "https://storage.googleapis.com/bucket/output.png?"
        "GoogleAccessId=signer-b&Expires=1900000000&Signature=sig-b"
    )
    other_resource = _v1_6_hash_for_output_url(
        "https://storage.googleapis.com/bucket/other.png?"
        "GoogleAccessId=signer-b&Expires=1900000000&Signature=sig-b"
    )

    assert a == b
    assert a != other_resource


def test_manifest_hashed_output_assets_verify_with_url_rewrites():
    """sha256, not URL, is the provenance identity once bytes are declared."""
    base = dict(provider="mock", model="m", prompt="same prompt", status=StepStatus.SUCCEEDED)
    step_a = Step(
        **base,
        assets=[
            Asset(
                url="https://cdn-a.example.com/output.png",
                media_type="image/png",
                sha256="a" * 64,
                size_bytes=3,
            )
        ],
    )
    step_b = Step(
        **base,
        assets=[
            Asset(
                url="https://cdn-b.example.com/output.png",
                media_type="image/png",
                sha256="a" * 64,
                size_bytes=3,
            )
        ],
    )

    manifest_a = Manifest.from_run(Run(name="same", steps=[step_a]))
    manifest_b = Manifest.from_run(Run(name="same", steps=[step_b]))

    assert manifest_a.canonical_hash == manifest_b.canonical_hash
    assert manifest_a.verify()
    assert manifest_b.verify()
    assert manifest_a.verification_report().ok


def test_hash_excludes_operational_fields():
    """Operational fields (status, timestamps, errors) must not affect the hash."""
    s = Step(provider="replicate", model="flux-schnell", prompt="a cat")
    r = Run(steps=[s])
    m = Manifest(run=r)
    m.compute_hash()
    original_hash = m.canonical_hash
    assert m.verify(), "Hash should verify before any mutation"

    # Mutate all operational fields — hash should remain the same
    s.status = StepStatus.FAILED
    s.error = "something broke"
    s.error_code = ProviderErrorCode.TIMEOUT
    s.retries = 3
    s.started_at = datetime(2026, 1, 1, tzinfo=UTC)
    s.completed_at = datetime(2026, 1, 2, tzinfo=UTC)
    s.provider_payload = {"replicate": {"prediction_id": "abc"}}
    s.step_index = 5
    r.status = RunStatus.COMPLETED
    r.created_at = datetime(2025, 6, 1, tzinfo=UTC)
    r.started_at = datetime(2025, 6, 1, tzinfo=UTC)
    r.completed_at = datetime(2025, 6, 2, tzinfo=UTC)
    r.parent_run_id = "parent-abc"
    r.idempotency_key = "idem-123"

    assert m.verify(), "Hash should still verify after operational field changes"
    m.compute_hash()
    assert m.canonical_hash == original_hash


def test_hash_changes_on_provenance_fields():
    """Provenance fields (prompt, model, params, seed) must affect the hash."""
    s = Step(provider="replicate", model="flux-schnell", prompt="a cat")
    r = Run(steps=[s])
    m = Manifest(run=r)
    m.compute_hash()
    original_hash = m.canonical_hash

    # Changing prompt is a provenance change
    s.prompt = "a dog"
    m.compute_hash()
    assert m.canonical_hash != original_hash


# --- New model fields ---


def test_step_retryable_property():
    """Step.retryable is True only for transient error codes."""
    s = Step(provider="p", model="m")
    assert not s.retryable  # no error_code

    s.error_code = ProviderErrorCode.TIMEOUT
    assert s.retryable

    s.error_code = ProviderErrorCode.RATE_LIMIT
    assert s.retryable

    s.error_code = ProviderErrorCode.SERVER_ERROR
    assert s.retryable

    s.error_code = ProviderErrorCode.AUTH_FAILURE
    assert not s.retryable

    s.error_code = ProviderErrorCode.INVALID_INPUT
    assert not s.retryable


def test_step_index_default_none():
    """step_index is None by default, set by RunBuilder."""
    s = Step(provider="p", model="m")
    assert s.step_index is None


def test_run_new_fields_default_none():
    """New Run fields default to None."""
    r = Run(steps=[])
    assert r.parent_run_id is None
    assert r.idempotency_key is None
    assert r.started_at is None
    assert r.completed_at is None


def test_run_builder_sets_step_index():
    """RunBuilder.build() assigns step_index to each step."""
    from genblaze_core.builders.run_builder import RunBuilder

    s1 = Step(provider="p", model="m", prompt="a")
    s2 = Step(provider="p", model="m", prompt="b")
    builder = RunBuilder("test")
    builder.add_step(s1).add_step(s2)
    run = builder.build()

    assert run.steps[0].step_index == 0
    assert run.steps[1].step_index == 1


# --- Manifest migration ---


def test_parse_manifest_v1_0_verify_roundtrip():
    """verify() works on manifests originally created under schema v1.0."""
    from genblaze_core.models.manifest import parse_manifest

    # Simulate a v1.0 manifest (no cost_usd, schema_version="1.0")
    s = Step(provider="replicate", model="flux", prompt="cat")
    r = Run(steps=[s])
    m = Manifest(run=r, schema_version="1.0")
    m.compute_hash()
    assert m.verify()

    # Serialize and re-parse through the migration path
    data = m.model_dump(mode="python")
    # Remove cost_usd to simulate v1.0 data
    for step in data["run"]["steps"]:
        step.pop("cost_usd", None)

    parsed = parse_manifest(data)
    assert parsed.verify(), "verify() must pass after v1.0 → v1.1 migration"
    assert parsed.schema_version == "1.0"


def test_parse_manifest_missing_schema_version_preserves_v1_0_policy():
    """Schema-less historical manifests parse with the v1.0 hash policy."""
    step = Step(provider="replicate", model="flux", prompt="cat")
    run = Run(steps=[step])
    manifest = Manifest(run=run, schema_version="1.0")
    manifest.compute_hash()
    data = manifest.model_dump(mode="python")
    data.pop("schema_version")
    for step_data in data["run"]["steps"]:
        step_data.pop("cost_usd", None)

    parsed = parse_manifest(data)

    assert parsed.schema_version == "1.0"
    assert parsed.verify_hash()


# --- EDIT StepType ---


def test_edit_step_type_exists():
    """EDIT is a valid StepType value."""
    assert StepType.EDIT == "edit"
    assert StepType.EDIT in list(StepType)


def test_step_with_edit_type():
    """Steps can use EDIT step_type for video editing operations."""
    s = Step(
        provider="runway",
        model="gen4_turbo",
        prompt="extend this clip",
        step_type=StepType.EDIT,
        params={"edit_type": "extend"},
    )
    assert s.step_type == StepType.EDIT
    assert s.params["edit_type"] == "extend"


# --- Schema 1.2 migration ---


def test_new_manifest_gets_current_schema_version():
    """New manifests default to the current SCHEMA_VERSION constant."""
    from genblaze_core.models.manifest import SCHEMA_VERSION

    assert SCHEMA_VERSION == "1.5"
    s = Step(provider="p", model="m")
    r = Run(steps=[s])
    m = Manifest.from_run(r)
    assert m.schema_version == "1.5"


def test_equivalent_reruns_produce_same_hash():
    """Two semantically identical runs with different IDs produce the same hash."""
    s1 = Step(provider="replicate", model="flux-schnell", prompt="a cat")
    r1 = Run(steps=[s1])
    m1 = Manifest.from_run(r1)

    s2 = Step(provider="replicate", model="flux-schnell", prompt="a cat")
    r2 = Run(steps=[s2])
    m2 = Manifest.from_run(r2)

    # run_id, step_id, and asset_id are all different
    assert r1.run_id != r2.run_id
    assert s1.step_id != s2.step_id
    # But hashes are identical because IDs are excluded
    assert m1.canonical_hash == m2.canonical_hash


def test_asset_provenance_key_ignores_asset_id_and_url():
    """Regression for issue #76: the content-based sort key used by
    Pipeline.ingest must tie for assets that differ only in asset_id/url —
    the same fields _hash_payload excludes — and differ when actual
    hash-relevant content (sha256) differs."""
    from genblaze_core.models.manifest import asset_provenance_key

    a1 = Asset(url="https://x/a.mp3", media_type="audio/mp3", sha256="a" * 64, size_bytes=1)
    a2 = Asset(
        url="https://x/different.mp3", media_type="audio/mp3", sha256="a" * 64, size_bytes=1
    )
    assert a1.asset_id != a2.asset_id
    assert asset_provenance_key(a1) == asset_provenance_key(a2)

    b = Asset(url="https://x/b.mp3", media_type="audio/mp3", sha256="b" * 64, size_bytes=1)
    assert asset_provenance_key(a1) != asset_provenance_key(b)


def test_old_v1_3_manifest_still_verifies():
    """Manifests created under v1.3 (IDs included in hash) still verify."""
    from genblaze_core.models.manifest import parse_manifest

    s = Step(provider="replicate", model="flux", prompt="cat")
    r = Run(steps=[s])
    m = Manifest(run=r, schema_version="1.3")
    m.compute_hash()
    assert m.verify()

    # Round-trip through parse_manifest
    data = m.model_dump(mode="python")
    parsed = parse_manifest(data)
    assert parsed.verify(), "v1.3 manifest must still verify after parsing"
    assert parsed.schema_version == "1.3"


def test_parse_manifest_v1_1_verify_roundtrip():
    """verify() works on manifests originally created under schema v1.1."""
    from genblaze_core.models.manifest import parse_manifest

    s = Step(provider="replicate", model="flux", prompt="cat")
    r = Run(steps=[s])
    m = Manifest(run=r, schema_version="1.1")
    m.compute_hash()
    assert m.verify()

    data = m.model_dump(mode="python")
    parsed = parse_manifest(data)
    assert parsed.verify(), "verify() must pass after v1.1 → v1.2 migration"
    assert parsed.schema_version == "1.1"


def test_parse_manifest_v1_0_through_1_2():
    """v1.0 manifests migrate through v1.1 → v1.2 and still verify."""
    from genblaze_core.models.manifest import parse_manifest

    s = Step(provider="replicate", model="flux", prompt="cat")
    r = Run(steps=[s])
    m = Manifest(run=r, schema_version="1.0")
    m.compute_hash()

    data = m.model_dump(mode="python")
    for step in data["run"]["steps"]:
        step.pop("cost_usd", None)

    parsed = parse_manifest(data)
    assert parsed.verify(), "verify() must pass after v1.0 → v1.2 migration"
    assert parsed.schema_version == "1.0"


def test_parse_manifest_native_v1_2_roundtrip():
    """Native v1.2 manifests round-trip through parse_manifest without migration."""
    from genblaze_core.models.manifest import parse_manifest

    s = Step(
        provider="runway",
        model="gen4_turbo",
        prompt="extend clip",
        step_type=StepType.EDIT,
        params={"edit_type": "extend"},
    )
    r = Run(steps=[s])
    m = Manifest(run=r, schema_version="1.2")
    m.compute_hash()
    assert m.verify()

    data = m.model_dump(mode="python")
    parsed = parse_manifest(data)
    assert parsed.verify(), "Native v1.2 manifest must round-trip"
    assert parsed.schema_version == "1.2"
    assert parsed.run.steps[0].step_type == StepType.EDIT


# --- WordTiming ---


def test_word_timing_model():
    """WordTiming model holds word, start, end, and optional confidence."""
    wt = WordTiming(word="hello", start=0.0, end=0.5)
    assert wt.word == "hello"
    assert wt.start == 0.0
    assert wt.end == 0.5
    assert wt.confidence is None

    wt2 = WordTiming(word="world", start=0.5, end=1.0, confidence=0.95)
    assert wt2.confidence == 0.95


def test_audio_metadata_word_timings_default_none():
    """word_timings defaults to None on AudioMetadata."""
    from genblaze_core.models.asset import AudioMetadata

    am = AudioMetadata(channels=1)
    assert am.word_timings is None


def test_audio_metadata_word_timings_typed():
    """AudioMetadata accepts typed WordTiming objects."""
    from genblaze_core.models.asset import AudioMetadata

    timings = [
        WordTiming(word="hello", start=0.0, end=0.3),
        WordTiming(word="world", start=0.3, end=0.7),
    ]
    am = AudioMetadata(channels=1, word_timings=timings)
    assert len(am.word_timings) == 2
    assert am.word_timings[0].word == "hello"
    assert am.word_timings[1].end == 0.7


def test_audio_metadata_word_timings_backward_compat_from_dicts():
    """Raw dicts in word_timings are coerced to WordTiming for backward compat."""
    from genblaze_core.models.asset import AudioMetadata

    raw = [
        {"word": "hello", "start": 0.0, "end": 0.5},
        {"word": "world", "start": 0.5, "end": 1.0, "confidence": 0.9},
    ]
    am = AudioMetadata(channels=1, word_timings=raw)
    assert all(isinstance(wt, WordTiming) for wt in am.word_timings)
    assert am.word_timings[0].word == "hello"
    assert am.word_timings[1].confidence == 0.9


def test_word_timing_serialization_roundtrip():
    """WordTiming data survives JSON serialization round-trip through Asset."""
    from genblaze_core.models.asset import AudioMetadata

    timings = [
        WordTiming(word="a", start=0.0, end=0.1, confidence=0.99),
        WordTiming(word="b", start=0.1, end=0.2),
    ]
    a = Asset(
        url="file:///tmp/test.mp3",
        media_type="audio/mpeg",
        audio=AudioMetadata(channels=1, word_timings=timings),
    )
    data = a.model_dump(mode="json")
    restored = Asset(**data)
    assert len(restored.audio.word_timings) == 2
    assert restored.audio.word_timings[0].confidence == 0.99
    assert restored.audio.word_timings[1].confidence is None


def test_track_creation():
    t = Track(kind="video", codec="h264", label="main")
    assert t.kind == "video"
    assert t.codec == "h264"
    assert t.label == "main"


def test_track_defaults():
    t = Track(kind="audio")
    assert t.codec is None
    assert t.label is None


def test_asset_with_tracks():
    tracks = [
        Track(kind="video", codec="h264"),
        Track(kind="audio", codec="aac", label="generated-audio"),
    ]
    a = Asset(url="https://example.com/out.mp4", media_type="video/mp4", tracks=tracks)
    assert len(a.tracks) == 2
    assert a.tracks[0].kind == "video"
    assert a.tracks[1].label == "generated-audio"


def test_asset_tracks_serialization_roundtrip():
    """Asset with tracks survives JSON serialization round-trip."""
    tracks = [
        Track(kind="video", codec="h264"),
        Track(kind="audio", codec="aac", label="generated-audio"),
    ]
    a = Asset(url="https://example.com/out.mp4", media_type="video/mp4", tracks=tracks)
    data = a.model_dump(mode="json")
    restored = Asset.model_validate(data)
    assert len(restored.tracks) == 2
    assert restored.tracks[0].codec == "h264"
    assert restored.tracks[1].label == "generated-audio"
