"""Verify command — check manifest hash and output sha256 coverage."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit
from urllib.request import url2pathname

import click

from genblaze_cli.manifest_io import extract_manifest

if TYPE_CHECKING:
    from genblaze_core.models.manifest import Manifest

# 256 KB chunks; matches the transfer layer's streaming granularity.
_FETCH_CHUNK = 256 * 1024

# A presigned URL's query string, or basic-auth userinfo (user:pass@host), is
# a bearer credential for that object (#75, #201); it must never be echoed in
# CLI output or error messages. Matches any http(s) URL — not just ones with a
# query string — so a userinfo-only URL embedded in a transfer-layer exception
# message is also caught; _redact_url no-ops on a URL with neither.
_URL_RE = re.compile(r"https?://\S+")


def _redact_url(url: str) -> str:
    parsed = urlsplit(url)
    # Strip userinfo by editing the netloc string directly rather than
    # rebuilding it from parsed.hostname/.port — hostname strips the []
    # brackets off an IPv6 literal, which would otherwise corrupt the URL.
    netloc = parsed.netloc.rsplit("@", 1)[-1] if "@" in parsed.netloc else parsed.netloc
    if parsed.query or netloc != parsed.netloc:
        query = "REDACTED" if parsed.query else parsed.query
        return urlunsplit(parsed._replace(netloc=netloc, query=query))
    return url


def _redact_text(text: str) -> str:
    return _URL_RE.sub(lambda m: _redact_url(m.group(0)), text)


def _hash_url_bytes(url: str, extra_roots: tuple[Path, ...] = ()) -> tuple[str, int]:
    """Stream the bytes at ``url`` and return ``(sha256_hex, size)``.

    ``https://`` URLs go through the transfer layer's SSRF-pinned stream;
    every redirect hop is re-validated against the SSRF blocklist and DNS
    pinned, because a manifest is untrusted input and ``asset.url`` can point
    anywhere. ``file://`` paths must resolve under the same allowed roots the
    storage and ffmpeg paths accept, plus any ``extra_roots`` the caller
    explicitly opted into (mirroring ``_read_local_file``'s ``extra_roots``
    semantics; pipelines run with ``output_dir=`` write assets outside the
    temp-dir allowlist). Bytes are hashed as they arrive; a whole asset is
    never held in memory.
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme
    if scheme == "https":
        # TODO(#202): these are genblaze_core underscore-private symbols;
        # the private-import cleanup (promoting a public streaming helper)
        # is a deferred follow-up, not part of this fix.
        from genblaze_core.storage.transfer import (
            _DEFAULT_DOWNLOAD_TIMEOUT,
            _DEFAULT_MAX_DOWNLOAD_BYTES,
            _HashingStreamReader,
            _http_get_stream,
        )

        resp = _http_get_stream(url, timeout=_DEFAULT_DOWNLOAD_TIMEOUT)
        # Reuse the transfer layer's own chunk-read/incremental-sha256/cap
        # loop instead of duplicating it (#202) — same exception type and
        # message shape by construction, so callers see one error taxonomy.
        reader = _HashingStreamReader(resp, max_bytes=_DEFAULT_MAX_DOWNLOAD_BYTES)
        try:
            while reader.read(_FETCH_CHUNK):
                pass
        finally:
            resp.release_conn()
        return reader.sha256_hex, reader.size
    if scheme == "file":
        from genblaze_core._utils import ALLOWED_FILE_ROOTS

        # RFC 8089: empty or 'localhost' netloc only; anything else is a
        # remote-authority reference and must not be silently resolved as a
        # local path (#201) — matches core's validate_chain_input_url.
        if parsed.netloc and parsed.netloc != "localhost":
            raise ValueError(
                f"file:// URL must have empty or 'localhost' netloc; got {parsed.netloc!r}"
            )
        allowed = (*ALLOWED_FILE_ROOTS, *(r.resolve() for r in extra_roots))
        resolved = Path(url2pathname(parsed.path)).resolve()
        if not any(resolved.is_relative_to(root) for root in allowed):
            raise ValueError(f"file:// path outside allowed roots: {resolved}")
        digest = hashlib.sha256()
        size = 0
        with resolved.open("rb") as fh:
            while chunk := fh.read(_FETCH_CHUNK):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size
    raise ValueError(f"unsupported URL scheme {scheme!r} (https:// or file:// only)")


def _fetch_and_compare(
    manifest: Manifest, extra_roots: tuple[Path, ...] = ()
) -> tuple[int, list[str]]:
    """Fetch every output asset and compare bytes against the manifest.

    Returns ``(asset_count, failures)``. An asset that cannot be fetched is a
    failure, not a skip. ``--fetch`` promises byte verification, so an
    unverifiable asset must not contribute to an OK verdict. One asset
    failing does not stop the rest: a corrupt asset and an unreachable one
    should both surface in a single pass.
    """
    count = 0
    failures: list[str] = []
    for step in manifest.run.steps:
        for asset in step.assets:
            count += 1
            label = f"asset {asset.asset_id[:8]} ({_redact_url(asset.url)})"
            try:
                digest, size = _hash_url_bytes(asset.url, extra_roots)
            except Exception as exc:
                failures.append(f"{label}: fetch failed: {_redact_text(str(exc))}")
                continue
            if digest != asset.sha256:
                failures.append(
                    f"{label}: sha256 mismatch: manifest declares {asset.sha256}, "
                    f"fetched bytes hash to {digest}"
                )
            elif asset.size_bytes is not None and size != asset.size_bytes:
                failures.append(
                    f"{label}: size mismatch: manifest declares "
                    f"{asset.size_bytes} bytes, fetched {size}"
                )
    return count, failures


@click.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--hash-only",
    is_flag=True,
    help=(
        "Only verify canonical_hash; skip output sha256 and asset-metadata checks. "
        "Mutually exclusive with --fetch."
    ),
)
@click.option(
    "--fetch",
    is_flag=True,
    help=(
        "Also download each output asset and compare its bytes to the declared "
        "sha256. Mutually exclusive with --hash-only."
    ),
)
@click.option(
    "--allowed-root",
    "allowed_roots",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help=(
        "With --fetch: additionally trust file:// assets under this directory "
        "(repeatable). By default only the system temp directories are allowed; "
        "pipelines run with output_dir= write assets elsewhere."
    ),
)
def verify(file: Path, hash_only: bool, fetch: bool, allowed_roots: tuple[Path, ...]) -> None:
    """Verify an embedded, sidecar, or standalone genblaze manifest."""
    if fetch and hash_only:
        raise click.UsageError("--fetch and --hash-only are mutually exclusive.")
    if allowed_roots and not fetch:
        raise click.UsageError("--allowed-root requires --fetch.")
    try:
        manifest = extract_manifest(file)
        report = manifest.verification_report()
        if not report.hash_ok:
            click.echo("FAIL: manifest hash mismatch.", err=True)
            raise click.exceptions.Exit(1)
        if hash_only:
            click.echo("OK: manifest hash verified. Asset bytes were not fetched or compared.")
            return
        if report.unverified_sha256_ids:
            click.echo(
                f"FAIL: {len(report.unverified_sha256_ids)} output asset(s) "
                "missing or malformed sha256.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        # Out-of-spec numeric/media_type metadata (e.g. width=0) is tolerated on
        # load by parse_manifest() but fails verify() (#149); surface it here so
        # the CLI verdict matches Manifest.verify()/report.ok instead of a stale
        # "OK" that only checked sha256.
        if report.invalid_metadata_ids:
            click.echo(
                f"FAIL: {len(report.invalid_metadata_ids)} output asset(s) "
                "carry out-of-spec numeric/media_type metadata "
                "(e.g. width=0, or a malformed media_type).",
                err=True,
            )
            raise click.exceptions.Exit(1)
        if fetch:
            count, failures = _fetch_and_compare(manifest, allowed_roots)
            if failures:
                for line in failures:
                    click.echo(f"FAIL: {line}", err=True)
                raise click.exceptions.Exit(1)
            if count == 0:
                # Distinguish "nothing to check" from "checked and matched":
                # a vacuous OK must not read like a byte-verification pass.
                click.echo("OK: manifest hash verified; no output assets to fetch.")
                return
            click.echo(
                f"OK: manifest hash verified; {count} output asset(s) "
                "fetched and matched their declared sha256."
            )
            return
        click.echo(
            "OK: manifest hash verified; all output assets declare sha256 and "
            "carry in-spec metadata. Asset bytes were not fetched or compared; "
            "add --fetch to verify the media itself."
        )
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        # Prefix with exception type so "PermissionError: ..." and
        # "EmbeddingError: ..." are distinguishable at a glance.
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
