"""Extract command — print embedded manifest from a media file."""

from pathlib import Path

import click
from genblaze_core.canonical.json import canonical_json
from genblaze_core.exceptions import ManifestError
from genblaze_core.models.manifest import Manifest

from genblaze_cli.manifest_io import extract_manifest


def _manifest_json_for_display(manifest: Manifest) -> str:
    """Serialize for inspection without bypassing write-schema guards."""
    try:
        manifest.assert_writable_schema()
    except ManifestError:
        return canonical_json(manifest.model_dump(mode="python"))
    return manifest.to_canonical_json()


@click.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--format", "fmt", type=click.Choice(["json", "summary"]), default="json")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Write extracted manifest JSON to this file instead of stdout.",
)
def extract(file: Path, fmt: str, output: Path | None) -> None:
    """Extract and display the genblaze manifest from a media file."""
    try:
        manifest = extract_manifest(file)
        if fmt == "json":
            json_str = _manifest_json_for_display(manifest)
            if output is not None:
                output.write_text(json_str, encoding="utf-8")
            else:
                click.echo(json_str)
        else:
            report = manifest.verification_report()
            click.echo(f"Run ID:    {manifest.run.run_id}")
            click.echo(f"Steps:     {len(manifest.run.steps)}")
            click.echo(f"Hash:      {manifest.canonical_hash}")
            click.echo(f"Hash OK:   {report.hash_ok}")
            click.echo(f"Output sha256: {len(report.unverified_sha256_ids)} missing or malformed")
            click.echo(f"Verified:  {report.ok} (asset bytes were not fetched or compared)")
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
