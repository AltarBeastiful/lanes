"""`lanes go` through the CLI, against markdown tickets, a real git origin and a fake `claude` on PATH."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from lanes.cli import main
from lanes.models import LaneDispatched
from lanes.store.files import FileStore, state_directory

FULL_ID = "dddddddd-0000-1111-2222-333333333333"

FAKE_CLAUDE = f"""#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
# Where the new worktree would branch from: the cached origin/main when the session starts.
origin = subprocess.run(["git", "rev-parse", "origin/main"], capture_output=True, text=True).stdout.strip()
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "cwd": os.getcwd(), "origin": origin}}) + "\\n")
if args[:1] == ["agents"]:
    print(json.dumps([{{"id": "dddddddd", "sessionId": "{FULL_ID}", "name": "T12", "state": "working",
                        "cwd": os.getcwd(), "kind": "background", "startedAt": 3}}]))
else:
    print("backgrounded · dddddddd · T12")
"""

LANES_TOML = """\
host = "github"
tracker = "markdown"
test_command = "pytest"
max_lanes = 2
codex_allowed = false

[reviewers]
trivial = []
small = []
medium = []
high = []
"""

TICKET = """\
# 12: Round the mean

**Status:** {status}

## What to build

The mean of a list rounds to two places.

## Acceptance criteria

- [ ] `mean([1, 2, 2])` returns 1.67

## Lanes

```toml
route = "direct"
tier = "{tier}"
produces = []
consumes = []
seams = ["cli"]
demo = "mean([1, 2, 2]) is 1.67"
```
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class Lanes:
    """A clone of a real origin, set up for Lanes with markdown tickets, and a fake `claude` that logs its calls."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.repo = root / "repo"
        self._log = root / "claude.log"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.repo))
        self.commit_upstream("first")
        git(self.repo, "pull", "-q", "origin", "main")
        (self.repo / ".lanes").mkdir()
        (self.repo / ".lanes" / "lanes.toml").write_text(LANES_TOML)
        self.issues = self.repo / ".scratch" / "feature" / "issues"
        self.issues.mkdir(parents=True)
        bin = root / "bin"
        bin.mkdir()
        (bin / "claude").write_text(FAKE_CLAUDE)
        (bin / "claude").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(self._log))
        monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
        monkeypatch.chdir(self.repo)

    def ticket(self, tier: str = "medium", status: str = "ready-for-agent", text: str | None = None) -> None:
        (self.issues / "12-round-the-mean.md").write_text(text or TICKET.format(tier=tier, status=status))

    def commit_upstream(self, message: str) -> str:
        """Push a commit to origin from another clone, as a merged pull request would; return its hash."""
        other = self.root / "other"
        if not other.exists():
            git(self.root, "clone", "-q", str(self.origin), str(other))
        git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", message)
        git(other, "push", "-q", "origin", "HEAD:main")
        return git(other, "rev-parse", "HEAD").strip()

    def go(self, *args: str) -> Result:
        return CliRunner().invoke(main, ["go", *args])

    def dispatches(self) -> list[dict[str, Any]]:
        """Each `claude --bg` call: its arguments and working directory."""
        calls = [json.loads(line) for line in self._log.read_text().splitlines()] if self._log.exists() else []
        return [call for call in calls if "--bg" in call["args"]]

    def events(self) -> list[Any]:
        return FileStore(state_directory(self.repo)).events()


@pytest.fixture
def lanes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lanes:
    return Lanes(tmp_path, monkeypatch)


def test_go_starts_a_medium_ticket_as_t_n_running_implement_on_sonnet(lanes: Lanes) -> None:
    lanes.ticket(tier="medium")

    result = lanes.go("12")

    assert result.exit_code == 0, result.output
    [dispatch] = lanes.dispatches()
    args = dispatch["args"]
    assert args[args.index("--name") + 1] == "T12"
    assert args[args.index("--agent") + 1] == "implementer"
    assert args[args.index("--model") + 1] == "claude-sonnet-5-5"
    assert args[-1] == "/mattpocock-skills:implement #12, then write the pull request body with /mattpocock-skills:pr"
    assert Path(dispatch["cwd"]) == lanes.repo.resolve()


@pytest.mark.parametrize(
    ("tier", "starts", "ends"),
    [
        ("high", "/mattpocock-skills:implement #12, then", "with /mattpocock-skills:pr"),
        ("small", "Ticket #12:\n\n# 12: Round the mean", "write the pull request body with /mattpocock-skills:pr"),
        ("trivial", "Ticket #12:\n\n# 12: Round the mean", '```\n'),
    ],
)
def test_each_tier_gets_its_prompt_form(lanes: Lanes, tier: str, starts: str, ends: str) -> None:
    lanes.ticket(tier=tier)

    result = lanes.go("12")

    assert result.exit_code == 0, result.output
    [dispatch] = lanes.dispatches()
    prompt = dispatch["args"][-1]
    assert prompt.startswith(starts)
    assert prompt.endswith(ends)
    assert ("The mean of a list rounds to two places." in prompt) == (tier in ("small", "trivial"))


def test_each_tier_runs_on_the_model_the_config_names_for_it(lanes: Lanes) -> None:
    (lanes.repo / ".lanes" / "lanes.toml").write_text(LANES_TOML + '\n[models]\nhigh = "claude-opus-5-5"\n')
    lanes.ticket(tier="high")
    lanes.go("12")
    lanes.ticket(tier="small")
    lanes.go("12")

    models = [call["args"][call["args"].index("--model") + 1] for call in lanes.dispatches()]
    assert models == ["claude-opus-5-5", "claude-sonnet-5-5"]


def test_go_fetches_origin_before_the_session_starts(lanes: Lanes) -> None:
    lanes.ticket()
    merged = lanes.commit_upstream("the blocker's merge")

    result = lanes.go("12")

    assert result.exit_code == 0, result.output
    [dispatch] = lanes.dispatches()
    assert dispatch["origin"] == merged


def test_a_ticket_that_fails_check_is_refused_with_its_problems(lanes: Lanes) -> None:
    lanes.ticket(text=TICKET.format(tier="medium", status="ready-for-agent").replace('demo = "mean([1, 2, 2]) is 1.67"', ""))

    result = lanes.go("12")

    assert result.exit_code == 1
    assert "12: missing demo" in result.output
    assert lanes.dispatches() == []
    assert lanes.events() == []


def test_a_ticket_not_marked_ready_is_refused(lanes: Lanes) -> None:
    lanes.ticket(status="needs-triage")

    result = lanes.go("12")

    assert result.exit_code == 1
    assert "12: not labelled ready-for-agent; run lanes check 12" in result.output
    assert lanes.dispatches() == []


def test_go_logs_the_dispatch_and_remembers_the_lanes_session(lanes: Lanes) -> None:
    lanes.ticket(tier="medium")

    result = lanes.go("12")

    assert result.exit_code == 0, result.output
    [event] = lanes.events()
    assert isinstance(event, LaneDispatched)
    assert (event.ticket, event.route, event.tier) == ("12", "direct", "medium")
    assert (event.ticket_route, event.ticket_tier) == (None, None)
    assert FileStore(state_directory(lanes.repo)).session("12") == FULL_ID


def test_a_tier_override_runs_and_logs_the_lane_at_that_tier(lanes: Lanes) -> None:
    lanes.ticket(tier="medium")

    result = lanes.go("12", "--tier", "small", "--route", "grill")

    assert result.exit_code == 0, result.output
    assert "overriding the ticket's route direct with grill and tier medium with small" in result.output
    [dispatch] = lanes.dispatches()
    assert dispatch["args"][-1].startswith("Ticket #12:")
    [event] = lanes.events()
    assert isinstance(event, LaneDispatched)
    assert (event.route, event.tier, event.ticket_route, event.ticket_tier) == ("grill", "small", "direct", "medium")


def test_an_override_equal_to_the_ticket_is_no_override(lanes: Lanes) -> None:
    lanes.ticket(tier="medium")

    result = lanes.go("12", "--tier", "medium")

    assert "overriding" not in result.output
    [event] = lanes.events()
    assert isinstance(event, LaneDispatched)
    assert event.ticket_tier is None


def test_a_ticket_number_with_leading_zeros_names_the_same_lane(lanes: Lanes) -> None:
    lanes.ticket()

    result = lanes.go("012")

    assert result.exit_code == 0, result.output
    [dispatch] = lanes.dispatches()
    assert dispatch["args"][dispatch["args"].index("--name") + 1] == "T12"
    assert dispatch["args"][-1].startswith("/mattpocock-skills:implement #12,")


def test_a_ticket_named_by_its_path_is_refused_for_its_number(lanes: Lanes) -> None:
    lanes.ticket()

    result = lanes.go(".scratch/feature/issues/12-round-the-mean.md")

    assert result.exit_code == 1
    assert "name the ticket by its number, which its lane is named after" in result.output
    assert lanes.dispatches() == []
