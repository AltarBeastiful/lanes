#!/usr/bin/env python3
"""The implementer's quick checks: a Stop hook that sends obvious breakage back before CI sees it.

It runs `lint_command` from `.lanes/lanes.toml`, when set, and the test files the
lane changed against the default branch through `.lanes/runner run`. A test file
is source code whose name starts with `test_` or ends in `_test`, `.test` or
`.spec` before its extension. When `test_command` runs under `uv run`, so does the
runner, so that it sees the project's environment as the gates do.

When either fails, the hook keeps the lane going with the output as the reason,
at most three times in a row; after that it lets the lane stop and leaves the
failure to CI and the fix rounds.
"""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ATTEMPTS = 3
# Enough output to act on without flooding the lane's context.
TAIL = 60
TEST_FILE = re.compile(r"^(test_.*|.*(_test|\.test|\.spec))\.(py|js|jsx|ts|tsx|mjs|cjs|rb|go|rs|java|kt|cs|php|ex|exs)$")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout.strip()


def changed_tests(root: Path) -> list[str]:
    """Test files that differ from where the lane branched off, committed or not."""
    base = git(root, "merge-base", "HEAD", "origin/HEAD") or git(root, "merge-base", "HEAD", "main") or "HEAD"
    paths = set(git(root, "diff", "--name-only", "--no-renames", base).splitlines())
    paths |= set(git(root, "ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(path for path in paths if TEST_FILE.search(Path(path).name) and (root / path).is_file())


def tail(text: str) -> str:
    return "\n".join(text.rstrip("\n").splitlines()[-TAIL:])


def failures(root: Path) -> list[str]:
    found = []
    config = tomllib.loads((root / ".lanes" / "lanes.toml").read_text(encoding="utf-8"))
    lint = config.get("lint_command", "")
    if lint:
        done = subprocess.run(lint, shell=True, cwd=root, capture_output=True, text=True)
        if done.returncode != 0:
            found.append(f"`{lint}` exited {done.returncode}:\n{tail(done.stdout + done.stderr)}")
    tests = changed_tests(root)
    if tests:
        launcher = ["uv", "run"] if config.get("test_command", "").split()[:2] == ["uv", "run"] else []
        runner = [*launcher, str(root / ".lanes" / "runner"), "run", *tests]
        done = subprocess.run(runner, cwd=root, capture_output=True, text=True)
        if done.returncode != 0:
            found.append(f"the tests you changed exited {done.returncode}:\n{tail(done.stdout + done.stderr)}")
    return found


def main() -> int:
    given = json.load(sys.stdin)
    root = Path(git(Path(given.get("cwd") or "."), "rev-parse", "--show-toplevel"))
    # Kept beside the worktree's git metadata: never committed, and gone with the worktree.
    counter = root / git(root, "rev-parse", "--git-path", f"lanes-quick-check-{given['session_id']}")
    # A stop that no stop hook caused starts the count again.
    attempts = int(counter.read_text()) if given.get("stop_hook_active") and counter.exists() else 0
    found = failures(root)
    if not found:
        counter.unlink(missing_ok=True)
        return 0
    if attempts >= ATTEMPTS:
        print(f"Lanes quick checks still fail after {ATTEMPTS} attempts; leaving them to CI.", file=sys.stderr)
        return 0
    counter.write_text(str(attempts + 1))
    reason = "Lanes quick checks failed; fix this before you stop.\n\n" + "\n\n".join(found)
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
