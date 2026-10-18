"""`lanes tick`: the control table. Per lane, read the facts, pick one action, act, and move on."""

import os
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
    Ask,
    Event,
    Finding,
    FixRound,
    HumanAsked,
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
# What a lane writes at the root of its worktree, uncommitted, when it needs a contract no merged code provides.
BLOCKED_ON = ".lanes-blocked-on"
MAX_FIX_ROUNDS = 2
MAX_PROMPT_LINES = 200
NOTIFY_TIMEOUT = 60
# The lines `.lanes/gates.py` prints for each problem that fails the gates.
_GATE_PROBLEM = re.compile(
    r"^(new failure|test deleted|new skip|vacuous test, passes on base|preserved test does not pass on \w+"
    r"|config changed without [\w-]+|suite inconclusive at \w+|skips inconclusive at \w+): "
)
_ERROR = re.compile(r"error|fail|traceback", re.IGNORECASE)
_LANE = re.compile(r"T(\d+)(\s.*)?")


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
    # A lane that stops blocked does so before opening its pull request: only its session, named `T<n>`, leads to it.
    unanchored = {str(int(found[1])) for session in world.sessions() if (found := _LANE.fullmatch(session.name))}
    for ticket in sorted(unanchored - lanes.keys(), key=int):
        try:
            done = _act_unanchored(ticket, history.get(ticket, []), world)
        except AgentError as error:
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
        return _labelled(pr, history, world)
    session = _session(pr, world)
    blocked_on = _blocked_on(session)
    if blocked_on is not None:
        return _blocked(pr.ticket, session, pr.head, blocked_on, history, world)
    if session is not None and session.state == "failed":
        # A shutdown leaves a session failed but resumable, wherever it was in its work.
        resumed = world.agents.resume(session, "continue", lane(pr.ticket))
        world.store.remember_session(pr.ticket, resumed)
        return f"session {session.session_id[:8]} had failed, resumed {resumed[:8]} with continue"
    if pr.ci == "failed":
        log = world.host.ci_log
        opening = f"CI is red on pull request #{pr.ref} at {pr.head[:7]}."
        return _fix_round(pr, spent, world, "CI is red", "its failures", opening, lambda: _failures(log(pr.ref)))
    if pr.ci == "passed":
        return _review(pr, spent, history, world)
    return None


def _act_unanchored(ticket: str, history: list[Event], world: _World) -> str | None:
    """A lane with a session but no pull request yet: only a blocked one is acted on."""
    session = _session_of(ticket, None, world)
    blocked_on = _blocked_on(session)
    if blocked_on is None:
        return None
    return _blocked(ticket, session, None, blocked_on, history, world)


def _blocked(
    ticket: str, session: Session | None, commit: str | None, blocked_on: str, history: list[Event], world: _World
) -> str | None:
    """A blocked lane asks the person once per commit, with what its blocked-on file says it needs, and is otherwise
    left alone."""
    if any(isinstance(event, HumanAsked) and event.why == "blocked" and event.commit == commit for event in history):
        return None
    reason = "it is blocked on a contract no merged code provides"
    return _ask(ticket, session, commit, "blocked", reason, [f"{BLOCKED_ON}:", *blocked_on.splitlines()], world)


def _labelled(pr: PullRequest, history: list[Event], world: _World) -> str | None:
    """A lane labelled `needs-human` asks the person once, with CI's failures or the blocking findings on its head.

    Lanes labels it and asks in the same tick; one labelled by anyone else asks on the next. Once asked, it is left
    alone, even when someone pushes to it, and Lanes never deletes it.
    """
    if any(isinstance(event, HumanAsked) and event.why != "blocked" for event in history):
        return None
    if pr.ci == "failed":
        details = _failures(world.host.ci_log(pr.ref))
    else:
        verdicts = [event for event in history if isinstance(event, VerdictGiven) and event.commit == pr.head]
        details = _findings([(verdict.reviewer, finding) for verdict in verdicts for finding in verdict.verdict.blocking])
    reason = f"pull request #{pr.ref} is labelled {NEEDS_HUMAN}"
    return _ask(pr.ticket, _session(pr, world), pr.head, "needs_human", reason, details, world)


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
        opening = f"Reviewers found blocking problems in pull request #{pr.ref} at {pr.head[:7]}."
        resumed = _fix_round(
            pr, spent, world, "Reviewers block", "their findings", opening, lambda: _findings(blocking)
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
        why = f"pull request #{pr.ref} still conflicts with main at {pr.head[:7]}, after the one rebase Lanes asks for"
        said = _ask(pr.ticket, _session(pr, world), pr.head, "conflict", why, [], world)
        return f"#{pr.ref} still conflicts with its base after a rebase, labelled #{pr.ref} {NEEDS_HUMAN}; {said}"
    session = _session(pr, world)
    if session is None:
        return f"#{pr.ref} conflicts with its base, but no session of {lane(pr.ticket)} is left to resume"
    resumed = world.agents.resume(session, _rebase_prompt(pr), lane(pr.ticket))
    world.store.remember_session(pr.ticket, resumed)
    world.store.append(RebaseRequested(ticket=pr.ticket, commit=pr.head))
    return f"#{pr.ref} conflicts with its base, resumed {resumed[:8]} to rebase on origin/main"


def _fix_round(
    pr: PullRequest,
    spent: list[FixRound],
    world: _World,
    why: str,
    what: str,
    opening: str,
    details: Callable[[], list[str]],
) -> str | None:
    """Resume the lane with `details`, the failures or findings to fix, or once the rounds are spent, ask a person."""
    if any(spent_on.commit == pr.head for spent_on in spent):
        # A round already spent on this commit is still being worked on: the fix isn't pushed yet.
        return None
    if len(spent) >= MAX_FIX_ROUNDS:
        world.host.label_pull_request(pr.ref, NEEDS_HUMAN)
        reason = f"{why} on #{pr.ref} after {MAX_FIX_ROUNDS} fix rounds"
        said = _ask(pr.ticket, _session(pr, world), pr.head, "fix_rounds", reason, details(), world)
        return f"{reason}, labelled #{pr.ref} {NEEDS_HUMAN}; {said}"
    session = _session(pr, world)
    if session is None:
        return f"{why} on #{pr.ref}, but no session of {lane(pr.ticket)} is left to resume"
    number = len(spent) + 1
    resumed = world.agents.resume(session, _fix_prompt(opening, details(), number), lane(pr.ticket))
    world.store.remember_session(pr.ticket, resumed)
    world.store.append(FixRound(ticket=pr.ticket, commit=pr.head))
    return f"{why} on #{pr.ref}, resumed {resumed[:8]} with {what} (fix round {number} of {MAX_FIX_ROUNDS})"


def _ask(
    ticket: str, session: Session | None, commit: str | None, why: Ask, reason: str, details: list[str], world: _World
) -> str:
    """Resume the lane's `session` to ask the person, with AskUserQuestion, why it needs them, so that the session
    shows Needs input; `details` are the last findings or failures."""
    if session is None:
        return f"no session of {lane(ticket)} is left to ask you through"
    resumed = world.agents.resume(session, _ask_prompt(ticket, reason, details), lane(ticket))
    world.store.remember_session(ticket, resumed)
    world.store.append(HumanAsked(ticket=ticket, why=why, commit=commit))
    said = f"resumed {resumed[:8]} and asked you why: {reason}"
    if world.config.notify_cmd:
        failed = _notify(world.config.notify_cmd, ticket, f"Lanes needs a person in lane {lane(ticket)}: {reason}")
        said += f"; {failed}" if failed else ""
    return said


def _notify(command: str, ticket: str, message: str) -> str | None:
    """Run the configured `notify_cmd` through the shell with `$LANES_LANE` and `$LANES_MESSAGE` set; say why it
    failed, if it did."""
    env = {**os.environ, "LANES_LANE": lane(ticket), "LANES_MESSAGE": message}
    try:
        done = subprocess.run(command, shell=True, env=env, capture_output=True, text=True, timeout=NOTIFY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"notify_cmd ran over {NOTIFY_TIMEOUT}s and was stopped"
    except OSError as error:
        return f"notify_cmd could not start: {error}"
    return f"notify_cmd exited {done.returncode}" if done.returncode != 0 else None


def lane(ticket: str) -> str:
    """A lane's name, `T<n>` after its ticket: the name its session runs under."""
    return f"T{ticket}"


def _session(pr: PullRequest, world: _World) -> Session | None:
    return _session_of(pr.ticket, pr.branch, world)


def _session_of(ticket: str, branch: str | None, world: _World) -> Session | None:
    """The session the cache maps the lane to, else the newest one named after it or working on its `branch`.

    Found without the cache, the lane is remembered again, so that deleting the state directory never strands it.
    """
    cached = world.store.session(ticket)
    sessions = world.sessions()
    for session in sessions:
        if session.session_id == cached:
            return session
    found = [
        session
        for session in sessions
        if re.fullmatch(rf"{lane(ticket)}(\s.*)?", session.name)
        or (branch is not None and world.branch(session.cwd) == branch)
    ]
    newest = max(found, key=lambda session: session.started_at, default=None)
    if newest is not None:
        world.store.remember_session(ticket, newest.session_id)
    return newest


def _blocked_on(session: Session | None) -> str | None:
    """What the session's blocked-on file says, or None when it wrote none."""
    if session is None:
        return None
    try:
        return (Path(session.cwd) / BLOCKED_ON).read_text(encoding="utf-8")
    except OSError:
        return None


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


def _ask_prompt(ticket: str, reason: str, details: list[str]) -> str:
    """The fixed prompt that has a lane ask the person why it needs them, cut to fit in `MAX_PROMPT_LINES`."""
    head = [f"Lanes needs a person in lane {lane(ticket)}: {reason}.", ""]
    tail = [
        "",
        "Ask me with AskUserQuestion: say why you need me, using the reason above, give the last findings or "
        "failures, and offer the ways forward you see. Then stop, and change nothing before I answer.",
    ]
    details = details[: MAX_PROMPT_LINES - len(head) - len(tail)] or ["(no findings or failures to show)"]
    return "\n".join(head + details + tail)


def _fix_prompt(opening: str, details: list[str], number: int) -> str:
    """The prompt of fix round `number`, its details cut to fit in `MAX_PROMPT_LINES`."""
    head = [f"{opening} This is fix round {number} of {MAX_FIX_ROUNDS}.", ""]
    tail = ["", "Fix them, run the affected tests, then commit and push. Stop once pushed."]
    return "\n".join(head + details[: MAX_PROMPT_LINES - len(head) - len(tail)] + tail)


def _failures(log: str) -> list[str]:
    """The gates' problems, then CI's first error lines."""
    lines = log.splitlines()
    problems = [line for line in lines if _GATE_PROBLEM.match(line)]
    rest = [line for line in lines if not _GATE_PROBLEM.match(line)]
    start = next((i for i, line in enumerate(rest) if _ERROR.search(line)), 0)
    problems = problems or ["(no gate problem lines; see the errors below)"]
    return ["The gates failed on:", *problems, "", "First error lines from CI:", *rest[start:]]


def _findings(blocking: list[tuple[Reviewer, Finding]]) -> list[str]:
    """Every blocking finding with the reviewer that gave it; nits stay out."""
    return [f"- {finding.file}:{finding.line}: {finding.message} ({name})" for name, finding in blocking]


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
