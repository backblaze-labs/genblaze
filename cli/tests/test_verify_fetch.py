from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from genblaze_cli.main import cli
from genblaze_core.builders import RunBuilder, StepBuilder
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import StepStatus
from genblaze_core.models.manifest import Manifest


def _create_fetch_manifest(tmp_path: Path, data: bytes = b"genblaze bytes") -> tuple[Path, Path]:
    """Manifest JSON + on-disk file:// asset whose sha256/size are byte-bound."""
    asset_path = tmp_path / "asset.bin"
    asset_path.write_bytes(data)
    step = StepBuilder("test", "test-model").prompt("hello").build()
    step.status = StepStatus.SUCCEEDED
    asset = Asset(url=asset_path.as_uri(), media_type="image/png")
    asset.set_hash(data)
    step.assets = [asset]
    run = RunBuilder("fetch-test").add_step(step).build()
    manifest = Manifest.from_run(run)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.to_canonical_json(), encoding="utf-8")
    return manifest_path, asset_path


def test_verify_fetch_matching_bytes(tmp_path: Path) -> None:
    manifest_path, _ = _create_fetch_manifest(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--fetch", str(manifest_path)])

    assert result.exit_code == 0, result.output
    assert "1 output asset(s) fetched and matched their declared sha256" in result.output


def test_verify_fetch_detects_tampered_bytes(tmp_path: Path) -> None:
    manifest_path, asset_path = _create_fetch_manifest(tmp_path)
    asset_path.write_bytes(b"tampered after the manifest was written")

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--fetch", str(manifest_path)])
    combined = result.output + getattr(result, "stderr", "")

    assert result.exit_code != 0
    assert "sha256 mismatch" in combined


def test_verify_fetch_detects_size_mismatch(tmp_path: Path) -> None:
    """Correct digest but wrong declared size_bytes must fail --fetch."""
    data = b"genblaze bytes"
    asset_path = tmp_path / "asset.bin"
    asset_path.write_bytes(data)
    step = StepBuilder("test", "test-model").prompt("hello").build()
    step.status = StepStatus.SUCCEEDED
    asset = Asset(url=asset_path.as_uri(), media_type="image/png")
    asset.set_hash(data)
    asset.size_bytes = len(data) + 1
    step.assets = [asset]
    run = RunBuilder("fetch-size").add_step(step).build()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(Manifest.from_run(run).to_canonical_json(), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--fetch", str(manifest_path)])
    combined = result.output + getattr(result, "stderr", "")

    assert result.exit_code != 0
    assert "size mismatch" in combined


def test_verify_fetch_reports_every_failing_asset(tmp_path: Path) -> None:
    """One bad asset must not stop verification of the rest."""
    tampered = tmp_path / "tampered.bin"
    tampered.write_bytes(b"original")
    missing = tmp_path / "missing.bin"
    missing.write_bytes(b"will be deleted")
    step = StepBuilder("test", "test-model").prompt("hello").build()
    step.status = StepStatus.SUCCEEDED
    assets = []
    for path in (tampered, missing):
        asset = Asset(url=path.as_uri(), media_type="image/png")
        asset.set_hash(path.read_bytes())
        assets.append(asset)
    step.assets = assets
    run = RunBuilder("fetch-multi").add_step(step).build()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(Manifest.from_run(run).to_canonical_json(), encoding="utf-8")
    tampered.write_bytes(b"changed")
    missing.unlink()

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--fetch", str(manifest_path)])
    combined = result.output + getattr(result, "stderr", "")

    assert result.exit_code != 0
    assert "sha256 mismatch" in combined
    assert "fetch failed" in combined


def test_verify_fetch_rejects_hash_only_combo(tmp_path: Path) -> None:
    manifest_path, _ = _create_fetch_manifest(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--fetch", "--hash-only", str(manifest_path)])
    combined = result.output + getattr(result, "stderr", "")

    assert result.exit_code != 0
    assert "mutually exclusive" in combined


def test_verify_fetch_https_streams_through_pinned_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """https assets must go through transfer's SSRF-pinned stream, chunked, and
    release the connection afterwards."""
    data = b"remote asset bytes" * 1024
    step = StepBuilder("test", "test-model").prompt("hello").build()
    step.status = StepStatus.SUCCEEDED
    asset = Asset(url="https://cdn.example.com/output.png", media_type="image/png")
    asset.set_hash(data)
    step.assets = [asset]
    run = RunBuilder("fetch-https").add_step(step).build()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(Manifest.from_run(run).to_canonical_json(), encoding="utf-8")

    released = {"called": False}

    class _FakeResp:
        def __init__(self) -> None:
            self._buf = data

        def read(self, size: int) -> bytes:
            chunk, self._buf = self._buf[:size], self._buf[size:]
            return chunk

        def release_conn(self) -> None:
            released["called"] = True

    monkeypatch.setattr(
        "genblaze_core.storage.transfer._http_get_stream",
        lambda url, *, timeout: _FakeResp(),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--fetch", str(manifest_path)])

    assert result.exit_code == 0, result.output
    assert "fetched and matched" in result.output
    assert released["called"]


def test_verify_fetch_redacts_presigned_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A presigned query string is a bearer credential; it must not survive
    into CLI output even when the transfer layer's error message echoes it."""
    from genblaze_core.exceptions import StorageError

    url = "https://cdn.example.com/output.png?X-Amz-Signature=SECRETSIG"
    step = StepBuilder("test", "test-model").prompt("hello").build()
    step.status = StepStatus.SUCCEEDED
    asset = Asset(url=url, media_type="image/png")
    asset.set_hash(b"whatever")
    step.assets = [asset]
    run = RunBuilder("fetch-redact").add_step(step).build()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(Manifest.from_run(run).to_canonical_json(), encoding="utf-8")

    def _raise(url: str, *, timeout: float) -> None:
        raise StorageError(f"HTTP 403 downloading {url}")

    monkeypatch.setattr("genblaze_core.storage.transfer._http_get_stream", _raise)

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--fetch", str(manifest_path)])
    combined = result.output + getattr(result, "stderr", "")

    assert result.exit_code != 0
    assert "SECRETSIG" not in combined
    assert "REDACTED" in combined


def test_verify_fetch_rejects_file_url_outside_allowed_roots(tmp_path: Path) -> None:
    step = StepBuilder("test", "test-model").prompt("hello").build()
    step.status = StepStatus.SUCCEEDED
    asset = Asset(url="file:///etc/hosts", media_type="image/png")
    asset.sha256 = "f" * 64
    step.assets = [asset]
    run = RunBuilder("fetch-roots").add_step(step).build()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(Manifest.from_run(run).to_canonical_json(), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--fetch", str(manifest_path)])
    combined = result.output + getattr(result, "stderr", "")

    assert result.exit_code != 0
    assert "outside allowed roots" in combined


def test_verify_default_message_points_to_fetch(tmp_path: Path) -> None:
    """The default OK verdict must route users to --fetch instead of
    dead-ending at 'bytes were not compared'."""
    manifest_path, _ = _create_fetch_manifest(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", str(manifest_path)])

    assert result.exit_code == 0
    assert "add --fetch" in result.output


def test_verify_fetch_blocks_ssrf_urls_via_real_transfer_guard(tmp_path: Path) -> None:
    """No mocking: --fetch must route through core's real SSRF guard.

    localhost and the cloud-metadata IP are rejected by resolve_ssrf before any
    connection is opened, so this exercises the genuine security path offline.
    If anyone ever swaps the hardened fetch for a naive HTTP client, this test
    starts making real network calls and fails.
    """
    step = StepBuilder("test", "test-model").prompt("hello").build()
    step.status = StepStatus.SUCCEEDED
    assets = []
    for url in ("https://localhost/asset.png", "https://169.254.169.254/latest/meta-data"):
        asset = Asset(url=url, media_type="image/png")
        asset.sha256 = "f" * 64
        assets.append(asset)
    step.assets = assets
    run = RunBuilder("fetch-ssrf").add_step(step).build()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(Manifest.from_run(run).to_canonical_json(), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--fetch", str(manifest_path)])
    # result.output is the mixed stream on every click version (stderr raises
    # on click<8.2; on >=8.2 output+stderr would double-count these lines).
    combined = result.output

    assert result.exit_code != 0
    # Both assets must surface as fetch failures in a single pass.
    assert combined.count("fetch failed") == 2
    # And the failures must come from the SSRF guard, not a connection attempt.
    assert "not allowed" in combined


def test_verify_fetch_allowed_root_admits_file_outside_default_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--allowed-root extends the file:// allowlist, mirroring the transfer
    layer's extra_roots semantics for pipelines run with output_dir=."""
    manifest_path, _ = _create_fetch_manifest(tmp_path)
    # Simulate a root outside the temp-dir allowlist: with no default roots,
    # tmp_path is only reachable via --allowed-root.
    monkeypatch.setattr("genblaze_core._utils.ALLOWED_FILE_ROOTS", ())

    runner = CliRunner()
    result = runner.invoke(
        cli, ["verify", "--fetch", "--allowed-root", str(tmp_path), str(manifest_path)]
    )

    assert result.exit_code == 0, result.output
    assert "fetched and matched" in result.output


def test_verify_fetch_allowed_root_requires_fetch(tmp_path: Path) -> None:
    manifest_path, _ = _create_fetch_manifest(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--allowed-root", str(tmp_path), str(manifest_path)])
    combined = result.output + getattr(result, "stderr", "")

    assert result.exit_code != 0
    assert "requires --fetch" in combined


def test_verify_fetch_no_output_assets_reports_nothing_to_fetch(tmp_path: Path) -> None:
    """Zero output assets must not print a vacuous 'fetched and matched' OK."""
    step = StepBuilder("test", "test-model").prompt("hello").build()
    step.status = StepStatus.SUCCEEDED
    step.assets = []
    run = RunBuilder("fetch-empty").add_step(step).build()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(Manifest.from_run(run).to_canonical_json(), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--fetch", str(manifest_path)])

    assert result.exit_code == 0, result.output
    assert "no output assets to fetch" in result.output


def test_verify_fetch_rejects_http_scheme(tmp_path: Path) -> None:
    """Plain http:// is rejected; https-only, same policy as the transfer layer."""
    step = StepBuilder("test", "test-model").prompt("hello").build()
    step.status = StepStatus.SUCCEEDED
    asset = Asset(url="http://cdn.example.com/output.png", media_type="image/png")
    asset.sha256 = "f" * 64
    step.assets = [asset]
    run = RunBuilder("fetch-http").add_step(step).build()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(Manifest.from_run(run).to_canonical_json(), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--fetch", str(manifest_path)])
    combined = result.output + getattr(result, "stderr", "")

    assert result.exit_code != 0
    assert "unsupported URL scheme" in combined
