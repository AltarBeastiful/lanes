"""Seam 1: `lanes tick` through the CLI, against a fake `gh` and a fake `claude` on PATH and a real `LaneStore`.

The fakes read the world from a JSON file the test writes, and log every call they get, so a test sets up pull
requests, CI logs and sessions, ticks, and reads back what Lanes did to them.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from lanes.cli import main

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_WORLD"]) as file:
    world = json.load(file)
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["repo", "view"]:
    print("owner/repo")
elif args[:2] == ["pr", "list"]:
    print(json.dumps(world["prs"]))
elif args[:2] == ["pr", "view"]:
    print(json.dumps(next(pr for pr in world["prs"] if str(pr["number"]) == args[2])))
elif args[:2] == ["run", "view"]:
    print(world["jobs"][args[args.index("--job") + 1]], end="")
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
name = args[args.index("--name") + 1]
if "--resume" in args:
    resumed = args[args.index("--resume") + 1]
    session = next(s for s in world["sessions"] if s["sessionId"] == resumed)
    copy = world.get("copies", {}).get(resumed)
    if copy:
        world["sessions"].append({**session, "id": copy[:8], "sessionId": copy, "startedAt": session["startedAt"] + 1})
        print(f"note: session {session['id']} is already running in the background, so this started a copy as {copy[:8]}.")
        session = world["sessions"][-1]
else:
    session = {"id": "dddddddd", "sessionId": "dddddddd-0000-0000-0000-000000000000", "name": name, "state": "working",
               "cwd": os.getcwd(), "kind": "background", "startedAt": 1}
    world["sessions"].append(session)
with open(path, "w") as file:
    json.dump(world, file)
print(f"backgrounded · {session['id']} · {name}")
"""

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
        self.repo = repo
        self.worktree = root / "worktrees" / "T9"
        self.worktree.mkdir(parents=True)
        self.save({
            "prs": [],
            "jobs": {},
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

    def copy_on_resume(self, session: str, copy: str) -> None:
        world = self.load()
        world.setdefault("copies", {})[session] = copy
        self.save(world)

    def gh_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self._gh_log.read_text().splitlines()] if self._gh_log.exists() else []

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
    return {
        "number": number,
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
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    bin = tmp_path / "bin"
    bin.mkdir()
    for name, script in (("gh", FAKE_GH), ("claude", FAKE_CLAUDE)):
        (bin / name).write_text(script)
        (bin / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_WORLD", str(tmp_path / "world.json"))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
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
    assert len(world.resumes()) == 2
    assert [event["commit"] for event in logged(world)] == ["c0ffee1", "c0ffee2"]
    assert ["pr", "edit", "31", "--repo", "owner/repo", "--add-label", "needs-human"] in world.gh_calls()
    assert "needs-human" in result.output


def test_a_lane_that_needs_a_human_is_left_alone(world: World) -> None:
    world.red_pr(log=github_log(["new failure: tests/test_calc.py::test_add"]))
    labelled = world.load()
    labelled["prs"][0]["labels"] = [{"name": "needs-human"}]
    world.save(labelled)

    result = tick()

    assert result.exit_code == 0, result.output
    assert world.resumes() == []
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
