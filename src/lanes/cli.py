"""The `lanes` command line: builds the ports and passes them in."""

import subprocess
import sys
from pathlib import Path
from typing import Any, get_args

import click

from lanes import agents, install, registry, ticket
from lanes.hosts import HostAdapter, TicketNotFound
from lanes.hosts.markdown import MarkdownHost
from lanes.models import Host, LanesConfig, Reviewer, TierReviewers, Tracker


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


@main.command()
@click.option("--force", is_flag=True, help="Overwrite installed files that differ from what init writes.")
def init(force: bool) -> None:
    """Set up the current repository for Lanes: config, hotspots, gates and runner."""
    repo = _git_root()
    host = click.prompt("Host for pull requests", type=_choice(Host), default="github")
    tracker = click.prompt("Tracker for tickets", type=_choice(Tracker), default="github")
    test_command = click.prompt("Test command", default="pytest")
    if not install.runs_pytest(test_command):
        click.echo(
            f"lanes init installs only the pytest runner, so it can't set up {test_command!r}. Commit a "
            "`.lanes/runner` script with `run [files...]` and `skips` commands that follows the runner "
            f"contract in docs/adr/0002-runner-script.md ({install.RUNNER_CONTRACT}).",
            err=True,
        )
        sys.exit(2)
    max_lanes = click.prompt("Lanes running at once (N)", type=click.IntRange(min=1), default=2)
    codex_allowed = click.confirm("May Codex read this repository's code?", default=False)
    second_reviewer = "codex" if codex_allowed else "claude-adversarial"
    defaults = {
        "trivial": [],
        "small": [second_reviewer],
        "medium": [second_reviewer, "claude-high"],
        "high": [second_reviewer, "claude-max"],
    }
    reviewers = {
        tier: click.prompt(
            f"Reviewers for {tier} tickets",
            default=", ".join(default) or "none",
            value_proc=lambda answer: _reviewers(answer, codex_allowed),
        )
        for tier, default in defaults.items()
    }
    config = LanesConfig(
        host=host,
        tracker=tracker,
        test_command=test_command,
        max_lanes=max_lanes,
        codex_allowed=codex_allowed,
        reviewers=TierReviewers(**reviewers),
    )
    differing = [] if force else install.differing(repo, config)
    if differing:
        for path in differing:
            click.echo(f"{path} differs from what lanes init writes; pass --force to overwrite it", err=True)
        sys.exit(2)
    install.install(repo, config)
    registry.register(repo)
    if not agents.workspace_trusted(repo):
        click.echo(
            f"warning: Claude Code's workspace trust has not been accepted for {repo}, so lanes "
            "started from the timer will exit with 'Workspace not trusted'. Run `claude` here once and accept it.",
            err=True,
        )
    click.echo("Wrote .lanes/ and .github/workflows/gates.yml; commit them under the allow-config label.")


def _git_root() -> Path:
    found = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if found.returncode != 0:
        click.echo("lanes init: not a git repository; run it inside the repository to set up", err=True)
        sys.exit(2)
    return Path(found.stdout.strip()).resolve()


def _choice(literal: Any) -> click.Choice[str]:
    return click.Choice(get_args(literal))


def _reviewers(answer: str, codex_allowed: bool) -> list[str]:
    """A comma-separated reviewer list, or `none` for CI only."""
    names = [] if answer.strip() == "none" else [name.strip() for name in answer.split(",") if name.strip()]
    unknown = [name for name in names if name not in get_args(Reviewer)]
    if unknown:
        allowed = ", ".join(get_args(Reviewer))
        raise click.BadParameter(f"unknown reviewer {', '.join(unknown)}: allowed values are {allowed}, or none")
    if "codex" in names and not codex_allowed:
        raise click.BadParameter("Codex may not read this repository's code; pick another reviewer")
    return names
