"""`lanes tick`: the control table. Per lane, read the facts, pick one action, act, and move on."""

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from lanes import ticket
from lanes.agents import AgentError, Agents
from lanes.hosts import HostAdapter, HostError, MergeConflict
from lanes.models import (
    Event,
    Finding,
    FixRound,
    LanesConfig,
    PullRequest,
    RebaseRequested,
    Reviewer,
    Session,
    VerdictGiven,
)
from lanes.reviewers import ReviewerError, Reviewers
from lanes.store import LaneStore

NEEDS_HUMAN = "needs-human"
MAX_FIX_ROUNDS = 2
MAX_PROMPT_LINES = 200
# The lines `.lanes/gates.py` prints for each problem that fails the gates.
_GATE_PROBLEM = re.compile(
    r"^(new failure|test deleted|new skip|vacuous test, passes on base|preserved test does not pass on \w+"
    r"|config changed without [\w-]+|suite inconclusive at \w+|skips inconclusive at \w+): "
)
_ERROR = re.compile(r"error|fail|traceback", re.IGNORECASE)


def run(
    host: HostAdapter, store: LaneStore, agents: Agents, reviewers: Reviewers, config: LanesConfig, standards: str
) -> list[str]:
    """Act at most once on each lane, and say what was done, one line per lane acted on.

    `standards` is the repository's coding standards, which every reviewer reads.
    """
    history: dict[str, list[Event]] = {}
    for event in store.events():
        history.setdefault(event.ticket, []).append(event)
    lanes: dict[str, PullRequest] = {}
    # One lane is one pull request; should two close the same ticket, the older one is the lane.
    for pr in sorted(host.pull_requests(), key=lambda pr: int(pr.ref)):
        lanes.setdefault(pr.ticket, pr)
    world = _World(host, store, agents, reviewers, config, standards, cache(agents.sessions), cache(_branch))
    said: list[str] = []
    for ticket, pr in lanes.items():
        try:
            done = _act(pr, history.get(ticket, []), world)
        except (AgentError, HostError, ReviewerError) as error:
            done = str(error)
        if done:
            said.append(f"{lane(ticket)}: {done}")
    return said


@dataclass(frozen=True)
class _World:
    """The ports and settings every row of the control table may need."""

    host: HostAdapter
    store: LaneStore
    agents: Agents
    reviewers: Reviewers
    config: LanesConfig
    standards: str
    sessions: Callable[[], list[Session]]
    branch: Callable[[str], str | None]


def _act(pr: PullRequest, history: list[Event], world: _World) -> str | None:
    """The control table: the first row the lane's facts match picks its one action."""
    spent = [event for event in history if isinstance(event, FixRound)]
    if NEEDS_HUMAN in pr.labels:
        return None
    session = _session(pr, world)
    if session is not None and session.state == "failed":
        # A shutdown leaves a session failed but resumable, wherever it was in its work.
        resumed = world.agents.resume(session, "continue", lane(pr.ticket))
        world.store.remember_session(pr.ticket, resumed)
        return f"session {session.session_id[:8]} had failed, resumed {resumed[:8]} with continue"
    if pr.ci == "failed":
        log = world.host.ci_log
        return _fix_round(pr, spent, world, "CI is red", "its failures", lambda n: _red_prompt(pr, log(pr.ref), n))
    if pr.ci == "passed":
        return _review(pr, spent, history, world)
    return None


def _review(pr: PullRequest, spent: list[FixRound], history: list[Event], world: _World) -> str | None:
    """Run the tier's reviewers that have no verdict on the head commit yet, then resume the lane if any blocks."""
    body = world.host.read_ticket(pr.ticket)
    try:
        tier = ticket.lanes_section(body).tier
    except ValueError as error:
        return f"CI is green on #{pr.ref}, but ticket #{pr.ticket} names no tier to review it at: {error}"
    names = world.config.reviewers.for_tier(tier)
    on_head = {
        verdict.reviewer: verdict for verdict in history if isinstance(verdict, VerdictGiven) and verdict.commit == pr.head
    }
    missing = [name for name in names if name not in on_head]
    said: list[str] = []
    if missing:
        session = _session(pr, world)
        if session is None:
            return f"CI is green on #{pr.ref}, but no session of {lane(pr.ticket)} is left to review the worktree of"
        prompt = _review_prompt(pr, body, world.host.diff(pr.ref), world.host.ci_log(pr.ref), world.standards)
        for name in missing:
            verdict = world.reviewers.review(name, prompt, Path(session.cwd))
            # Logged one by one, so that a reviewer failing later doesn't cost the verdicts already given.
            on_head[name] = VerdictGiven(ticket=pr.ticket, reviewer=name, commit=pr.head, verdict=verdict)
            world.store.append(on_head[name])
        said.append(f"reviewed #{pr.ref} at {pr.head[:7]} with {', '.join(missing)}")
    blocking = [(name, finding) for name in names for finding in on_head[name].verdict.blocking]
    if blocking:
        resumed = _fix_round(
            pr, spent, world, "Reviewers block", "their findings", lambda number: _findings_prompt(pr, blocking, number)
        )
        said += [resumed] if resumed else []
    elif missing:
        said.append("nothing blocks")
    else:
        return _land(pr, [event for event in history if isinstance(event, RebaseRequested)], world)
    return "; ".join(said) or None


def _land(pr: PullRequest, asked: list[RebaseRequested], world: _World) -> str | None:
    """A green lane nothing blocks: bring it up to date with its base, have it rebased once if they conflict, and
    once it is up to date, ask for the person's review."""
    try:
        if world.host.update_branch(pr.ref):
            return f"updated #{pr.ref} with its base"
    except MergeConflict:
        return _rebase(pr, asked, world)
    if not pr.draft:
        return None
    # Asked first: asking again is harmless, but once out of draft the lane is never asked for again.
    world.host.request_review(pr.ref)
    world.host.mark_ready(pr.ref)
    return f"#{pr.ref} is up to date and nothing blocks it, marked it ready and asked for your review"


def _rebase(pr: PullRequest, asked: list[RebaseRequested], world: _World) -> str | None:
    """Resume the lane to rebase, unless it already was: then it is still at it, or a person must resolve it."""
    if any(asked_on.commit == pr.head for asked_on in asked):
        return None
    if asked:
        world.host.label_pull_request(pr.ref, NEEDS_HUMAN)
        return f"#{pr.ref} still conflicts with its base after a rebase, labelled #{pr.ref} {NEEDS_HUMAN}"
    session = _session(pr, world)
    if session is None:
        return f"#{pr.ref} conflicts with its base, but no session of {lane(pr.ticket)} is left to resume"
    resumed = world.agents.resume(session, _rebase_prompt(pr), lane(pr.ticket))
    world.store.remember_session(pr.ticket, resumed)
    world.store.append(RebaseRequested(ticket=pr.ticket, commit=pr.head))
    return f"#{pr.ref} conflicts with its base, resumed {resumed[:8]} to rebase on origin/main"


def _fix_round(
    pr: PullRequest, spent: list[FixRound], world: _World, why: str, what: str, prompt: Callable[[int], str]
) -> str | None:
    """Resume the lane with `prompt` for the fix round it builds, or bring in a person once the rounds are spent."""
    if any(spent_on.commit == pr.head for spent_on in spent):
        # A round already spent on this commit is still being worked on: the fix isn't pushed yet.
        return None
    if len(spent) >= MAX_FIX_ROUNDS:
        world.host.label_pull_request(pr.ref, NEEDS_HUMAN)
        return f"{why} on #{pr.ref} after {MAX_FIX_ROUNDS} fix rounds, labelled #{pr.ref} {NEEDS_HUMAN}"
    session = _session(pr, world)
    if session is None:
        return f"{why} on #{pr.ref}, but no session of {lane(pr.ticket)} is left to resume"
    number = len(spent) + 1
    resumed = world.agents.resume(session, prompt(number), lane(pr.ticket))
    world.store.remember_session(pr.ticket, resumed)
    world.store.append(FixRound(ticket=pr.ticket, commit=pr.head))
    return f"{why} on #{pr.ref}, resumed {resumed[:8]} with {what} (fix round {number} of {MAX_FIX_ROUNDS})"


def lane(ticket: str) -> str:
    """A lane's name, `T<n>` after its ticket: the name its session runs under."""
    return f"T{ticket}"


def _session(pr: PullRequest, world: _World) -> Session | None:
    """The session the cache maps the lane to, else the newest one named after it or working on its branch.

    Found without the cache, the lane is remembered again, so that deleting the state directory never strands it.
    """
    cached = world.store.session(pr.ticket)
    sessions = world.sessions()
    for session in sessions:
        if session.session_id == cached:
            return session
    found = [
        session
        for session in sessions
        if re.fullmatch(rf"{lane(pr.ticket)}(\s.*)?", session.name) or world.branch(session.cwd) == pr.branch
    ]
    newest = max(found, key=lambda session: session.started_at, default=None)
    if newest is not None:
        world.store.remember_session(pr.ticket, newest.session_id)
    return newest


def _branch(cwd: str) -> str | None:
    """The branch checked out in `cwd`, or None when it is gone or holds no branch."""
    try:
        done = subprocess.run(["git", "-C", cwd, "branch", "--show-current"], capture_output=True, text=True)
    except OSError:
        return None
    return done.stdout.strip() or None


def _rebase_prompt(pr: PullRequest) -> str:
    return (
        f"Pull request #{pr.ref} conflicts with main at {pr.head[:7]}, so its branch can't be updated. Fetch, then "
        "rebase on origin/main, resolve the conflicts, run the affected tests, and force-push with lease. "
        "Stop once pushed."
    )


def _red_prompt(pr: PullRequest, log: str, number: int) -> str:
    """The gates' problems, then CI's first error lines, cut to fit in `MAX_PROMPT_LINES`."""
    lines = log.splitlines()
    problems = [line for line in lines if _GATE_PROBLEM.match(line)]
    rest = [line for line in lines if not _GATE_PROBLEM.match(line)]
    start = next((i for i, line in enumerate(rest) if _ERROR.search(line)), 0)
    head = [
        f"CI is red on pull request #{pr.ref} at {pr.head[:7]}. This is fix round {number} of {MAX_FIX_ROUNDS}.",
        "",
        "The gates failed on:",
    ]
    middle = ["", "First error lines from CI:"]
    tail = ["", "Fix them, run the affected tests, then commit and push. Stop once pushed."]
    room = MAX_PROMPT_LINES - len(head) - len(middle) - len(tail)
    problems = problems[:room] or ["(no gate problem lines; see the errors below)"]
    errors = rest[start:][: max(room - len(problems), 0)]
    return "\n".join(head + problems + middle + errors + tail)


def _findings_prompt(pr: PullRequest, blocking: list[tuple[Reviewer, Finding]], number: int) -> str:
    """Every blocking finding with the reviewer that gave it, cut to fit in `MAX_PROMPT_LINES`; nits stay out."""
    head = [
        f"Reviewers found blocking problems in pull request #{pr.ref} at {pr.head[:7]}. "
        f"This is fix round {number} of {MAX_FIX_ROUNDS}.",
        "",
    ]
    tail = ["", "Fix them, run the affected tests, then commit and push. Stop once pushed."]
    findings = [f"- {finding.file}:{finding.line}: {finding.message} ({name})" for name, finding in blocking]
    return "\n".join(head + findings[: MAX_PROMPT_LINES - len(head) - len(tail)] + tail)


def _review_prompt(pr: PullRequest, body: str, diff: str, log: str, standards: str) -> str:
    """What a reviewer is given: the ticket, the diff, the CI log and the coding standards, and never the transcript."""
    return "\n".join([
        f"Review commit {pr.head} of pull request #{pr.ref}, which closes ticket #{pr.ticket}. The checkout you are "
        "in is that pull request's worktree.",
        "",
        "## Ticket",
        "",
        body.strip(),
        "",
        "## Diff against the base",
        "",
        "```diff",
        diff.rstrip("\n"),
        "```",
        "",
        "## CI log",
        "",
        "```",
        log.rstrip("\n"),
        "```",
        "",
        "## Coding standards",
        "",
        standards.strip() or "(this repository has no CODING_STANDARDS.md)",
    ])
