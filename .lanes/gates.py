"""The Lanes gates: certify the checked-out head against its base, with no model involved.

Run from the repository root in a fresh checkout:

    python .lanes/gates.py --base <rev> [--labels a,b]

The gates reach the test framework only through `.lanes/runner` (ADR-0002). Each
problem is printed on its own line; exit 0 when there are none, 1 otherwise.
"""

import argparse
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

RUNNER = Path(".lanes/runner")
FAILED = ("failed", "error")
ALLOW_CONFIG = "allow-config"
# CI, coverage and lint config: changing any of it needs the allow-config label.
CONFIG_DIRS = [".github/", ".lanes/"]
CONFIG_NAMES = [
    ".coveragerc",
    ".flake8",
    ".isort.cfg",
    ".mypy.ini",
    ".pre-commit-config.yaml",
    ".pylintrc",
    ".ruff.toml",
    "mypy.ini",
    "pylintrc",
    "pyrightconfig.json",
    "pytest.ini",
    "ruff.toml",
    "setup.cfg",
    "tox.ini",
]
CONFIG_TOOLS = ["black", "coverage", "flake8", "isort", "mypy", "pylint", "pyright", "pytest", "ruff"]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def pyproject_tools(rev: str) -> dict[str, Any]:
    try:
        text = git("show", f"{rev}:pyproject.toml")
    except subprocess.CalledProcessError:
        return {}
    tools: dict[str, Any] = tomllib.loads(text).get("tool", {})
    return tools


def config_changes(base: str) -> list[str]:
    changed = git("diff", "--no-renames", "--name-only", base, "HEAD").splitlines()
    found = [path for path in changed if path.startswith(tuple(CONFIG_DIRS)) or Path(path).name in CONFIG_NAMES]
    if "pyproject.toml" in changed:
        before, after = pyproject_tools(base), pyproject_tools("HEAD")
        found += [f"pyproject.toml [tool.{tool}]" for tool in CONFIG_TOOLS if before.get(tool) != after.get(tool)]
    return found


def ask_runner(command: str, cwd: Path) -> tuple[int, list[str]]:
    done = subprocess.run([RUNNER.resolve(), command], cwd=cwd, stdout=subprocess.PIPE, text=True)
    return done.returncode, [line for line in done.stdout.splitlines() if line]


class Snapshot:
    """What the runner reports for the whole suite at one commit."""

    def __init__(self, cwd: Path) -> None:
        self.run_code, lines = ask_runner("run", cwd)
        self.outcomes: dict[str, str] = {}
        for line in lines:
            outcome, test_id = line.split(" ", 1)
            self.outcomes[test_id] = outcome
        self.skips_code, skips = ask_runner("skips", cwd)
        self.skips = set(skips)

    def failed(self, test_id: str) -> bool:
        return self.outcomes.get(test_id) in FAILED


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--labels", default="")
    args = parser.parse_args(argv)
    base = git("merge-base", args.base, "HEAD").strip()

    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp) / "base"
        git("worktree", "add", "--detach", "--quiet", str(worktree), base)
        try:
            at_base = Snapshot(worktree)
        finally:
            git("worktree", "remove", "--force", str(worktree))
    at_head = Snapshot(Path.cwd())

    problems: list[str] = []
    if ALLOW_CONFIG not in args.labels.split(","):
        problems += [f"config changed without {ALLOW_CONFIG}: {change}" for change in config_changes(base)]
    # Without a conclusive answer at both commits nothing can be compared, so the gates fail closed.
    for at, snapshot in (("base", at_base), ("head", at_head)):
        if snapshot.run_code not in (0, 1):
            problems.append(f"suite inconclusive at {at}: runner run exited {snapshot.run_code}")
        if snapshot.skips_code != 0:
            problems.append(f"skips inconclusive at {at}: runner skips exited {snapshot.skips_code}")
    # An `error` ID is a file that failed to collect, not a test, so it can't be deleted.
    deleted = [test_id for test_id, outcome in at_base.outcomes.items() if outcome != "error" and test_id not in at_head.outcomes]
    failing = [test_id for test_id in at_head.outcomes if at_head.failed(test_id) and not at_base.failed(test_id)]
    problems += [f"test deleted: {test_id}" for test_id in deleted]
    problems += [f"new failure: {test_id}" for test_id in failing]
    problems += [f"new skip: {test_id}" for test_id in sorted(at_head.skips - at_base.skips)]
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print("gates: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
