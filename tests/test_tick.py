"""Seam 1: `lanes tick` through the CLI, against a fake `gh`, `claude` and `codex` on PATH and a real `LaneStore`.

The fakes read the world from a JSON file the test writes, and log every call they get, so a test sets up pull
requests, CI logs, tickets, sessions and reviewers' verdicts, ticks, and reads back what Lanes did to them.
"""

import json
import os
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
    log.write(json.dumps(args) + "\\n")
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
    assert "T9" not in again.output
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
    assert len(world.resumes()) == 2
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
