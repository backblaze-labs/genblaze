"""Extract command — print embedded manifest from a media file."""

from pathlib import Path

import click
from genblaze_core.exceptions import EmbeddingError
from genblaze_core.media import get_handler, guess_mime
from genblaze_core.media.sidecar import SidecarHandler


def _extract_manifest(file: Path):
    """Try format-specific handler first, then sidecar fallback."""
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
@click.option("--format", "fmt", type=click.Choice(["json", "summary"]), default="json")
def extract(file: Path, fmt: str) -> None:
    """Extract and display the genblaze manifest from a media file."""
    try:
        manifest = _extract_manifest(file)
        if fmt == "json":
            click.echo(manifest.to_canonical_json())
        else:
            click.echo(f"Run ID:    {manifest.run.run_id}")
            click.echo(f"Steps:     {len(manifest.run.steps)}")
            click.echo(f"Hash:      {manifest.canonical_hash}")
            click.echo(f"Verified:  {manifest.verify()}")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
