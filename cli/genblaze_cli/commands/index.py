"""Index command — write manifest data to a Parquet sink."""

import json
from pathlib import Path

import click


@click.command()
@click.argument("manifest_file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default="./genblaze_index",
    help="Base directory for Parquet output.",
)
def index(manifest_file: Path, output: Path) -> None:
    """Write manifest data to a Parquet sink for querying."""
    from genblaze_core.models.manifest import Manifest
    from genblaze_core.sinks.parquet import ParquetSink

    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest = Manifest.model_validate(data)
    except Exception as exc:
        raise click.ClickException(f"Failed to load manifest: {exc}") from exc

    try:
        sink = ParquetSink(output)
        sink.write_run(manifest.run, manifest)
        click.echo(f"Indexed run {manifest.run.run_id} to {output}")
    except Exception as exc:
        raise click.ClickException(f"Failed to write Parquet: {exc}") from exc
