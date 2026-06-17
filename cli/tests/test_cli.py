"""Tests for CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from genblaze_cli.main import cli
from genblaze_core.builders import RunBuilder, StepBuilder
from genblaze_core.models.asset import Asset
from genblaze_core.media.png import PngHandler
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepStatus
from genblaze_core.models.manifest import Manifest
from genblaze_core.testing import MockProvider
from PIL import Image


def _create_manifest_json(tmp_path: Path) -> Path:
    """Create a manifest JSON file for testing."""
    step = StepBuilder("test", "test-model").prompt("hello world").modality(Modality.IMAGE).build()
    run = RunBuilder("cli-test").add_step(step).build()
    manifest = Manifest.from_run(run)

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.to_canonical_json(), encoding="utf-8")
    return manifest_path


def _create_embedded_png(tmp_path: Path) -> Path:
    """Create a PNG with an embedded manifest."""
    step = StepBuilder("test", "test-model").prompt("hello").build()
    run = RunBuilder("png-test").add_step(step).build()
    manifest = Manifest.from_run(run)

    png_path = tmp_path / "test.png"
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(png_path)
    PngHandler().embed(png_path, manifest)
    return png_path


def _create_url_only_manifest(*, schema_version: str | None = None) -> Manifest:
    """Create a hash-valid manifest whose output assets are not byte-bound."""
    step = StepBuilder("test", "test-model").prompt("hello").build()
    step.status = StepStatus.SUCCEEDED
    step.assets = [Asset(url="https://cdn.example.com/output.png", media_type="image/png")]
    run = RunBuilder("url-only").add_step(step).build()
    if schema_version is None:
        return Manifest.from_run(run)
    manifest = Manifest(run=run, schema_version=schema_version)
    manifest.compute_hash()
    return manifest


def test_extract_json(tmp_path: Path) -> None:
    png = _create_embedded_png(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["extract", str(png)])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "canonical_hash" in data
    assert "run" in data


def test_extract_summary(tmp_path: Path) -> None:
    png = _create_embedded_png(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["extract", "--format", "summary", str(png)])
    assert result.exit_code == 0
    assert "Run ID:" in result.output
    assert "Verified:" in result.output


def test_verify_ok(tmp_path: Path) -> None:
    png = _create_embedded_png(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["verify", str(png)])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_verify_distinguishes_unverified_assets(tmp_path: Path) -> None:
    manifest = _create_url_only_manifest()
    png_path = tmp_path / "url-only.png"
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(png_path)
    PngHandler().embed(png_path, manifest)

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", str(png_path)])
    combined = result.output + getattr(result, "stderr", "")
    assert result.exit_code != 0
    assert "1 output asset(s) missing sha256" in combined
    assert "hash mismatch" not in combined


def test_verify_reports_legacy_unverified_assets(tmp_path: Path) -> None:
    manifest = _create_url_only_manifest(schema_version="1.5")
    png_path = tmp_path / "legacy-url-only.png"
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(png_path)
    PngHandler().embed(png_path, manifest)

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", str(png_path)])
    combined = result.output + getattr(result, "stderr", "")
    assert result.exit_code != 0
    assert "1 output asset(s) missing sha256" in combined
    assert "hash mismatch" not in combined


def test_verify_no_manifest(tmp_path: Path) -> None:
    png = tmp_path / "bare.png"
    Image.new("RGBA", (1, 1)).save(png)
    runner = CliRunner()
    result = runner.invoke(cli, ["verify", str(png)])
    assert result.exit_code != 0


def test_replay_dry_run(tmp_path: Path) -> None:
    manifest_path = _create_manifest_json(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["replay", str(manifest_path)])
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "Step 1:" in result.output


def test_replay_warns_on_unverified_assets_without_abort(tmp_path: Path) -> None:
    manifest = _create_url_only_manifest()
    manifest_path = tmp_path / "url-only.json"
    manifest_path.write_text(manifest.to_canonical_json(), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["replay", str(manifest_path)])
    combined = result.output + getattr(result, "stderr", "")
    assert result.exit_code == 0
    assert "output asset bytes are unverified" in combined
    assert "Dry run" in result.output


def test_replay_warns_on_legacy_unverified_assets(tmp_path: Path) -> None:
    manifest = _create_url_only_manifest(schema_version="1.5")
    manifest_path = tmp_path / "legacy-url-only.json"
    manifest_path.write_text(manifest.to_canonical_json(), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["replay", str(manifest_path)])
    combined = result.output + getattr(result, "stderr", "")
    assert result.exit_code == 0
    assert "output asset bytes are unverified" in combined
    assert "Dry run" in result.output


def test_replay_redacts_prompts_by_default(tmp_path: Path) -> None:
    """Dry-run summary must redact prompts unless --show-prompts is passed."""
    manifest_path = _create_manifest_json(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["replay", str(manifest_path)])
    assert result.exit_code == 0
    assert "hello world" not in result.output
    assert "[redacted" in result.output


def test_replay_shows_public_prompts_with_flag(tmp_path: Path) -> None:
    """--show-prompts reveals public-visibility prompts."""
    manifest_path = _create_manifest_json(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["replay", str(manifest_path), "--show-prompts"])
    assert result.exit_code == 0
    assert "hello world" in result.output


def test_replay_redacts_private_prompts_even_with_flag(tmp_path: Path) -> None:
    """--show-prompts must NOT reveal non-public prompts."""
    from genblaze_core.models.enums import PromptVisibility

    step = (
        StepBuilder("test", "test-model")
        .prompt("secret prompt content")
        .modality(Modality.IMAGE)
        .build()
    )
    step.prompt_visibility = PromptVisibility.PRIVATE
    run = RunBuilder("cli-test").add_step(step).build()
    manifest = Manifest.from_run(run)
    manifest_path = tmp_path / "private.json"
    manifest_path.write_text(manifest.to_canonical_json(), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["replay", str(manifest_path), "--show-prompts"])
    assert result.exit_code == 0
    assert "secret prompt content" not in result.output
    assert "[redacted" in result.output


def test_replay_aborts_when_no_allowlist_and_declined(tmp_path: Path) -> None:
    """Non-dry-run replay without --allow-provider must prompt for confirmation
    and abort on 'no'."""
    manifest_path = _create_manifest_json(tmp_path)
    runner = CliRunner()
    # Respond "n" to the per-provider confirmation prompt
    result = runner.invoke(cli, ["replay", str(manifest_path), "--no-dry-run"], input="n\n")
    assert result.exit_code != 0
    assert "Aborted" in result.output or "Execute with provider" in result.output


def test_replay_no_dry_run_exits_nonzero_when_run_fails(tmp_path: Path, monkeypatch) -> None:
    """A replayed failed run must not look successful to automation."""

    class FailingReplayProvider(MockProvider):
        def __init__(self) -> None:
            super().__init__(
                name="test",
                should_fail=True,
                error_code=ProviderErrorCode.AUTH_FAILURE,
                error_message="missing credentials",
            )

    import genblaze_core.providers.registry as provider_registry

    monkeypatch.setattr(
        provider_registry,
        "discover_providers",
        lambda: {"test": FailingReplayProvider},
    )
    manifest_path = _create_manifest_json(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["replay", str(manifest_path), "--no-dry-run", "--allow-provider", "test"],
    )

    assert result.exit_code != 0
    assert "Replay failed" in result.stderr
    assert "Replay failed" not in result.stdout
    assert "Replay complete" not in result.stdout


def test_index(tmp_path: Path) -> None:
    manifest_path = _create_manifest_json(tmp_path)
    out_dir = tmp_path / "index_out"
    runner = CliRunner()
    result = runner.invoke(cli, ["index", str(manifest_path), "-o", str(out_dir)])
    assert result.exit_code == 0
    assert "Indexed" in result.output
    parquet_files = list(out_dir.rglob("*.parquet"))
    assert len(parquet_files) > 0
