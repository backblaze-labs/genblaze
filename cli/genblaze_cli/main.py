"""CLI entry point."""

import click

from genblaze_cli.commands.extract import extract
from genblaze_cli.commands.index import index
from genblaze_cli.commands.replay import replay
from genblaze_cli.commands.verify import verify


@click.group()
@click.version_option(package_name="genblaze-cli")
def cli() -> None:
    """genblaze — media generation manifest toolkit."""


cli.add_command(extract)
cli.add_command(index)
cli.add_command(replay)
cli.add_command(verify)
