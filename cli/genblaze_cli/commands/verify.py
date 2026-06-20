"""Verify command — check manifest hash and asset-byte integrity."""

from pathlib import Path

import click
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media import get_handler, guess_mime
from genblaze_core.media.sidecar import SidecarHandler
from genblaze_core.models.manifest import Manifest


def _extract_manifest(file: Path) -> Manifest:
    """Extract manifest, trying format-specific handler then sidecar."""
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
    """Verify the integrity of a genblaze manifest in a media file."""
    try:
        manifest = _extract_manifest(file)
        if not manifest.verify_hash():
            click.echo("FAIL — manifest hash mismatch.", err=True)
            raise click.exceptions.Exit(1)
        missing_sha_ids = manifest.output_asset_ids_missing_sha256()
        if missing_sha_ids:
            click.echo(
                f"FAIL — {len(missing_sha_ids)} output asset(s) missing sha256.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        click.echo("OK — manifest hash and output asset integrity verified.")
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        # Prefix with exception type so "PermissionError: ..." and
        # "EmbeddingError: ..." are distinguishable at a glance.
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
