"""Asset URL canonicalization and credential stripping helpers."""

from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit, urlunsplit

_CREDENTIAL_QUERY_EXCLUDE = frozenset(
    {
        "access_token",
        "authorization",  # B2 download-auth / CDN edge-auth bearer tokens
        "awsaccesskeyid",
        "expires",
        "expires_in",
        "expiry",
        "key-pair-id",
        "response-cache-control",
        "response-content-disposition",
        "response-content-encoding",
        "response-content-language",
        "response-content-type",
        "x-id",
    }
)
# AWS SigV2 presigned URLs carry AWSAccessKeyId + Expires + Signature. The first
# two are already in the unconditional set; `signature` is gated here so a bare
# resource param literally named "signature" is not stripped (see GCS/CloudFront).
_AWS_SIGV2_SIGNED_QUERY_PARAMS = frozenset({"signature"})
_AZURE_SAS_QUERY_PARAMS = frozenset(
    {
        "se",
        "sig",
        "skoid",
        "sks",
        "sktid",
        "skv",
        "sp",
        "spr",
        "sr",
        "srt",
        "ss",
        "st",
        "sv",
    }
)
_CLOUDFRONT_SIGNED_QUERY_PARAMS = frozenset(
    {
        "expires",
        "key-pair-id",
        "policy",
        "signature",
    }
)
_GCS_V2_SIGNED_QUERY_PARAMS = frozenset(
    {
        "expires",
        "googleaccessid",
        "signature",
    }
)
_CREDENTIAL_QUERY_PREFIX_EXCLUDE = ("x-amz-", "x-goog-", "x-bz-")


def _query_pairs_preserving_plus(query: str) -> list[tuple[str, str | None]]:
    """Parse query pairs without treating '+' as a space."""
    if not query:
        return []
    pairs: list[tuple[str, str | None]] = []
    for raw_pair in query.split("&"):
        if not raw_pair:
            continue
        raw_name, separator, raw_value = raw_pair.partition("=")
        pairs.append((unquote(raw_name), unquote(raw_value) if separator else None))
    return pairs


def _encode_query_pairs(pairs: list[tuple[str, str | None]]) -> str:
    encoded: list[str] = []
    for name, value in pairs:
        encoded_name = quote(name, safe="")
        if value is None:
            encoded.append(encoded_name)
        else:
            encoded.append(f"{encoded_name}={quote(value, safe='')}")
    return "&".join(encoded)


def _is_credential_query_param(
    name: str,
    *,
    is_azure_sas: bool,
    is_cloudfront_signed: bool,
    is_gcs_v2_signed: bool,
    is_aws_sigv2: bool,
) -> bool:
    key = name.lower()
    return (
        key in _CREDENTIAL_QUERY_EXCLUDE
        or key.startswith(_CREDENTIAL_QUERY_PREFIX_EXCLUDE)
        or (is_azure_sas and key in _AZURE_SAS_QUERY_PARAMS)
        or (is_cloudfront_signed and key in _CLOUDFRONT_SIGNED_QUERY_PARAMS)
        or (is_gcs_v2_signed and key in _GCS_V2_SIGNED_QUERY_PARAMS)
        or (is_aws_sigv2 and key in _AWS_SIGV2_SIGNED_QUERY_PARAMS)
    )


def strip_asset_url_credentials(url: str) -> str:
    """Strip known signed-URL credentials before storing or hashing asset URLs."""
    parts = urlsplit(url)
    query_pairs = _query_pairs_preserving_plus(parts.query)
    query_keys = {name.lower() for name, _value in query_pairs}

    is_azure_sas = bool(query_keys & {"sv", "se", "sp", "sr", "ss", "srt"})
    is_cloudfront_signed = "key-pair-id" in query_keys
    is_gcs_v2_signed = "googleaccessid" in query_keys
    is_aws_sigv2 = "awsaccesskeyid" in query_keys
    query_items = [
        (name, value)
        for name, value in query_pairs
        if not _is_credential_query_param(
            name,
            is_azure_sas=is_azure_sas,
            is_cloudfront_signed=is_cloudfront_signed,
            is_gcs_v2_signed=is_gcs_v2_signed,
            is_aws_sigv2=is_aws_sigv2,
        )
    ]
    query = _encode_query_pairs(
        sorted(query_items, key=lambda item: (item[0], item[1] is not None, item[1] or ""))
    )

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    return urlunsplit((scheme, netloc, parts.path, query, ""))
