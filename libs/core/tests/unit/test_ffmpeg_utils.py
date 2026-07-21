"""Tests for FFmpeg utility functions in providers/_ffmpeg_utils.py."""

from __future__ import annotations

import logging
import subprocess
from pathlib import PureWindowsPath
from unittest.mock import patch

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.providers._ffmpeg_utils import (
    _redact_cmd_for_log,
    _redact_url_query,
    _redact_urls_in_text,
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

    @patch("genblaze_core._utils.socket.getaddrinfo")
    def test_https_url_rejects_private_ip(self, mock_dns):
        """HTTPS URLs resolving to private IPs must be rejected (SSRF guard)."""
        # Cloud IMDS endpoint — never legitimate as an ffmpeg input
        mock_dns.return_value = [(2, 1, 6, "", ("169.254.169.254", 0))]
        with pytest.raises(ProviderError, match="Private/loopback"):
            resolve_input_path("https://metadata.example.com/token")

    @patch("genblaze_core._utils.socket.getaddrinfo")
    def test_https_url_rejects_loopback(self, mock_dns):
        """Hostnames resolving to 127.0.0.0/8 must be rejected."""
        mock_dns.return_value = [(2, 1, 6, "", ("127.0.0.1", 0))]
        with pytest.raises(ProviderError, match="Private/loopback"):
            resolve_input_path("https://evil.example.com/payload.mp4")

    def test_unsupported_scheme_rejected(self):
        with pytest.raises(ProviderError, match="Unsupported URL scheme"):
            resolve_input_path("ftp://example.com/file.mp4")

    def test_http_scheme_rejected(self):
        with pytest.raises(ProviderError, match="Unsupported URL scheme"):
            resolve_input_path("http://example.com/file.mp4")

    def test_windows_drive_letter_file_url(self, tmp_path, monkeypatch):
        """Regression for #132/#164: url2pathname() strips the leading slash
        before a Windows drive letter so Path.resolve() produces an absolute
        path that passes the allowlist check.

        The input URL is built with PureWindowsPath.as_uri() — the exact
        form local_file_url() (the shared connector helper) produces for a
        Windows path — so ffmpeg-chained providers accept what connectors
        now emit. url2pathname is monkeypatched to return the pre-computed
        real_path since a genuine Windows path string can't round-trip
        through POSIX pathlib on this host; nturl2path.url2pathname is
        exercised unmocked in test_utils.py::TestLocalFileUrl.
        """
        f = tmp_path / "clip.mp4"
        f.write_bytes(b"fake")
        real_path = str(f.resolve())
        win_url = PureWindowsPath(r"C:\tmp\clip.mp4").as_uri()
        assert win_url == "file:///C:/tmp/clip.mp4"
        monkeypatch.setattr(
            "genblaze_core.providers._ffmpeg_utils.url2pathname",
            lambda _: real_path,
        )
        monkeypatch.setattr(
            "genblaze_core.providers._ffmpeg_utils._ALLOWED_FILE_ROOTS",
            (tmp_path.resolve(),),
        )
        result = resolve_input_path(win_url)
        assert result == real_path


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

    def test_nonzero_exit_redacts_presigned_url_from_stderr(self):
        """Regression for #75 (secondary leak path found in review): ffmpeg's
        own stderr can echo a presigned input URL verbatim on a fetch
        failure (e.g. an expired-signature 403), and that stderr becomes the
        ProviderError message. The signature must not survive into it."""
        stderr = (
            b"[https @ 0x0] HTTP error 403 Forbidden\n"
            b"https://s3.us-west-004.backblazeb2.com/bucket/obj?"
            b"X-Amz-Signature=deadbeefcafef00d: Server returned 403 Forbidden"
        )
        fake_result = subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=1, stdout=b"", stderr=stderr
        )
        with patch(
            "genblaze_core.providers._ffmpeg_utils.subprocess.run",
            return_value=fake_result,
        ):
            with pytest.raises(ProviderError) as exc_info:
                run_ffmpeg(
                    ["ffmpeg", "-i", "https://s3.example.com/obj?X-Amz-Signature=deadbeefcafef00d"]
                )
        assert "X-Amz-Signature=deadbeefcafef00d" not in str(exc_info.value)
        assert "REDACTED" in str(exc_info.value)


class TestRedactCmdForLog:
    """Regression for #75: presigned URL signatures must not reach logs."""

    def test_redacts_query_string_from_https_arg(self):
        url = "https://s3.us-west-004.backblazeb2.com/bucket/obj?X-Amz-Signature=deadbeefcafef00d"
        redacted = _redact_url_query(url)
        assert "X-Amz-Signature=deadbeefcafef00d" not in redacted
        assert redacted == ("https://s3.us-west-004.backblazeb2.com/bucket/obj?REDACTED")

    def test_leaves_non_url_args_unchanged(self):
        for arg in ("-i", "-vf", "scale=1280:720", "/media/out.mp4", "-y"):
            assert _redact_url_query(arg) == arg

    def test_leaves_url_without_query_unchanged(self):
        assert _redact_url_query("https://cdn.example.com/video.mp4") == (
            "https://cdn.example.com/video.mp4"
        )

    def test_redact_cmd_for_log_only_touches_url_args(self):
        cmd = [
            "ffmpeg",
            "-i",
            "https://s3.example.com/bucket/obj?X-Amz-Signature=deadbeef",
            "-c",
            "copy",
            "-y",
            "/media/out.mp4",
        ]
        rendered = _redact_cmd_for_log(cmd)
        assert "X-Amz-Signature=deadbeef" not in rendered
        assert (
            "ffmpeg -i https://s3.example.com/bucket/obj?REDACTED -c copy -y /media/out.mp4"
            == rendered
        )

    def test_redact_urls_in_text_redacts_embedded_url(self):
        """`_redact_urls_in_text` (unlike `_redact_url_query`) scans free-form
        text for an embedded URL rather than requiring the whole string to
        be one — needed for ffmpeg stderr, which surrounds the URL with
        other diagnostic text."""
        text = (
            "[https @ 0x0] HTTP error 403 Forbidden\n"
            "https://s3.example.com/bucket/obj?X-Amz-Signature=deadbeef: "
            "Server returned 403 Forbidden"
        )
        redacted = _redact_urls_in_text(text)
        assert "X-Amz-Signature=deadbeef" not in redacted
        assert "REDACTED" in redacted
        assert "HTTP error 403 Forbidden" in redacted  # surrounding text preserved

    def test_redact_urls_in_text_leaves_plain_text_unchanged(self):
        text = "Error: invalid input, no such filter 'scale'"
        assert _redact_urls_in_text(text) == text

    def test_run_ffmpeg_redacts_presigned_url_from_debug_log(self, caplog):
        cmd = [
            "ffmpeg",
            "-i",
            "https://s3.us-west-004.backblazeb2.com/bucket/obj?X-Amz-Signature=deadbeefcafef00d",
            "-c",
            "copy",
            "-y",
            "/media/out.mp4",
        ]
        fake_result = subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")
        with patch(
            "genblaze_core.providers._ffmpeg_utils.subprocess.run",
            return_value=fake_result,
        ):
            with caplog.at_level(logging.DEBUG, logger="genblaze.ffmpeg"):
                run_ffmpeg(cmd)
        assert "X-Amz-Signature=deadbeefcafef00d" not in caplog.text
        assert "REDACTED" in caplog.text
        # Execution itself must still use the untouched cmd (query intact).
        assert cmd[2].endswith("?X-Amz-Signature=deadbeefcafef00d")


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
