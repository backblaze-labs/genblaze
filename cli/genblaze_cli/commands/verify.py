"""Verify command — check manifest hash integrity."""

from pathlib import Path

import click
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media import get_handler, guess_mime
from genblaze_core.media.sidecar import SidecarHandler


def _verify_manifest(file: Path) -> bool:
    """Extract and verify manifest, trying format-specific handler then sidecar."""
    mime = guess_mime(file)
    handler = get_handler(mime)
    if handler is not None:
        try:
            return handler.verify(file)
        except EmbeddingError:
            pass  # Expected: no manifest in this format, try sidecar
    # Try sidecar fallback
    sidecar = SidecarHandler()
    return sidecar.verify(file)


@click.command()
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def verify(file: Path) -> None:
    """Verify the integrity of a genblaze manifest in a media file."""
    try:
        valid = _verify_manifest(file)
        if valid:
            click.echo("OK — manifest hash verified.")
        else:
            click.echo("FAIL — manifest hash mismatch.", err=True)
            raise click.exceptions.Exit(1)
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        # Prefix with exception type so "PermissionError: ..." and
        # "EmbeddingError: ..." are distinguishable at a glance.
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
