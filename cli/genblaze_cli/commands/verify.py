"""Verify command — check manifest hash and output sha256 coverage."""

from pathlib import Path

import click

from genblaze_cli.manifest_io import extract_manifest


@click.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--hash-only",
    is_flag=True,
    help="Only verify canonical_hash; do not require output sha256 declarations.",
)
def verify(file: Path, hash_only: bool) -> None:
    """Verify an embedded, sidecar, or standalone genblaze manifest."""
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
        click.echo(
            "OK: manifest hash verified; all output assets declare sha256. "
            "Asset bytes were not fetched or compared."
        )
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        # Prefix with exception type so "PermissionError: ..." and
        # "EmbeddingError: ..." are distinguishable at a glance.
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
