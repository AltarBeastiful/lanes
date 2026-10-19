"""`lanes go`: start the lane for one ticket."""

import subprocess
from pathlib import Path

from lanes import ticket
from lanes.agents import Agents
from lanes.hosts import HostAdapter
from lanes.models import LaneDispatched, LanesConfig, Route, Tier
from lanes.store import LaneStore
from lanes.tick import lane

AGENT = "implementer"
IMPLEMENT = "/mattpocock-skills:implement"
PR = "/mattpocock-skills:pr"


class Refused(Exception):
    """`lanes go` won't start the lane; each argument is one reason why."""


class FetchFailed(Exception):
    """`git fetch origin` failed, so the lane would branch from what may be a stale `origin/HEAD`."""


def start(
    host: HostAdapter,
    store: LaneStore,
    agents: Agents,
    config: LanesConfig,
    ref: str,
    repo: Path,
    route: Route | None = None,
    tier: Tier | None = None,
) -> list[str]:
    """Start ticket `ref`'s lane in the background from `repo`, at `route` and `tier` in place of the ticket's when
    given, and say what was done."""
    if not ref.isdigit():
        raise Refused("name the ticket by its number, which its lane is named after")
    # `012` and `12` are the same lane.
    ref = str(int(ref))
    body = host.read_ticket(ref)
    problems = ticket.problems(body)
    if problems:
        raise Refused(*problems)
    # Only a ticket you marked ready becomes a prompt, so that an outsider's issue text never does.
    if ticket.READY not in host.labels(ref):
        raise Refused(f"not labelled {ticket.READY}; run lanes check {ref}")
    section = ticket.lanes_section(body)
    dispatched = LaneDispatched(
        ticket=ref,
        route=route or section.route,
        tier=tier or section.tier,
        ticket_route=section.route if route not in (None, section.route) else None,
        ticket_tier=section.tier if tier not in (None, section.tier) else None,
    )
    # A new worktree branches from the cached `origin/HEAD`, which without a fetch can predate the blocker's merge.
    _fetch(repo)
    model = config.models.for_tier(dispatched.tier)
    session = agents.dispatch(lane(ref), AGENT, _prompt(ref, body, dispatched.tier), model, repo)
    # The cache comes before the name when tick looks a lane up, so a T<n> session of another repository never
    # stands in for this one.
    store.remember_session(ref, session)
    store.append(dispatched)
    said = [f"{lane(ref)}: started session {session[:8]} at route {dispatched.route}, tier {dispatched.tier}, on {model}"]
    overrides = [
        f"{field} {named} with {used}"
        for field, named, used in (
            ("route", dispatched.ticket_route, dispatched.route),
            ("tier", dispatched.ticket_tier, dispatched.tier),
        )
        if named is not None
    ]
    if overrides:
        said.append(f"{lane(ref)}: overriding the ticket's {' and '.join(overrides)}")
    return said


def _prompt(ref: str, body: str, tier: Tier) -> str:
    """Medium and high lanes run the ticket test-first through `/implement`; small and trivial ones get its text,
    so that small work doesn't pay for skills that add nothing."""
    if tier in ("medium", "high"):
        return f"{IMPLEMENT} #{ref}, then write the pull request body with {PR}"
    text = f"Ticket #{ref}:\n\n{body}"
    return f"{text}\n\nWhen done, write the pull request body with {PR}" if tier == "small" else text


def _fetch(repo: Path) -> None:
    done = subprocess.run(["git", "fetch", "-q", "origin"], cwd=repo, capture_output=True, text=True)
    if done.returncode != 0:
        raise FetchFailed(f"git fetch origin failed: {done.stderr.strip()}")
