"""Tests for FFmpeg utility functions in providers/_ffmpeg_utils.py."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.providers._ffmpeg_utils import (
    get_output_path,
    resolve_ffmpeg,
    resolve_input_path,
    run_ffmpeg,
)


class TestResolveInputPath:
    def test_file_url_under_temp_allowed(self, tmp_path):
        """file:// URLs under temp dirs are accepted."""
        f = tmp_path / "clip.mp4"
        f.write_bytes(b"fake")
        result = resolve_input_path(f"file://{f}")
        assert result == str(f.resolve())

    def test_file_url_outside_allowed_rejected(self):
        with pytest.raises(ProviderError, match="outside allowed directories"):
            resolve_input_path("file:///etc/passwd")

    def test_file_url_with_extra_roots(self, tmp_path):
        custom = tmp_path / "custom"
        custom.mkdir()
        f = custom / "clip.mp4"
        f.write_bytes(b"fake")
        result = resolve_input_path(f"file://{f}", extra_roots=[custom])
        assert result == str(f.resolve())

    @patch("genblaze_core._utils.socket.getaddrinfo")
    def test_https_url_validated(self, mock_dns):
        """HTTPS URLs are validated via validate_asset_url."""
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 0))]
        result = resolve_input_path("https://cdn.example.com/video.mp4")
        assert result == "https://cdn.example.com/video.mp4"

    def test_unsupported_scheme_rejected(self):
        with pytest.raises(ProviderError, match="Unsupported URL scheme"):
            resolve_input_path("ftp://example.com/file.mp4")

    def test_http_scheme_rejected(self):
        with pytest.raises(ProviderError, match="Unsupported URL scheme"):
            resolve_input_path("http://example.com/file.mp4")


class TestResolveFfmpeg:
    def test_raises_when_not_found(self):
        with patch("genblaze_core.providers._ffmpeg_utils.shutil.which", return_value=None):
            with pytest.raises(ProviderError, match="ffmpeg not found"):
                resolve_ffmpeg("ffmpeg")

    def test_returns_path_when_found(self):
        with patch(
            "genblaze_core.providers._ffmpeg_utils.shutil.which",
            return_value="/usr/bin/ffmpeg",
        ):
            assert resolve_ffmpeg("ffmpeg") == "/usr/bin/ffmpeg"


class TestRunFfmpeg:
    def test_timeout_raises_provider_error(self):
        with patch(
            "genblaze_core.providers._ffmpeg_utils.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10),
        ):
            with pytest.raises(ProviderError, match="timed out"):
                run_ffmpeg(["ffmpeg", "-version"], timeout=10)

    def test_nonzero_exit_raises_provider_error(self):
        fake_result = subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=1, stdout=b"", stderr=b"Error: invalid input"
        )
        with patch(
            "genblaze_core.providers._ffmpeg_utils.subprocess.run",
            return_value=fake_result,
        ):
            with pytest.raises(ProviderError, match="exited with code 1"):
                run_ffmpeg(["ffmpeg", "-i", "bad.mp4"])

    def test_os_error_raises_provider_error(self):
        with patch(
            "genblaze_core.providers._ffmpeg_utils.subprocess.run",
            side_effect=OSError("No such file or directory"),
        ):
            with pytest.raises(ProviderError, match="Failed to run ffmpeg"):
                run_ffmpeg(["ffmpeg", "-version"])


class TestGetOutputPath:
    def test_returns_temp_file_without_output_dir(self):
        path = get_output_path("step-123", "mp4", output_dir=None)
        assert path.suffix == ".mp4"
        assert path.exists()
        path.unlink()

    def test_creates_dir_and_returns_path(self, tmp_path):
        out = tmp_path / "new_dir"
        path = get_output_path("step-123", "mp4", output_dir=out)
        assert path == out / "step-123.mp4"
        assert out.is_dir()
