"""The `lanes` command line: builds the ports and passes them in."""

import sys
from pathlib import Path

import click

from lanes import ticket
from lanes.hosts import HostAdapter, TicketNotFound
from lanes.hosts.markdown import MarkdownHost


def _host() -> HostAdapter:
    return MarkdownHost(Path.cwd())


@click.group()
def main() -> None:
    """Take refined tickets to reviewed, mergeable pull requests."""


@main.command()
@click.argument("ref")
def check(ref: str) -> None:
    """Accept or reject the ticket REF as ready for a lane."""
    try:
        body = _host().read_ticket(ref)
    except TicketNotFound as error:
        click.echo(f"{ref}: {error}", err=True)
        sys.exit(2)
    found = ticket.problems(body)
    for problem in found:
        click.echo(f"{ref}: {problem}")
    if found:
        sys.exit(1)
    click.echo(f"{ref}: ok")
