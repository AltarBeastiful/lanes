"""The `lanes` command line: builds the ports and passes them in."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, get_args

import click

from lanes import go as dispatch, install, lock, registry, report as reports, ticket, tick as ticks, timer
from lanes.agents import AgentError
from lanes.agents.claude import ClaudeAgents, workspace_trusted
from lanes.hosts import HostAdapter, HostError
from lanes.hosts.github import GitHubHost
from lanes.hosts.markdown import MarkdownHost
from lanes.models import FailureLogged, Host, LanesConfig, Reviewer, Route, Tier, TierReviewers, Tracker
from lanes.reviewers import ReviewerError
from lanes.reviewers.local import LocalReviewers
from lanes.store import LaneStore
from lanes.store.files import FileStore, state_directory


# The triage states a ticket leaves when it passes; ready-for-human and wontfix are a person's call and stay.
TRIAGE = ("needs-triage", "needs-info")


def _host() -> HostAdapter:
    """The tracker `.lanes/lanes.toml` names; markdown tickets under the working directory without one."""
    root = _git_root_or_none()
    config = root / ".lanes" / "lanes.toml" if root else None
    if config and config.is_file():
        tracker = LanesConfig.model_validate(tomllib.loads(config.read_text(encoding="utf-8"))).tracker
        if tracker == "github":
            return GitHubHost()
    return MarkdownHost(Path.cwd())


def _config() -> LanesConfig:
    """`.lanes/lanes.toml`, which every command that works on pull requests needs."""
    config = _git_root() / ".lanes" / "lanes.toml"
    if not config.is_file():
        command = click.get_current_context().info_name
        click.echo(f"lanes {command}: no .lanes/lanes.toml names the pull request host; run lanes init", err=True)
        sys.exit(2)
    return LanesConfig.model_validate(tomllib.loads(config.read_text(encoding="utf-8")))


def _standards() -> str:
    """The repository's `CODING_STANDARDS.md`, which every reviewer reads; empty when it has none."""
    standards = _git_root() / "CODING_STANDARDS.md"
    return standards.read_text(encoding="utf-8") if standards.is_file() else ""


def _store() -> LaneStore:
    return FileStore(state_directory(_git_root()))


@click.group()
def main() -> None:
    """Take refined tickets to reviewed, mergeable pull requests."""


@main.command()
@click.argument("ref")
def check(ref: str) -> None:
    """Accept or reject the ticket REF as ready for a lane; a ticket that passes goes from triage to ready-for-agent."""
    host = _host()
    try:
        found = ticket.problems(host.read_ticket(ref))
        for problem in found:
            click.echo(f"{ref}: {problem}")
        if found:
            sys.exit(1)
        host.add_label(ref, ticket.READY)
        for label in TRIAGE:
            host.remove_label(ref, label)
    except HostError as error:
        click.echo(f"{ref}: {error}", err=True)
        sys.exit(2)
    click.echo(f"{ref}: ok, labelled {ticket.READY}")


@main.command()
@click.argument("ref")
@click.argument("what")
def fail(ref: str, what: str) -> None:
    """Log WHAT, a defect caught at merge in the lane for ticket REF."""
    try:
        section = ticket.lanes_section(_host().read_ticket(ref))
    except (HostError, ValueError) as error:
        click.echo(f"{ref}: {error}", err=True)
        sys.exit(2)
    _store().append(FailureLogged(ticket=ref, route=section.route, tier=section.tier, what=what))
    click.echo(f"{ref}: logged a defect caught at merge")


@main.command()
def report() -> None:
    """Show first-pass acceptance, fix rounds and defects caught at merge, per route and tier."""
    click.echo(reports.render(_store().events()))


@main.command()
@click.option("--all", "every", is_flag=True, help="Tick every repository lanes init registered, one after another.")
def tick(every: bool) -> None:
    """Act once on each lane: review a green one, resume it with failures or blocking findings, update or mark ready a
    clean one, continue a failed session, or have a blocked or needs-human lane ask you why it needs you."""
    if not every:
        _tick_here()
        return
    repos = registry.load().repos
    if not repos:
        click.echo("lanes tick --all: no repository is registered; run lanes init in one")
    start, failed = Path.cwd(), False
    for repo in repos:
        click.echo(f"{repo}:")
        try:
            os.chdir(repo)
            _tick_here()
        except SystemExit as stop:
            # The tick has said why already.
            failed = failed or bool(stop.code)
        except Exception as error:
            # One broken repository must not stop the others.
            click.echo(f"lanes tick: {repo}: {error}", err=True)
            failed = True
        finally:
            os.chdir(start)
    if failed:
        sys.exit(1)


def _tick_here() -> None:
    """Tick the repository the working directory is in."""
    config = _config()
    # GitHub is the only pull request host the config allows so far.
    host = GitHubHost()
    with lock.exclusive(_git_root()) as held:
        if not held:
            click.echo("lanes tick: another tick is running in this repository; leaving it alone")
            return
        try:
            for line in ticks.run(host, _store(), ClaudeAgents(), LocalReviewers(), config, _standards()):
                click.echo(line)
        except (HostError, AgentError, ReviewerError) as error:
            click.echo(f"lanes tick: {error}", err=True)
            sys.exit(2)


@main.command()
@click.argument("ref")
@click.option("--route", type=click.Choice(get_args(Route)), help="Run the lane at this route instead of the ticket's; logged.")
@click.option("--tier", type=click.Choice(get_args(Tier)), help="Run the lane at this tier instead of the ticket's; logged.")
def go(ref: str, route: Route | None, tier: Tier | None) -> None:
    """Start the lane for ticket REF: fetch origin, then a background session named T<n> with the prompt and model
    its tier calls for."""
    config = _config()
    try:
        for line in dispatch.dispatch(_host(), _store(), ClaudeAgents(), config, ref, _git_root(), route, tier):
            click.echo(line)
    except dispatch.Refused as refused:
        for reason in refused.args:
            click.echo(f"{ref}: {reason}")
        sys.exit(1)
    except (HostError, AgentError, dispatch.FetchFailed) as error:
        click.echo(f"{ref}: {error}", err=True)
        sys.exit(2)


@main.command()
@click.option("--force", is_flag=True, help="Overwrite installed files that differ from what init writes.")
def init(force: bool) -> None:
    """Set up the current repository for Lanes: config, hotspots, gates, runner, agents and hooks; register it and
    enable the timer that ticks every registered repository."""
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
    lint_command = click.prompt("Lint command, run before a lane stops (blank for none)", default="", show_default=False)
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
        lint_command=lint_command,
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
    try:
        timer.install()
    except timer.TimerError as error:
        click.echo(
            f"warning: the timer that runs lanes tick --all is written but not enabled: {error}. Once systemctl "
            f"works, run `systemctl --user enable --now {timer.TIMER}`.",
            err=True,
        )
    if not workspace_trusted(repo):
        click.echo(
            f"warning: Claude Code's workspace trust has not been accepted for {repo}, so lanes "
            "started from the timer will exit with 'Workspace not trusted'. Run `claude` here once and accept it.",
            err=True,
        )
    click.echo("Wrote .lanes/, .claude/agents/ and .github/workflows/gates.yml; commit them under the allow-config label.")


def _git_root() -> Path:
    root = _git_root_or_none()
    if root is None:
        command = click.get_current_context().info_name
        click.echo(f"lanes {command}: not a git repository; run it inside the repository", err=True)
        sys.exit(2)
    return root


def _git_root_or_none() -> Path | None:
    found = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(found.stdout.strip()).resolve() if found.returncode == 0 else None


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
