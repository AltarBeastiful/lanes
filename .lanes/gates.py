"""The Lanes gates: certify the checked-out head against its base, with no model involved.

Run from the repository root in a fresh checkout:

    python .lanes/gates.py --base <rev> [--labels a,b] [--body <pull request body>]

A refactor declares each test that proves it preserved behaviour on its own line
of the pull request body, as `behaviour: preserved <test-id>`. Such a test must
pass on both the base and the head, instead of failing on the base. Only a new
test's own file is copied onto the base, so a test that needs a helper the base
lacks comes out inconclusive, for the reviewer.

The gates reach the test framework only through `.lanes/runner` (ADR-0002). Each
problem is printed on its own line; exit 0 when there are none, 1 otherwise.
"""

import argparse
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

RUNNER = Path(".lanes/runner")
FAILED = ("failed", "error")
ALLOW_CONFIG = "allow-config"
PRESERVED = re.compile(r"^\s*behaviour: preserved\s+(.+?)\s*$", re.MULTILINE)
# CI, coverage, lint and Lanes config, the implementer agent and its hooks included:
# changing any of it needs the allow-config label.
CONFIG_DIRS = [".claude/", ".github/", ".lanes/"]
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


def preserved_tests(body: str) -> set[str]:
    return {match.strip("`") for match in PRESERVED.findall(body)}


def ask_runner(cwd: Path, *args: str) -> tuple[int, list[str]]:
    done = subprocess.run([RUNNER.resolve(), *args], cwd=cwd, stdout=subprocess.PIPE, text=True)
    return done.returncode, [line for line in done.stdout.splitlines() if line]


def run_outcomes(cwd: Path, *files: str) -> tuple[int, dict[str, str]]:
    """Ask the runner to run the given files, or the whole suite, and map each test ID to its outcome."""
    code, lines = ask_runner(cwd, "run", *files)
    outcomes: dict[str, str] = {}
    for line in lines:
        outcome, test_id = line.split(" ", 1)
        outcomes[test_id] = outcome
    return code, outcomes


class Snapshot:
    """What the runner reports for the whole suite at one commit."""

    def __init__(self, cwd: Path) -> None:
        self.run_code, self.outcomes = run_outcomes(cwd)
        self.skips_code, skips = ask_runner(cwd, "skips")
        self.skips = set(skips)

    def failed(self, test_id: str) -> bool:
        return self.outcomes.get(test_id) in FAILED


def run_new_tests_on_base(worktree: Path, head: str, new_tests: list[str], preserved: set[str]) -> tuple[list[str], list[str]]:
    """Run the head's version of each new test on the base, where it must fail, or pass if preserved.

    Returns the problems, and the inconclusive tests that go to the reviewer instead.
    """
    by_file: dict[str, list[str]] = {}
    for test_id in new_tests:
        # A test ID starts with its file's path (ADR-0002), so the runner can run the file.
        by_file.setdefault(test_id.split("::", 1)[0], []).append(test_id)
    problems: list[str] = []
    inconclusive: list[str] = []
    # One run per file: a file that can't collect on the base mustn't hide another file's tests.
    for path, test_ids in sorted(by_file.items()):
        git("-C", str(worktree), "checkout", head, "--", path)
        code, outcomes = run_outcomes(worktree, path)
        if code not in (0, 1):
            inconclusive += [f"{test_id} (runner run exited {code})" for test_id in test_ids]
            continue
        for test_id in test_ids:
            outcome = outcomes.get(test_id)
            if outcome not in ("passed", "failed"):
                inconclusive.append(f"{test_id} ({outcome or 'no outcome'})")
            elif test_id in preserved and outcome == "failed":
                problems.append(f"preserved test does not pass on base: {test_id}")
            elif test_id not in preserved and outcome == "passed":
                problems.append(f"vacuous test, passes on base: {test_id}")
    return problems, inconclusive


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--labels", default="")
    parser.add_argument("--body", default="")
    args = parser.parse_args(argv)
    preserved = preserved_tests(args.body)
    base = git("merge-base", args.base, "HEAD").strip()
    head = git("rev-parse", "HEAD").strip()

    at_head = Snapshot(Path.cwd())
    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp) / "base"
        git("worktree", "add", "--detach", "--quiet", str(worktree), base)
        try:
            at_base = Snapshot(worktree)
            # Only a test passing at head needs proof; a failing or skipped one is already a problem.
            new_tests = [test_id for test_id, outcome in at_head.outcomes.items() if outcome == "passed" and test_id not in at_base.outcomes]
            invalid, inconclusive = run_new_tests_on_base(worktree, head, new_tests, preserved)
        finally:
            git("worktree", "remove", "--force", str(worktree))

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
    problems += invalid
    # A preserved test the base already had needs no rerun: its outcome there is known.
    problems += [
        f"preserved test does not pass on base: {test_id}"
        for test_id in sorted(preserved)
        if at_base.outcomes.get(test_id, "passed") != "passed"
    ]
    problems += [f"preserved test does not pass on head: {test_id}" for test_id in sorted(preserved) if at_head.outcomes.get(test_id) != "passed"]
    for test_and_reason in inconclusive:
        print(f"inconclusive on base, for the reviewer: {test_and_reason}")
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print("gates: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
