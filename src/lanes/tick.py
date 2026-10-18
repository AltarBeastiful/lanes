"""`lanes tick`: the control table. Per lane, read the facts, pick one action, act, and move on."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from lanes import ticket
from lanes.agents import AgentError, Agents
from lanes.hosts import HostAdapter, HostError
from lanes.models import Finding, FixRound, LanesConfig, PullRequest, VerdictGiven, Reviewer, Session
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
    rounds: dict[str, list[FixRound]] = {}
    verdicts: dict[str, list[VerdictGiven]] = {}
    for event in store.events():
        if isinstance(event, FixRound):
            rounds.setdefault(event.ticket, []).append(event)
        elif isinstance(event, VerdictGiven):
            verdicts.setdefault(event.ticket, []).append(event)
    lanes: dict[str, PullRequest] = {}
    # One lane is one pull request; should two close the same ticket, the older one is the lane.
    for pr in sorted(host.pull_requests(), key=lambda pr: int(pr.ref)):
        lanes.setdefault(pr.ticket, pr)
    world = _World(host, store, agents, reviewers, config, standards, cache(agents.sessions))
    said: list[str] = []
    for ticket, pr in lanes.items():
        try:
            done = _act(pr, rounds.get(ticket, []), verdicts.get(ticket, []), world)
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


def _act(pr: PullRequest, spent: list[FixRound], given: list[VerdictGiven], world: _World) -> str | None:
    """The control table: the first row the lane's facts match picks its one action."""
    if NEEDS_HUMAN in pr.labels:
        return None
    if pr.ci == "failed":
        log = world.host.ci_log
        return _fix_round(pr, spent, world, "CI is red", "its failures", lambda n: _red_prompt(pr, log(pr.ref), n))
    if pr.ci == "passed":
        return _review(pr, spent, given, world)
    return None


def _review(pr: PullRequest, spent: list[FixRound], given: list[VerdictGiven], world: _World) -> str | None:
    """Run the tier's reviewers that have no verdict on the head commit yet, then resume the lane if any blocks."""
    body = world.host.read_ticket(pr.ticket)
    try:
        tier = ticket.lanes_section(body).tier
    except ValueError as error:
        return f"CI is green on #{pr.ref}, but ticket #{pr.ticket} names no tier to review it at: {error}"
    names = world.config.reviewers.for_tier(tier)
    on_head = {verdict.reviewer: verdict for verdict in given if verdict.commit == pr.head}
    missing = [name for name in names if name not in on_head]
    said: list[str] = []
    if missing:
        session = _session(pr.ticket, world.store, world.sessions())
        if session is None:
            return f"CI is green on #{pr.ref}, but no session named {lane(pr.ticket)} is left to review the worktree of"
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
    return "; ".join(said) or None


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
    session = _session(pr.ticket, world.store, world.sessions())
    if session is None:
        return f"{why} on #{pr.ref}, but no session named {lane(pr.ticket)} is left to resume"
    number = len(spent) + 1
    resumed = world.agents.resume(session, prompt(number), lane(pr.ticket))
    world.store.remember_session(pr.ticket, resumed)
    world.store.append(FixRound(ticket=pr.ticket, commit=pr.head))
    return f"{why} on #{pr.ref}, resumed {resumed[:8]} with {what} (fix round {number} of {MAX_FIX_ROUNDS})"


def lane(ticket: str) -> str:
    """A lane's name, `T<n>` after its ticket: the name its session runs under."""
    return f"T{ticket}"


def _session(ticket: str, store: LaneStore, sessions: list[Session]) -> Session | None:
    """The session the cache maps the lane to, else the newest one named after it."""
    cached = store.session(ticket)
    for session in sessions:
        if session.session_id == cached:
            return session
    named = [session for session in sessions if re.fullmatch(rf"{lane(ticket)}(\s.*)?", session.name)]
    return max(named, key=lambda session: session.started_at, default=None)


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
