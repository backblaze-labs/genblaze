"""Extract command — print embedded manifest from a media file."""

from pathlib import Path

import click
from genblaze_core.canonical.json import canonical_json
from genblaze_core.exceptions import EmbeddingError, ManifestError
from genblaze_core.media import get_handler, guess_mime
from genblaze_core.media.sidecar import SidecarHandler
from genblaze_core.models.manifest import Manifest


def _extract_manifest(file: Path) -> Manifest:
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


def _manifest_json_for_display(manifest: Manifest) -> str:
    """Serialize for inspection without bypassing write-schema guards."""
    try:
        manifest.assert_writable_schema()
    except ManifestError:
        return canonical_json(manifest.model_dump(mode="python"))
    return manifest.to_canonical_json()


@click.command()
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("--format", "fmt", type=click.Choice(["json", "summary"]), default="json")
def extract(file: Path, fmt: str) -> None:
    """Extract and display the genblaze manifest from a media file."""
    try:
        manifest = _extract_manifest(file)
        if fmt == "json":
            click.echo(_manifest_json_for_display(manifest))
        else:
            report = manifest.verification_report()
            click.echo(f"Run ID:    {manifest.run.run_id}")
            click.echo(f"Steps:     {len(manifest.run.steps)}")
            click.echo(f"Hash:      {manifest.canonical_hash}")
            click.echo(f"Hash OK:   {report.hash_ok}")
            click.echo(f"Output sha256: {len(report.missing_sha256_ids)} missing or malformed")
            click.echo(f"Verified:  {report.ok}")
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
