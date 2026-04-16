"""Tests for data models."""

from datetime import UTC, datetime

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


def test_asset_defaults():
    a = Asset(url="https://example.com/img.png", media_type="image/png")
    assert a.asset_id
    assert a.sha256 is None
    assert a.metadata == {}


def test_step_defaults():
    s = Step(provider="replicate", model="flux-schnell")
    assert s.step_id
    assert s.status == StepStatus.PENDING
    assert s.modality == Modality.IMAGE
    assert s.assets == []


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


def test_new_manifest_gets_schema_1_4():
    """New manifests default to schema version 1.4."""
    from genblaze_core.models.manifest import SCHEMA_VERSION

    assert SCHEMA_VERSION == "1.4"
    s = Step(provider="p", model="m")
    r = Run(steps=[s])
    m = Manifest.from_run(r)
    assert m.schema_version == "1.4"


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
