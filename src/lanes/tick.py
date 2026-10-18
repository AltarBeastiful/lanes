"""`lanes tick`: the control table. Per lane, read the facts, pick one action, act, and move on."""

import re
from collections.abc import Callable
from functools import cache

from lanes.agents import AgentError, Agents
from lanes.hosts import HostAdapter, HostError
from lanes.models import FixRound, PullRequest, Session
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


def run(host: HostAdapter, store: LaneStore, agents: Agents) -> list[str]:
    """Act at most once on each lane, and say what was done, one line per lane acted on."""
    rounds: dict[str, list[FixRound]] = {}
    for event in store.events():
        if isinstance(event, FixRound):
            rounds.setdefault(event.ticket, []).append(event)
    lanes: dict[str, PullRequest] = {}
    # One lane is one pull request; should two close the same ticket, the older one is the lane.
    for pr in sorted(host.pull_requests(), key=lambda pr: int(pr.ref)):
        lanes.setdefault(pr.ticket, pr)
    sessions = cache(agents.sessions)
    said: list[str] = []
    for ticket, pr in lanes.items():
        try:
            done = _act(pr, rounds.get(ticket, []), host, store, agents, sessions)
        except (AgentError, HostError) as error:
            done = str(error)
        if done:
            said.append(f"T{ticket}: {done}")
    return said


def _act(
    pr: PullRequest,
    spent: list[FixRound],
    host: HostAdapter,
    store: LaneStore,
    agents: Agents,
    sessions: Callable[[], list[Session]],
) -> str | None:
    """The control table: the first row the lane's facts match picks its one action."""
    if pr.ci != "failed" or NEEDS_HUMAN in pr.labels:
        return None
    if any(round.commit == pr.head for round in spent):
        # A round already spent on this commit is still being worked on: the fix isn't pushed yet.
        return None
    if len(spent) >= MAX_FIX_ROUNDS:
        host.label_pull_request(pr.ref, NEEDS_HUMAN)
        return f"CI is still red after {MAX_FIX_ROUNDS} fix rounds, labelled #{pr.ref} {NEEDS_HUMAN}"
    session = _session(pr.ticket, store, sessions())
    if session is None:
        return f"CI is red on #{pr.ref}, but no session named T{pr.ticket} is left to resume"
    resumed = agents.resume(session, _red_prompt(pr, host.ci_log(pr.ref), len(spent) + 1), f"T{pr.ticket}")
    store.remember_session(pr.ticket, resumed)
    store.append(FixRound(ticket=pr.ticket, commit=pr.head))
    return f"CI is red on #{pr.ref}, resumed {resumed[:8]} with its failures (fix round {len(spent) + 1} of {MAX_FIX_ROUNDS})"


def _session(ticket: str, store: LaneStore, sessions: list[Session]) -> Session | None:
    """The session the cache maps the lane to, else the newest one named after it."""
    cached = store.session(ticket)
    for session in sessions:
        if session.session_id == cached:
            return session
    named = [session for session in sessions if re.fullmatch(rf"T{ticket}(\s.*)?", session.name)]
    return max(named, key=lambda session: session.started_at, default=None)


def _red_prompt(pr: PullRequest, log: str, round: int) -> str:
    """The gates' problems, then CI's first error lines, cut to fit in `MAX_PROMPT_LINES`."""
    lines = log.splitlines()
    problems = [line for line in lines if _GATE_PROBLEM.match(line)]
    rest = [line for line in lines if not _GATE_PROBLEM.match(line)]
    start = next((i for i, line in enumerate(rest) if _ERROR.search(line)), 0)
    head = [
        f"CI is red on pull request #{pr.ref} at {pr.head[:7]}. This is fix round {round} of {MAX_FIX_ROUNDS}.",
        "",
        "The gates failed on:",
    ]
    middle = ["", "First error lines from CI:"]
    tail = ["", "Fix them, run the affected tests, then commit and push. Stop once pushed."]
    room = MAX_PROMPT_LINES - len(head) - len(middle) - len(tail)
    problems = problems[:room] or ["(no gate problem lines; see the errors below)"]
    errors = rest[start:][: max(room - len(problems), 0)]
    return "\n".join(head + problems + middle + errors + tail)
