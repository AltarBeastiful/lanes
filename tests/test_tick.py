"""Seam 1: `lanes tick` through the CLI, against a fake `gh`, `claude` and `codex` on PATH and a real `LaneStore`.

The fakes read the world from a JSON file the test writes, and log every call they get, so a test sets up pull
requests, CI logs, tickets, sessions and reviewers' verdicts, ticks, and reads back what Lanes did to them.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from lanes.cli import main
from lanes.models import Verdict

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_WORLD"]) as file:
    world = json.load(file)
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\\n")
if args[:2] == ["repo", "view"]:
    print("owner/repo")
elif args[:2] == ["pr", "list"]:
    print(json.dumps(world["prs"]))
elif args[:2] == ["pr", "view"]:
    print(json.dumps(next(pr for pr in world["prs"] if str(pr["number"]) == args[2])))
elif args[:2] == ["pr", "diff"]:
    print(world["diffs"][args[2]], end="")
elif args[:2] == ["issue", "view"]:
    print(json.dumps({"body": world["tickets"][args[2]]}))
elif args[:2] == ["run", "view"]:
    print(world["jobs"][args[args.index("--job") + 1]], end="")
elif args[:2] == ["api", "user"]:
    print(json.dumps({"login": "me"}))
elif args[:2] == ["pr", "update-branch"]:
    branch = world.get("branches", {}).get(args[2], "current")
    if branch == "conflict":
        print("X Cannot update PR branch due to conflicts", file=sys.stderr)
        sys.exit(1)
    print("✓ PR branch updated" if branch == "behind" else "✓ PR branch already up-to-date")
"""

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
path = os.environ["FAKE_WORLD"]
with open(path) as file:
    world = json.load(file)
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\\n")
if args[:1] == ["agents"]:
    print(json.dumps(world["sessions"]))
    sys.exit(0)
if "-p" in args:
    reviewer = "claude-adversarial" if "--append-system-prompt" in args else "claude-" + args[args.index("--effort") + 1]
    schema = json.loads(args[args.index("--json-schema") + 1])
    with open(os.environ["FAKE_REVIEW_LOG"], "a") as log:
        log.write(json.dumps({"reviewer": reviewer, "args": args, "schema": schema, "prompt": sys.stdin.read(),
                              "cwd": os.getcwd()}) + "\\n")
    verdict = world["verdicts"].get(reviewer, {"blocking": [], "nits": []})
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "",
                      "structured_output": verdict}))
    sys.exit(0)
name = args[args.index("--name") + 1]
if "--resume" in args:
    resumed = args[args.index("--resume") + 1]
    session = next(s for s in world["sessions"] if s["sessionId"] == resumed)
    copy = world.get("copies", {}).get(resumed)
    if copy:
        world["sessions"].append({**session, "id": copy[:8], "sessionId": copy, "startedAt": session["startedAt"] + 1})
        print(f"note: session {session['id']} is already running in the background, so this started a copy as {copy[:8]}.")
        session = world["sessions"][-1]
    session["state"] = "working"
else:
    session = {"id": "dddddddd", "sessionId": "dddddddd-0000-0000-0000-000000000000", "name": name, "state": "working",
               "cwd": os.getcwd(), "kind": "background", "startedAt": 1}
    world["sessions"].append(session)
with open(path, "w") as file:
    json.dump(world, file)
print(f"backgrounded · {session['id']} · {name}")
"""

FAKE_CODEX = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_WORLD"]) as file:
    world = json.load(file)
with open(args[args.index("--output-schema") + 1]) as file:
    schema = json.load(file)
with open(os.environ["FAKE_REVIEW_LOG"], "a") as log:
    log.write(json.dumps({"reviewer": "codex", "args": args, "schema": schema, "prompt": sys.stdin.read(),
                          "cwd": os.getcwd()}) + "\\n")
with open(args[args.index("--output-last-message") + 1], "w") as file:
    json.dump(world["verdicts"].get("codex", {"blocking": [], "nits": []}), file)
print("tokens used: 1234")
"""

TICKET = """\
## What to build

The mean of a list rounds to two places.

## Lanes

```toml
route = "direct"
tier = "{tier}"
produces = []
consumes = []
seams = ["cli"]
demo = "mean([1, 2]) is 1.5"
```
"""

DIFF = """\
diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def mean(values):
-    return sum(values) / len(values)
+    return round(sum(values) / len(values), 2)
"""

STANDARDS = "# Coding standards\n\nPrefer the standard library.\n"

LANES_TOML = """\
host = "github"
tracker = "github"
test_command = "pytest"
max_lanes = 2
codex_allowed = false

[reviewers]
trivial = []
small = []
medium = []
high = []
"""

SESSION = "aaaaaaaa-1111-2222-3333-444444444444"
COPY = "cccccccc-5555-6666-7777-888888888888"


class World:
    """The fake host and agent view: what `gh` and `claude` answer, and every call they were made."""

    def __init__(self, root: Path, repo: Path) -> None:
        self._file = root / "world.json"
        self._gh_log = root / "gh.log"
        self._claude_log = root / "claude.log"
        self._review_log = root / "review.log"
        self.repo = repo
        self.worktree = root / "worktrees" / "T9"
        self.worktree.mkdir(parents=True)
        self.save({
            "prs": [],
            "jobs": {},
            "diffs": {},
            "tickets": {"9": TICKET.format(tier="medium")},
            "verdicts": {},
            "sessions": [
                {"id": SESSION[:8], "sessionId": SESSION, "name": "T9", "state": "done", "cwd": str(self.worktree),
                 "kind": "background", "startedAt": 1000},
            ],
        })

    def load(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self._file.read_text())
        return loaded

    def save(self, world: dict[str, Any]) -> None:
        self._file.write_text(json.dumps(world))

    def red_pr(self, number: int = 31, ticket: int = 9, head: str = "c0ffee1", log: str = "") -> None:
        """An open pull request closing `ticket`, whose `gates` run failed on `head` with `log`."""
        world = self.load()
        world["prs"] = [pr for pr in world["prs"] if pr["number"] != number]
        job = str(1000 + len(world["jobs"]))
        world["jobs"][job] = log
        world["prs"].append(pr_json(number, ticket, head, [check("FAILURE", job)]))
        self.save(world)

    def green_pr(self, number: int = 31, ticket: int = 9, head: str = "c0ffee1", tier: str = "medium",
                 log: str = "", diff: str = DIFF) -> None:
        """An open pull request closing `ticket` of `tier`, whose `gates` run passed on `head` with `log`."""
        world = self.load()
        world["prs"] = [pr for pr in world["prs"] if pr["number"] != number]
        job = str(1000 + len(world["jobs"]))
        world["jobs"][job] = log
        world["prs"].append(pr_json(number, ticket, head, [check("SUCCESS", job)]))
        world["tickets"][str(ticket)] = TICKET.format(tier=tier)
        world["diffs"][str(number)] = diff
        self.save(world)

    def configure(self, codex_allowed: bool = True, **reviewers: list[str]) -> None:
        """Write `.lanes/lanes.toml` with `reviewers` per tier; the tiers not given get none."""
        tiers = {tier: reviewers.get(tier, []) for tier in ("trivial", "small", "medium", "high")}
        (self.repo / ".lanes" / "lanes.toml").write_text(
            LANES_TOML.replace("codex_allowed = false", f"codex_allowed = {str(codex_allowed).lower()}").split("[reviewers]")[0]
            + "[reviewers]\n" + "".join(f"{tier} = {json.dumps(names)}\n" for tier, names in tiers.items())
        )

    def verdict(self, reviewer: str, blocking: list[dict[str, Any]] = [], nits: list[str] = []) -> None:
        """What `reviewer` answers from now on."""
        world = self.load()
        world["verdicts"][reviewer] = {"blocking": blocking, "nits": nits}
        self.save(world)

    def reviews(self) -> list[dict[str, Any]]:
        """Each reviewer run: who reviewed, its arguments, the schema it was given, its prompt and working directory."""
        return [json.loads(line) for line in self._review_log.read_text().splitlines()] if self._review_log.exists() else []

    def branch(self, number: int, state: str) -> None:
        """Where pull request `number`'s branch stands against `main`: behind, conflict or current."""
        world = self.load()
        world.setdefault("branches", {})[str(number)] = state
        self.save(world)

    def copy_on_resume(self, session: str, copy: str) -> None:
        world = self.load()
        world.setdefault("copies", {})[session] = copy
        self.save(world)

    def gh_calls(self) -> list[list[str]]:
        return [call["args"] for call in self._gh_runs()]

    def gh_cwds(self) -> set[str]:
        """Every working directory `gh` was run from."""
        return {call["cwd"] for call in self._gh_runs()}

    def _gh_runs(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self._gh_log.read_text().splitlines()] if self._gh_log.exists() else []

    def agent_calls(self) -> list[list[str]]:
        """Every `claude` call but listing sessions, and every reviewer run."""
        calls = [json.loads(line)["args"] for line in self._claude_log.read_text().splitlines()] if self._claude_log.exists() else []
        return [args for args in calls if args[:1] != ["agents"]] + [review["args"] for review in self.reviews()]

    def resumes(self) -> list[dict[str, Any]]:
        """Each `claude --resume`: the session resumed, its name, prompt and working directory."""
        calls = [json.loads(line) for line in self._claude_log.read_text().splitlines()] if self._claude_log.exists() else []
        found = []
        for call in calls:
            args = call["args"]
            if "--resume" in args:
                found.append({
                    "session": args[args.index("--resume") + 1],
                    "name": args[args.index("--name") + 1] if "--name" in args else None,
                    "prompt": args[-1],
                    "cwd": call["cwd"],
                })
        return found


def check(conclusion: str, job: str, started: str = "2026-10-18T17:00:00Z") -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": "gates",
        "workflowName": "gates",
        "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "conclusion": conclusion,
        "startedAt": started,
        "detailsUrl": f"https://github.com/owner/repo/actions/runs/1/job/{job}",
    }


def pr_json(number: int, ticket: int, head: str, checks: list[dict[str, Any]], labels: list[str] = []) -> dict[str, Any]:
    """A draft, as a lane opens it, written by `me`: the account Lanes and its sessions run `gh` as."""
    return {
        "number": number,
        "headRefName": f"lane-{ticket}",
        "isDraft": True,
        "author": {"login": "me"},
        "body": f"Closes #{ticket}\n\n## Summary\n\nSomething.\n",
        "headRefOid": head,
        "labels": [{"name": label} for label in labels],
        "statusCheckRollup": checks,
    }


def github_log(lines: list[str]) -> str:
    """Lines as `gh run view --log-failed` prints them: job, step and timestamp before each."""
    header = ["##[group]Run uv run python .lanes/gates.py", "env:", "  BODY: Closes #9", "##[endgroup]"]
    return "".join(f"gates\tgates\t2026-10-18T17:48:00.0000000Z {line}\n" for line in header + lines)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    repo = tmp_path / "repo"
    (repo / ".lanes").mkdir(parents=True)
    (repo / ".lanes" / "lanes.toml").write_text(LANES_TOML)
    (repo / "CODING_STANDARDS.md").write_text(STANDARDS)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    bin = tmp_path / "bin"
    bin.mkdir()
    for name, script in (("gh", FAKE_GH), ("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
        (bin / name).write_text(script)
        (bin / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_WORLD", str(tmp_path / "world.json"))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    monkeypatch.setenv("FAKE_REVIEW_LOG", str(tmp_path / "review.log"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(repo)
    return World(tmp_path, repo)


def tick() -> Result:
    return CliRunner().invoke(main, ["tick"])


def logged(world: World) -> list[dict[str, Any]]:
    state = world.repo.parent / "state"
    return [json.loads(line) for log in state.glob("lanes/*/events.jsonl") for line in log.read_text().splitlines()]


def test_a_red_lane_is_resumed_once_with_its_failing_tests_in_at_most_200_lines(world: World) -> None:
    noise = [f"tests/test_calc.py:{n}: in test_add" for n in range(1000)]
    problems = ["new failure: tests/test_calc.py::test_add", "new failure: tests/test_calc.py::test_mean"]
    world.red_pr(log=github_log(["=== FAILURES ===", "E   assert 3 == 4", *noise, *problems]))

    result = tick()

    assert result.exit_code == 0, result.output
    [resume] = world.resumes()
    assert resume["session"] == SESSION
    assert resume["name"] == "T9"
    assert resume["cwd"] == str(world.worktree)
    prompt = resume["prompt"]
    assert len(prompt.splitlines()) <= 200
    assert "tests/test_calc.py::test_add" in prompt
    assert "tests/test_calc.py::test_mean" in prompt
    assert "E   assert 3 == 4" in prompt
    assert "BODY: Closes #9" not in prompt
    [event] = logged(world)
    assert (event["event"], event["ticket"], event["commit"]) == ("fix_round", "9", "c0ffee1")


def test_a_resume_that_starts_a_copy_remaps_the_lane_to_the_copy(world: World) -> None:
    world.red_pr(head="c0ffee1", log=github_log(["new failure: tests/test_calc.py::test_add"]))
    world.copy_on_resume(SESSION, COPY)
    first = tick()
    # Nothing says a copy keeps its name, so only the lane's own record can lead the next tick to it.
    unnamed = world.load()
    for session in unnamed["sessions"]:
        if session["sessionId"] == COPY:
            session["name"] = ""
    world.save(unnamed)
    world.red_pr(head="c0ffee2", log=github_log(["new failure: tests/test_calc.py::test_mean"]))

    second = tick()

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert [resume["session"] for resume in world.resumes()] == [SESSION, COPY]
    assert all(resume["name"] == "T9" for resume in world.resumes())


def test_a_red_commit_already_resumed_is_left_to_the_session(world: World) -> None:
    world.red_pr(head="c0ffee1", log=github_log(["new failure: tests/test_calc.py::test_add"]))

    tick()
    result = tick()

    assert result.exit_code == 0, result.output
    assert len(world.resumes()) == 1
    assert len(logged(world)) == 1


def test_after_two_fix_rounds_the_pull_request_needs_a_human_and_is_not_resumed(world: World) -> None:
    for head in ("c0ffee1", "c0ffee2"):
        world.red_pr(head=head, log=github_log(["new failure: tests/test_calc.py::test_add"]))
        tick()
    world.red_pr(head="c0ffee3", log=github_log(["new failure: tests/test_calc.py::test_add"]))

    result = tick()

    assert result.exit_code == 0, result.output
    # The one resume after the rounds asks a person; no third fix round is spent.
    assert all("This is fix round" not in resume["prompt"] for resume in world.resumes()[2:])
    assert [event["commit"] for event in logged(world) if event["event"] == "fix_round"] == ["c0ffee1", "c0ffee2"]
    assert ["pr", "edit", "31", "--repo", "owner/repo", "--add-label", "needs-human"] in world.gh_calls()
    assert "needs-human" in result.output


def test_after_two_fix_rounds_the_pull_request_needs_a_human_who_is_asked_with_the_failures(world: World) -> None:
    for head in ("c0ffee1", "c0ffee2"):
        world.red_pr(head=head, log=github_log(["new failure: tests/test_calc.py::test_add"]))
        tick()
    world.red_pr(head="c0ffee3", log=github_log(["E   assert 2 == 3", "new failure: tests/test_calc.py::test_mean"]))

    result = tick()

    assert result.exit_code == 0, result.output
    [*_, ask] = world.resumes()
    assert len(world.resumes()) == 3
    assert "AskUserQuestion" in ask["prompt"]
    assert "CI is red on #31 after 2 fix rounds" in ask["prompt"]
    assert "tests/test_calc.py::test_mean" in ask["prompt"] and "E   assert 2 == 3" in ask["prompt"]
    assert [(event["event"], event["commit"]) for event in logged(world)] == [
        ("fix_round", "c0ffee1"), ("fix_round", "c0ffee2"), ("human_asked", "c0ffee3")
    ]
    assert ["pr", "edit", "31", "--repo", "owner/repo", "--add-label", "needs-human"] in world.gh_calls()
    assert "needs-human" in result.output and "asked you" in result.output


def test_a_lane_that_needs_a_human_is_left_alone(world: World) -> None:
    world.red_pr(log=github_log(["new failure: tests/test_calc.py::test_add"]))
    labelled = world.load()
    labelled["prs"][0]["labels"] = [{"name": "needs-human"}]
    world.save(labelled)
    tick()

    result = tick()

    assert result.exit_code == 0, result.output
    # Only the one question: no fix round, and no label added or taken off.
    assert len(world.resumes()) == 1
    assert [event["event"] for event in logged(world)] == ["human_asked"]
    assert not [call for call in world.gh_calls() if call[:2] == ["pr", "edit"]]


def test_a_lane_labelled_needs_human_asks_me_once_with_its_failures_and_is_then_left_alone(world: World) -> None:
    world.red_pr(head="c0ffee1", log=github_log(["new failure: tests/test_calc.py::test_add"]))
    labelled = world.load()
    labelled["prs"][0]["labels"] = [{"name": "needs-human"}]
    world.save(labelled)

    asked = tick()
    again = tick()
    # Whoever answers may push with the label still on: the lane has asked already.
    pushed = world.load()
    pushed["prs"][0]["headRefOid"] = "c0ffee2"
    world.save(pushed)
    after_push = tick()

    assert asked.exit_code == 0, asked.output
    [ask] = world.resumes()
    assert (ask["session"], ask["name"]) == (SESSION, "T9")
    assert "AskUserQuestion" in ask["prompt"]
    assert "labelled needs-human" in ask["prompt"]
    assert "tests/test_calc.py::test_add" in ask["prompt"]
    assert "T9" in asked.output and "asked you" in asked.output
    assert [(event["event"], event["why"]) for event in logged(world)] == [("human_asked", "needs_human")]
    assert "T9" not in again.output and "T9" not in after_push.output
    assert not [call for call in world.gh_calls() if call[:2] == ["pr", "edit"]]


@pytest.mark.parametrize(
    "checks",
    [
        [check("SUCCESS", "1")],
        [check("", "1")],
        [],
        [check("FAILURE", "1", started="2026-10-18T17:00:00Z"), check("SUCCESS", "2", started="2026-10-18T17:30:00Z")],
    ],
    ids=["green", "running", "no-checks-yet", "failed-then-passed-on-rerun"],
)
def test_a_lane_whose_ci_is_not_red_is_not_resumed(world: World, checks: list[dict[str, Any]]) -> None:
    lanes = world.load()
    lanes["prs"] = [pr_json(31, 9, "c0ffee1", checks)]
    world.save(lanes)

    result = tick()

    assert result.exit_code == 0, result.output
    assert world.resumes() == []
    assert logged(world) == []


def test_a_lane_is_acted_on_at_most_once_per_tick(world: World) -> None:
    world.red_pr(number=31, log=github_log(["new failure: tests/test_calc.py::test_add"]))
    world.red_pr(number=32, log=github_log(["new failure: tests/test_calc.py::test_add"]))

    result = tick()

    assert result.exit_code == 0, result.output
    assert len(world.resumes()) == 1
    assert len(logged(world)) == 1


def test_a_red_lane_without_a_session_is_reported_and_no_round_is_spent(world: World) -> None:
    world.red_pr(log=github_log(["new failure: tests/test_calc.py::test_add"]))
    gone = world.load()
    gone["sessions"] = []
    world.save(gone)

    result = tick()

    assert result.exit_code == 0, result.output
    assert "T9" in result.output and "no session" in result.output
    assert logged(world) == []


def test_a_pull_request_that_closes_no_ticket_is_not_a_lane(world: World) -> None:
    world.red_pr(log=github_log(["new failure: tests/test_calc.py::test_add"]))
    unanchored = world.load()
    unanchored["prs"][0]["body"] = "Bumps a dependency.\n"
    world.save(unanchored)

    result = tick()

    assert result.exit_code == 0, result.output
    assert world.resumes() == []


def test_a_green_lane_without_a_verdict_gets_exactly_its_tiers_reviewers(world: World) -> None:
    world.configure(small=["codex"], medium=["codex", "claude-high"], high=["codex", "claude-max"])
    world.green_pr(head="c0ffee1", tier="medium")

    result = tick()

    assert result.exit_code == 0, result.output
    assert sorted(review["reviewer"] for review in world.reviews()) == ["claude-high", "codex"]
    assert all(review["cwd"] == str(world.worktree) for review in world.reviews())
    assert world.resumes() == []
    assert sorted((event["event"], event["reviewer"], event["commit"]) for event in logged(world)) == [
        ("verdict", "claude-high", "c0ffee1"),
        ("verdict", "codex", "c0ffee1"),
    ]


def test_a_commit_is_reviewed_once_and_a_new_commit_again(world: World) -> None:
    world.configure(medium=["codex", "claude-high"])
    world.green_pr(head="c0ffee1")

    tick()
    again = tick()
    world.green_pr(head="c0ffee2")
    pushed = tick()

    assert again.exit_code == 0, again.output
    assert pushed.exit_code == 0, pushed.output
    assert "reviewed" not in again.output
    assert sorted((event["commit"], event["reviewer"]) for event in logged(world)) == [
        ("c0ffee1", "claude-high"),
        ("c0ffee1", "codex"),
        ("c0ffee2", "claude-high"),
        ("c0ffee2", "codex"),
    ]
    assert len(world.reviews()) == 4


def test_a_reviewer_added_to_the_tier_reviews_only_what_it_has_not(world: World) -> None:
    world.configure(medium=["codex"])
    world.green_pr(head="c0ffee1")
    tick()
    world.configure(medium=["codex", "claude-high"])

    tick()

    assert [review["reviewer"] for review in world.reviews()] == ["codex", "claude-high"]


def test_a_blocking_finding_resumes_the_lane_with_the_findings(world: World) -> None:
    world.configure(medium=["codex", "claude-high"])
    world.verdict("codex", blocking=[{"file": "calc.py", "line": 3, "message": "mean([]) divides by zero"}],
                  nits=["name the rounding"])
    world.verdict("claude-high", nits=["a docstring would help"])
    world.green_pr(head="c0ffee1")

    result = tick()
    again = tick()

    assert result.exit_code == 0, result.output
    [resume] = world.resumes()
    assert (resume["session"], resume["name"]) == (SESSION, "T9")
    assert "calc.py:3: mean([]) divides by zero" in resume["prompt"]
    assert "a docstring would help" not in resume["prompt"]
    assert "fix round 1" in result.output
    assert [event["commit"] for event in logged(world) if event["event"] == "fix_round"] == ["c0ffee1"]
    assert again.exit_code == 0, again.output
    assert len(world.resumes()) == 1
    assert len(world.reviews()) == 2


def test_a_verdict_with_only_nits_does_not_resume_the_lane(world: World) -> None:
    world.configure(medium=["codex", "claude-high"])
    world.verdict("codex", nits=["name the rounding"])
    world.verdict("claude-high", nits=["a docstring would help"])
    world.green_pr(head="c0ffee1")

    result = tick()

    assert result.exit_code == 0, result.output
    assert world.resumes() == []
    assert [event["event"] for event in logged(world)] == ["verdict", "verdict"]
    assert "nothing blocks" in result.output


def test_blocking_findings_after_two_fix_rounds_need_a_human(world: World) -> None:
    world.configure(medium=["codex"])
    for head in ("c0ffee1", "c0ffee2"):
        world.red_pr(head=head, log=github_log(["new failure: tests/test_calc.py::test_add"]))
        tick()
    world.verdict("codex", blocking=[{"file": "calc.py", "line": 3, "message": "mean([]) divides by zero"}])
    world.green_pr(head="c0ffee3")

    result = tick()

    assert result.exit_code == 0, result.output
    [*_, ask] = world.resumes()
    assert len(world.resumes()) == 3
    assert "Reviewers block on #31 after 2 fix rounds" in ask["prompt"]
    assert "calc.py:3: mean([]) divides by zero (codex)" in ask["prompt"]
    assert ["pr", "edit", "31", "--repo", "owner/repo", "--add-label", "needs-human"] in world.gh_calls()


def test_reviewers_get_the_diff_ticket_ci_log_and_standards_and_no_transcript(world: World) -> None:
    world.configure(medium=["codex", "claude-high"])
    world.green_pr(head="c0ffee1", log=github_log(["suite inconclusive at base: tests/test_calc.py"]))

    result = tick()

    assert result.exit_code == 0, result.output
    assert len(world.reviews()) == 2
    for review in world.reviews():
        prompt = review["prompt"]
        assert "+    return round(sum(values) / len(values), 2)" in prompt
        assert "The mean of a list rounds to two places." in prompt
        assert "suite inconclusive at base: tests/test_calc.py" in prompt
        assert "Prefer the standard library." in prompt
        # A fresh session, never the lane's own: nothing it said or did reaches the reviewer.
        assert SESSION not in prompt and SESSION not in review["args"]
        assert not {"--resume", "--continue", "-c", "resume"} & set(review["args"])


def test_with_codex_disallowed_the_second_reviewer_is_claude_with_the_adversarial_prompt(world: World) -> None:
    world.configure(codex_allowed=False, medium=["claude-adversarial", "claude-high"])
    world.green_pr(head="c0ffee1")

    result = tick()

    assert result.exit_code == 0, result.output
    assert sorted(review["reviewer"] for review in world.reviews()) == ["claude-adversarial", "claude-high"]
    [adversarial] = [review for review in world.reviews() if review["reviewer"] == "claude-adversarial"]
    args = adversarial["args"]
    assert args[args.index("--agent") + 1] == "reviewer"
    assert "wrong" in args[args.index("--append-system-prompt") + 1]


def test_every_reviewer_fills_the_schema_generated_from_the_verdict_model(world: World) -> None:
    world.configure(medium=["codex", "claude-high"])
    world.green_pr(head="c0ffee1")

    tick()

    for review in world.reviews():
        assert review["schema"] == Verdict.model_json_schema()
        assert set(review["schema"]["required"]) == {"blocking", "nits"}


def test_a_green_lane_without_a_session_is_reported_and_not_reviewed(world: World) -> None:
    world.configure(medium=["codex"])
    world.green_pr(head="c0ffee1")
    gone = world.load()
    gone["sessions"] = []
    world.save(gone)

    result = tick()

    assert result.exit_code == 0, result.output
    assert "T9" in result.output and "no session" in result.output
    assert world.reviews() == []


def test_a_reviewer_that_returns_no_verdict_is_reported_and_reviews_again_next_tick(world: World) -> None:
    world.configure(medium=["claude-high", "codex"])
    broken = world.load()
    broken["verdicts"]["codex"] = "looks fine to me"
    world.save(broken)
    world.green_pr(head="c0ffee1")

    result = tick()
    world.verdict("codex")
    again = tick()

    assert result.exit_code == 0, result.output
    assert "T9" in result.output and "codex returned no verdict" in result.output
    assert again.exit_code == 0, again.output
    assert [review["reviewer"] for review in world.reviews()] == ["claude-high", "codex", "codex"]
    assert [event["reviewer"] for event in logged(world)] == ["claude-high", "codex"]


def test_codex_is_handed_the_reviewer_agents_instructions(world: World) -> None:
    world.configure(medium=["codex"])
    agents = world.worktree / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text("---\nname: reviewer\ntools: Read\n---\n\nA finding blocks only when merging is wrong.\n")
    world.green_pr(head="c0ffee1")

    tick()

    [review] = world.reviews()
    assert review["prompt"].startswith("A finding blocks only when merging is wrong.")
    assert "The mean of a list rounds to two places." in review["prompt"]


def test_a_clean_lane_behind_main_is_updated_without_any_agent_call(world: World) -> None:
    world.green_pr(head="c0ffee1")
    world.branch(31, "behind")

    result = tick()

    assert result.exit_code == 0, result.output
    assert ["pr", "update-branch", "31", "--repo", "owner/repo"] in world.gh_calls()
    assert world.agent_calls() == []
    assert not [call for call in world.gh_calls() if call[:2] == ["pr", "ready"]]
    assert "T9" in result.output and "updated" in result.output


def test_a_conflict_resumes_the_lane_once_to_rebase_and_a_second_needs_a_human(world: World) -> None:
    world.green_pr(head="c0ffee1")
    world.branch(31, "conflict")

    first = tick()
    again = tick()
    world.green_pr(head="c0ffee2")
    still = tick()

    assert first.exit_code == 0, first.output
    [resume, _] = world.resumes()
    assert (resume["session"], resume["name"], resume["cwd"]) == (SESSION, "T9", str(world.worktree))
    assert "rebase on origin/main" in resume["prompt"]
    assert again.exit_code == 0, again.output
    assert "T9" not in again.output
    assert still.exit_code == 0, still.output
    assert ["pr", "edit", "31", "--repo", "owner/repo", "--add-label", "needs-human"] in world.gh_calls()
    assert "needs-human" in still.output
    assert [event["event"] for event in logged(world)] == ["rebase_requested", "human_asked"]


def test_a_conflict_after_the_rebase_asks_me_once_through_the_lanes_session(world: World) -> None:
    world.green_pr(head="c0ffee1")
    world.branch(31, "conflict")
    tick()
    world.green_pr(head="c0ffee2")

    asked = tick()
    labelled = world.load()
    labelled["prs"][0]["labels"] = [{"name": "needs-human"}]
    world.save(labelled)
    again = tick()

    assert asked.exit_code == 0, asked.output
    [_, ask] = world.resumes()
    assert (ask["session"], ask["name"], ask["cwd"]) == (SESSION, "T9", str(world.worktree))
    assert "AskUserQuestion" in ask["prompt"]
    assert "still conflicts with main" in ask["prompt"] and "c0ffee2" in ask["prompt"]
    assert "T9" in asked.output and "asked you" in asked.output
    assert again.exit_code == 0, again.output
    assert len(world.resumes()) == 2
    assert "T9" not in again.output


@pytest.mark.parametrize(
    ("author", "asked"),
    [("someone", ["--add-reviewer", "me"]), ("me", ["--add-assignee", "@me"])],
    ids=["by-someone-else", "by-me-who-cannot-review-my-own"],
)
def test_an_up_to_date_clean_lane_is_marked_ready_and_my_review_asked_for(
    world: World, author: str, asked: list[str]
) -> None:
    world.configure(medium=["codex"])
    world.green_pr(head="c0ffee1")
    written = world.load()
    written["prs"][0]["author"] = {"login": author}
    world.save(written)

    reviewed = tick()
    landed = tick()
    ready = world.load()
    ready["prs"][0]["isDraft"] = False
    world.save(ready)
    again = tick()

    assert reviewed.exit_code == 0, reviewed.output
    assert landed.exit_code == 0, landed.output
    assert ["pr", "update-branch", "31", "--repo", "owner/repo"] in world.gh_calls()
    assert [call for call in world.gh_calls() if call[:2] == ["pr", "ready"]] == [["pr", "ready", "31", "--repo", "owner/repo"]]
    assert ["pr", "edit", "31", "--repo", "owner/repo", *asked] in world.gh_calls()
    assert "T9" in landed.output and "ready" in landed.output
    assert again.exit_code == 0, again.output
    assert "T9" not in again.output
    assert world.resumes() == []


@pytest.mark.parametrize("ci", ["pending", "red"])
def test_a_failed_session_is_resumed_with_continue_and_nothing_else(world: World, ci: str) -> None:
    world.red_pr(log=github_log(["new failure: tests/test_calc.py::test_add"]))
    if ci == "pending":
        pending = world.load()
        pending["prs"][0]["statusCheckRollup"] = []
        world.save(pending)
    rebooted = world.load()
    rebooted["sessions"][0]["state"] = "failed"
    world.save(rebooted)

    result = tick()

    assert result.exit_code == 0, result.output
    [resume] = world.resumes()
    assert (resume["session"], resume["name"], resume["prompt"]) == (SESSION, "T9", "continue")
    assert logged(world) == []
    assert "T9" in result.output and "continue" in result.output


def test_with_the_state_directory_deleted_the_next_tick_finds_every_open_lane(world: World) -> None:
    # T9's session continued in a copy that kept no name: only its worktree, on the lane's branch, leads to it.
    subprocess.run(["git", "init", "-q", "-b", "lane-9", str(world.worktree)], check=True)
    world.red_pr(number=31, ticket=9, head="c0ffee1", log=github_log(["new failure: tests/test_calc.py::test_add"]))
    world.copy_on_resume(SESSION, COPY)
    tick()
    unnamed = world.load()
    for session in unnamed["sessions"]:
        if session["sessionId"] == COPY:
            session["name"] = ""
    other = world.worktree.parent / "T12"
    other.mkdir()
    unnamed["sessions"].append({"id": "bbbbbbbb", "sessionId": "bbbbbbbb-1111-2222-3333-444444444444", "name": "T12",
                                "state": "done", "cwd": str(other), "kind": "background", "startedAt": 2000})
    world.save(unnamed)
    shutil.rmtree(world.repo.parent / "state")
    world.red_pr(number=31, ticket=9, head="c0ffee2", log=github_log(["new failure: tests/test_calc.py::test_mean"]))
    world.red_pr(number=32, ticket=12, head="beef001", log=github_log(["new failure: tests/test_calc.py::test_add"]))

    result = tick()

    assert result.exit_code == 0, result.output
    assert [resume["session"] for resume in world.resumes()] == [
        SESSION, COPY, "bbbbbbbb-1111-2222-3333-444444444444"
    ]
    assert "T9" in result.output and "T12" in result.output


BLOCKED_ON = "needs: #7\nwhy: calc.median needs the Stats type ticket 7 adds.\n"


def test_a_blocked_lane_without_a_pull_request_asks_me_once_why_it_is_blocked(world: World) -> None:
    blocked = world.worktree.parent / "T12"
    blocked.mkdir()
    (blocked / ".lanes-blocked-on").write_text(BLOCKED_ON)
    sessions = world.load()
    sessions["sessions"].append({"id": "bbbbbbbb", "sessionId": "bbbbbbbb-1111-2222-3333-444444444444", "name": "T12",
                                 "state": "done", "cwd": str(blocked), "kind": "background", "startedAt": 2000})
    world.save(sessions)

    asked = tick()
    again = tick()

    assert asked.exit_code == 0, asked.output
    [ask] = world.resumes()
    assert (ask["session"], ask["name"], ask["cwd"]) == ("bbbbbbbb-1111-2222-3333-444444444444", "T12", str(blocked))
    assert "AskUserQuestion" in ask["prompt"]
    assert "blocked" in ask["prompt"] and "needs: #7" in ask["prompt"] and "Stats type" in ask["prompt"]
    assert "T12" in asked.output and "asked you" in asked.output
    assert [(event["event"], event["ticket"], event["why"], event["commit"]) for event in logged(world)] == [
        ("human_asked", "12", "blocked", None)
    ]
    assert again.exit_code == 0, again.output
    assert len(world.resumes()) == 1
    # T9 has a session but no pull request and no blocked-on file: there is nothing to act on.
    assert "T9" not in asked.output + again.output


def test_a_blocked_lane_with_a_red_pull_request_asks_me_instead_of_spending_a_fix_round(world: World) -> None:
    world.red_pr(head="c0ffee1", log=github_log(["new failure: tests/test_calc.py::test_add"]))
    (world.worktree / ".lanes-blocked-on").write_text(BLOCKED_ON)

    asked = tick()
    again = tick()

    assert asked.exit_code == 0, asked.output
    [ask] = world.resumes()
    assert "AskUserQuestion" in ask["prompt"] and "needs: #7" in ask["prompt"]
    assert [(event["event"], event["why"], event["commit"]) for event in logged(world)] == [
        ("human_asked", "blocked", "c0ffee1")
    ]
    assert again.exit_code == 0, again.output
    assert len(world.resumes()) == 1


def test_notify_cmd_runs_when_a_lane_asks_me_and_only_then(world: World) -> None:
    notified = world.repo.parent / "notified.txt"
    config = world.repo / ".lanes" / "lanes.toml"
    config.write_text(f"notify_cmd = 'echo \"$LANES_LANE|$LANES_MESSAGE\" >> {notified}'\n" + config.read_text())
    world.red_pr(head="c0ffee1", log=github_log(["new failure: tests/test_calc.py::test_add"]))

    resumed = tick()
    nothing_new = tick()
    labelled = world.load()
    labelled["prs"][0]["labels"] = [{"name": "needs-human"}]
    world.save(labelled)
    asked = tick()
    again = tick()

    assert resumed.exit_code == nothing_new.exit_code == asked.exit_code == again.exit_code == 0
    [line] = notified.read_text().splitlines()
    assert line == "T9|Lanes needs a person in lane T9: pull request #31 is labelled needs-human"


def test_a_failing_notify_cmd_is_reported_and_the_question_still_asked(world: World) -> None:
    config = world.repo / ".lanes" / "lanes.toml"
    config.write_text("notify_cmd = 'exit 3'\n" + config.read_text())
    world.red_pr(head="c0ffee1", log=github_log(["new failure: tests/test_calc.py::test_add"]))
    labelled = world.load()
    labelled["prs"][0]["labels"] = [{"name": "needs-human"}]
    world.save(labelled)

    result = tick()

    assert result.exit_code == 0, result.output
    assert len(world.resumes()) == 1
    assert "asked you" in result.output and "notify_cmd exited 3" in result.output


def register(world: World, *repos: Path) -> None:
    """Register `repos` in the user-level registry `lanes tick --all` walks, as `lanes init` does."""
    config_home = world.repo.parent / "config"
    (config_home / "lanes").mkdir(parents=True, exist_ok=True)
    (config_home / "lanes" / "repos.toml").write_text(f"repos = {json.dumps([str(repo) for repo in repos])}\n")


def another_repo(world: World, name: str) -> Path:
    """A second repository set up for Lanes like `world.repo`."""
    repo = world.repo.parent / name
    (repo / ".lanes").mkdir(parents=True)
    (repo / ".lanes" / "lanes.toml").write_text(LANES_TOML)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def tick_all() -> Result:
    return CliRunner().invoke(main, ["tick", "--all"])


def test_tick_all_ticks_every_registered_repository(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = another_repo(world, "other")
    register(world, world.repo, other)
    world.red_pr(log=github_log(["new failure: tests/test_calc.py::test_add"]))
    monkeypatch.chdir(tmp_path)

    result = tick_all()

    assert result.exit_code == 0, result.output
    assert world.gh_cwds() == {str(world.repo), str(other)}
    assert f"{world.repo}:" in result.output and f"{other}:" in result.output


@pytest.mark.parametrize("broken", ["gone", "unconfigured", "invalid"])
def test_one_failing_repository_does_not_stop_the_others(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    repo = tmp_path / broken
    if broken != "gone":
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if broken == "invalid":
        (repo / ".lanes").mkdir()
        (repo / ".lanes" / "lanes.toml").write_text('host = "gitlab"\n')
    register(world, repo, world.repo)
    world.red_pr(log=github_log(["new failure: tests/test_calc.py::test_add"]))
    monkeypatch.chdir(tmp_path)

    result = tick_all()

    assert result.exit_code == 1, result.output
    assert f"{repo}:\nlanes tick: " in result.output
    [resume] = world.resumes()
    assert resume["name"] == "T9"


@pytest.mark.parametrize("command", [["tick"], ["tick", "--all"]])
def test_a_tick_while_another_holds_the_repository_exits_without_acting(world: World, command: list[str]) -> None:
    # Imported here so that, on a base without the lock, only this test fails to run.
    from lanes import lock

    register(world, world.repo)
    world.red_pr(log=github_log(["new failure: tests/test_calc.py::test_add"]))

    with lock.exclusive(world.repo) as held:
        busy = CliRunner().invoke(main, command)
    calls_while_busy = world.gh_calls() + world.agent_calls()
    after = tick()

    assert held
    assert busy.exit_code == 0, busy.output
    assert "another tick is running" in busy.output
    assert calls_while_busy == []
    assert after.exit_code == 0, after.output
    assert len(world.resumes()) == 1
