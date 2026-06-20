"""Verify command — check manifest hash and output sha256 coverage."""

import json
from pathlib import Path

import click
from genblaze_core._utils import MAX_MANIFEST_BYTES
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media import get_handler, guess_mime
from genblaze_core.media.sidecar import PointerSidecarError, SidecarHandler
from genblaze_core.models.manifest import Manifest, parse_manifest


def _load_standalone_json_manifest(file: Path) -> Manifest:
    size = file.stat().st_size
    if size > MAX_MANIFEST_BYTES:
        raise EmbeddingError(
            f"Manifest JSON exceeds size limit: {size} > {MAX_MANIFEST_BYTES} bytes"
        )
    data = json.loads(file.read_text(encoding="utf-8"))
    if "run" not in data and "manifest_uri" in data:
        raise PointerSidecarError(
            manifest_uri=data["manifest_uri"],
            canonical_hash=data.get("canonical_hash", ""),
        )
    return parse_manifest(data)


def _extract_manifest(file: Path) -> Manifest:
    """Extract manifest, trying format-specific handler then sidecar."""
    if file.suffix.lower() == ".json":
        return _load_standalone_json_manifest(file)

    mime = guess_mime(file)
    handler = get_handler(mime)
    if handler is not None:
        try:
            return handler.extract(file)
        except EmbeddingError:
            pass  # Expected: no manifest in this format, try sidecar
    # Try sidecar fallback
    sidecar = SidecarHandler()
    return sidecar.extract(file)


@click.command()
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def verify(file: Path) -> None:
    """Verify an embedded, sidecar, or standalone genblaze manifest."""
    try:
        manifest = _extract_manifest(file)
        report = manifest.verification_report()
        if not report.hash_ok:
            click.echo("FAIL — manifest hash mismatch.", err=True)
            raise click.exceptions.Exit(1)
        if report.missing_sha256_ids:
            click.echo(
                f"FAIL — {len(report.missing_sha256_ids)} output asset(s) "
                "missing or malformed sha256.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        click.echo("OK — manifest hash verified; all output assets declare sha256.")
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        # Prefix with exception type so "PermissionError: ..." and
        # "EmbeddingError: ..." are distinguishable at a glance.
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
