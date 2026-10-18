"""The `Agents` port over a fake `claude` on PATH, for the call `tick` doesn't make: dispatching a new lane."""

import json
import os
from pathlib import Path

import pytest

from lanes.agents.claude import ClaudeAgents

FULL_ID = "dddddddd-0000-1111-2222-333333333333"

FAKE_CLAUDE = f"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "cwd": os.getcwd()}}) + "\\n")
if args[:1] == ["agents"]:
    print(json.dumps([
        {{"id": "eeeeeeee", "sessionId": "eeeeeeee-0000-0000-0000-000000000000", "name": "T3", "state": "done",
          "cwd": "/elsewhere", "kind": "background", "startedAt": 1}},
        {{"pid": 1, "sessionId": "ffffffff-0000-0000-0000-000000000000", "name": "me", "status": "idle",
          "cwd": "/elsewhere", "kind": "interactive", "startedAt": 2}},
        {{"id": "dddddddd", "sessionId": "{FULL_ID}", "name": "T12", "state": "working", "cwd": os.getcwd(),
          "kind": "background", "startedAt": 3}},
    ]))
else:
    print("backgrounded · dddddddd · T12")
"""


def test_dispatch_starts_a_named_background_session_and_returns_its_full_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin, worktree, log = tmp_path / "bin", tmp_path / "worktree", tmp_path / "claude.log"
    bin.mkdir()
    worktree.mkdir()
    (bin / "claude").write_text(FAKE_CLAUDE)
    (bin / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))

    session = ClaudeAgents().dispatch("T12", "implementer", "Implement ticket 12.", "sonnet", worktree)

    assert session == FULL_ID
    [started, _listed] = [json.loads(line) for line in log.read_text().splitlines()]
    args = started["args"]
    assert started["cwd"] == str(worktree)
    assert "--bg" in args
    assert args[args.index("--name") + 1] == "T12"
    assert args[args.index("--agent") + 1] == "implementer"
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[-1] == "Implement ticket 12."
