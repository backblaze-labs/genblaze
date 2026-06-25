"""Unit tests for strip_asset_url_credentials (signed-URL credential stripping)."""

from __future__ import annotations

import pytest
from genblaze_core._asset_url import strip_asset_url_credentials


def test_no_query_returns_unchanged_no_trailing_question_mark():
    url = "https://cdn.example.com/path/to/output.png"
    assert strip_asset_url_credentials(url) == url


def test_authorization_param_stripped():
    out = strip_asset_url_credentials(
        "https://f000.backblazeb2.com/file/bucket/a.mp4?Authorization=Bearer+secret"
    )
    assert "authorization" not in out.lower()
    assert "secret" not in out


def test_aws_sigv2_signature_stripped():
    # AWS SigV2 presigned URL: AWSAccessKeyId marks it, so Signature is a credential.
    out = strip_asset_url_credentials(
        "https://s3.amazonaws.com/bucket/a.png"
        "?AWSAccessKeyId=AKIA&Expires=1700000000&Signature=abc123"
    )
    assert "awsaccesskeyid" not in out.lower()
    assert "signature" not in out.lower()
    assert "expires" not in out.lower()


def test_bare_signature_without_sigv2_marker_kept():
    # A resource param literally named "signature" (no AWSAccessKeyId / key-pair-id
    # / GoogleAccessId gate) is a content descriptor, not a credential — keep it.
    url = "https://cdn.example.com/download?signature=doc-a"
    assert "signature=doc-a" in strip_asset_url_credentials(url)


def test_azure_sas_credentials_stripped_resource_params_kept():
    out = strip_asset_url_credentials(
        "https://acct.blob.core.windows.net/c/img.png"
        "?st=2026-01-01T00%3A00%3A00Z&se=2026-01-01T01%3A00%3A00Z"
        "&sp=r&sv=2024-11-04&sig=secret&tenant_resource=keep"
    )
    for cred in ("sig=", "se=", "st=", "sp=", "sv="):
        assert cred not in out
    assert "tenant_resource=keep" in out


def test_userinfo_credentials_stripped():
    out = strip_asset_url_credentials("https://user:pass@cdn.example.com/a.png")
    assert "user" not in out
    assert "pass" not in out


def test_idempotent():
    url = (
        "https://acct.blob.core.windows.net/c/img.png"
        "?se=2026-01-01T01%3A00%3A00Z&sig=secret&keep=1"
    )
    once = strip_asset_url_credentials(url)
    assert strip_asset_url_credentials(once) == once


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "https://",
        "://missing-scheme",
        "ftp://host/path?sig=x",
    ],
)
def test_malformed_urls_do_not_raise(url):
    # Best-effort: must return a string, never raise, even on junk input.
    assert isinstance(strip_asset_url_credentials(url), str)
