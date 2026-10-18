"""The implementer's hook scripts, fed the JSON Claude Code sends them on stdin."""

import json
import os
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "src" / "lanes" / "templates" / "hooks"

Hook = Callable[..., subprocess.CompletedProcess[str]]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    return repo


def feed(
    script: str, repo: Path, given: dict[str, Any], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    stdin = json.dumps({"session_id": "s1", "cwd": str(repo), **given})
    return subprocess.run(
        [sys.executable, str(HOOKS / script)], input=stdin, cwd=repo, capture_output=True, text=True, env=env
    )


def guard(repo: Path, tool: str, **tool_input: Any) -> subprocess.CompletedProcess[str]:
    return feed("guard.py", repo, {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input})


def test_guard_denies_a_write_to_the_gates_workflow(repo: Path) -> None:
    result = guard(repo, "Write", file_path=str(repo / ".github" / "workflows" / "gates.yml"), content="")

    assert result.returncode == 2, result.stderr
    assert ".github/workflows/gates.yml" in result.stderr


def test_guard_allows_an_ordinary_source_edit(repo: Path) -> None:
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n")

    result = guard(repo, "Edit", file_path=str(repo / "src" / "app.py"), old_string="x = 1", new_string="x = 2")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def commit(repo: Path) -> None:
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "c")


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD:main",
        "git push origin main",
        "git push -f origin T6:refs/heads/main",
        "git push origin +T6:main",
        "cd sub && git push origin main",
        "git -C . push origin main",
        "GIT_TRACE=1 git push --force-with-lease origin main",
        "git push --all origin",
        "git push",
        "bash -c 'git push origin main'",
        "eval git push origin main",
        "env git push origin main",
        "/usr/bin/git push origin main",
        "git status\ngit push origin main",
    ],
)
def test_guard_denies_a_push_to_main(repo: Path, command: str) -> None:
    commit(repo)

    result = guard(repo, "Bash", command=command)

    assert result.returncode == 2, result.stderr
    assert "pushes to main" in result.stderr


@pytest.mark.parametrize("command", ["git push -u origin T6", "git push origin HEAD", "git push", "git status && git push origin T6:T6"])
def test_guard_allows_a_lane_to_push_its_own_branch(repo: Path, command: str) -> None:
    commit(repo)
    git(repo, "switch", "-q", "-c", "T6")

    result = guard(repo, "Bash", command=command)

    assert result.returncode == 0, result.stderr


def test_guard_denies_a_push_to_the_default_branch_origin_names(repo: Path, tmp_path: Path) -> None:
    commit(repo)
    git(repo, "branch", "-m", "trunk")
    git(tmp_path, "clone", "-q", str(repo), "clone")
    clone = tmp_path / "clone"
    git(clone, "switch", "-q", "-c", "T6")

    denied = guard(clone, "Bash", command="git push origin T6:trunk")
    allowed = guard(clone, "Bash", command="git push origin T6:main")

    assert denied.returncode == 2, denied.stderr
    assert allowed.returncode == 0, allowed.stderr


@pytest.mark.parametrize(
    "command",
    [
        "echo '' > .github/workflows/gates.yml",
        "printf x >> .lanes/lanes.toml",
        "sed -i 's/strict/lax/' mypy.ini",
        "rm -rf .lanes",
        "git status && cp /tmp/x .claude/agents/implementer.md",
        "python -c 'open(\".lanes/hooks/guard.py\", \"w\")'",
        "sed -n 's/a/b/w .lanes/lanes.toml' README.md",
        "find . -name x -fprint0 .github/out",
        "cat README.md\nrm -rf .lanes",
        "sed -i.bak s/a/b/ mypy.ini",
        "sed -i s/strict/lax/ pyproject.toml",
        "< .lanes/lanes.toml tee out",
    ],
)
def test_guard_denies_a_shell_write_to_config(repo: Path, command: str) -> None:
    result = guard(repo, "Bash", command=command)

    assert result.returncode == 2, result.stderr
    assert "config" in result.stderr


@pytest.mark.parametrize(
    "command",
    ["cat .lanes/lanes.toml", "git diff origin/main -- .github/ | head", "grep -rn strict mypy.ini", "uv run pytest tests", "bash -c 'cat .lanes/lanes.toml'", "grep -i strict mypy.ini", "cat pyproject.toml"],
)
def test_guard_allows_reading_config_from_the_shell(repo: Path, command: str) -> None:
    result = guard(repo, "Bash", command=command)

    assert result.returncode == 0, result.stderr


PYPROJECT = '[project]\nname = "app"\ndependencies = []\n\n[tool.mypy]\nstrict = true\n'


def test_guard_denies_loosening_a_lint_section_of_pyproject(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(PYPROJECT)

    result = guard(repo, "Edit", file_path=str(repo / "pyproject.toml"), old_string="strict = true", new_string="strict = false")

    assert result.returncode == 2, result.stderr
    assert "[tool.mypy]" in result.stderr


def test_guard_denies_a_multi_edit_that_loosens_pyproject(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(PYPROJECT)

    result = guard(
        repo,
        "MultiEdit",
        file_path=str(repo / "pyproject.toml"),
        edits=[{"old_string": "[]", "new_string": '["click"]'}, {"old_string": "strict = true", "new_string": "strict = false"}],
    )

    assert result.returncode == 2, result.stderr
    assert "[tool.mypy]" in result.stderr


def test_guard_allows_adding_a_dependency_to_pyproject(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(PYPROJECT)

    result = guard(
        repo, "Edit", file_path=str(repo / "pyproject.toml"), old_string="dependencies = []", new_string='dependencies = ["click"]'
    )

    assert result.returncode == 0, result.stderr


def test_guard_protects_exactly_what_the_gates_protect() -> None:
    guard_lists = runpy.run_path(str(HOOKS / "guard.py"))
    gates_lists = runpy.run_path(str(ROOT / "src" / "lanes" / "templates" / "gates" / "gates.py"))

    assert guard_lists["CONFIG_DIRS"] == gates_lists["CONFIG_DIRS"]
    assert guard_lists["CONFIG_NAMES"] == gates_lists["CONFIG_NAMES"]
    assert guard_lists["CONFIG_TOOLS"] == gates_lists["CONFIG_TOOLS"]


def test_guard_fails_rather_than_allows_on_input_it_cannot_read(repo: Path) -> None:
    result = subprocess.run([sys.executable, str(HOOKS / "guard.py")], input="not json", cwd=repo, capture_output=True, text=True)

    # Neither allow (0) nor a deliberate deny (2): a failure, which `onFailure: "block"` turns into a block.
    assert result.returncode not in (0, 2), result.stderr


# A runner that fails any test file whose text says FAIL, and logs the files it was given.
FAKE_RUNNER = """#!/bin/sh
echo "$@" >> runner.log
shift
for file in "$@"; do
    if grep -q FAIL "$file"; then echo "failed $file::test"; exit 1; fi
    echo "passed $file::test"
done
"""


@pytest.fixture
def lane(repo: Path) -> Path:
    """A lane's worktree: Lanes installed on main, and a branch with work on it."""
    (repo / ".gitignore").write_text("runner.log\nuv.log\n")
    (repo / ".lanes").mkdir()
    (repo / ".lanes" / "lanes.toml").write_text('lint_command = "! grep -rn LINT src"\n')
    (repo / ".lanes" / "runner").write_text(FAKE_RUNNER)
    (repo / ".lanes" / "runner").chmod(0o755)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_old.py").write_text("FAIL on purpose, but untouched by the lane\n")
    git(repo, "add", "--all")
    commit(repo)
    git(repo, "switch", "-q", "-c", "T6")
    return repo


def quick_check(lane: Path, again: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return feed("quick-check.py", lane, {"hook_event_name": "Stop", "stop_hook_active": again}, env)


def test_quick_check_lets_a_clean_lane_stop(lane: Path) -> None:
    (lane / "tests" / "test_new.py").write_text("ok\n")

    result = quick_check(lane)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_quick_check_runs_only_the_tests_the_lane_changed(lane: Path) -> None:
    (lane / "tests" / "test_new.py").write_text("ok\n")
    (lane / "tests" / "test_committed.py").write_text("ok\n")
    git(lane, "add", "tests/test_committed.py")
    commit(lane)
    (lane / "tests" / "helpers.py").write_text("not a test file\n")
    (lane / "tests" / "test_input.json").write_text("data, not a test file\n")

    quick_check(lane)

    assert (lane / "runner.log").read_text().split() == ["run", "tests/test_committed.py", "tests/test_new.py"]


def test_quick_check_sends_a_failing_test_back(lane: Path) -> None:
    (lane / "tests" / "test_new.py").write_text("FAIL\n")

    result = quick_check(lane)

    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert "failed tests/test_new.py::test" in decision["reason"]


def test_quick_check_sends_a_lint_failure_back(lane: Path) -> None:
    (lane / "src" / "app.py").write_text("LINT = 1\n")

    result = quick_check(lane)

    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert "src/app.py:1:LINT = 1" in decision["reason"]


def test_quick_check_stops_re_entering_after_three_attempts(lane: Path) -> None:
    (lane / "tests" / "test_new.py").write_text("FAIL\n")

    attempts = [quick_check(lane, again=index > 0) for index in range(5)]

    assert [bool(attempt.stdout) for attempt in attempts] == [True, True, True, False, False]
    assert "after 3 attempts" in attempts[3].stderr


def test_quick_check_counts_again_from_a_fresh_stop(lane: Path) -> None:
    (lane / "tests" / "test_new.py").write_text("FAIL\n")
    for index in range(4):
        quick_check(lane, again=index > 0)

    fresh = quick_check(lane, again=False)

    assert json.loads(fresh.stdout)["decision"] == "block"


def test_quick_check_runs_the_runner_under_uv_when_the_tests_run_under_uv(lane: Path, tmp_path: Path) -> None:
    (lane / ".lanes" / "lanes.toml").write_text('test_command = "uv run pytest"\n')
    (lane / "tests" / "test_new.py").write_text("ok\n")
    bin = tmp_path / "bin"
    bin.mkdir()
    (bin / "uv").write_text('#!/bin/sh\necho "uv $*" >> uv.log\nshift\nexec "$@"\n')
    (bin / "uv").chmod(0o755)

    result = quick_check(lane, env={**os.environ, "PATH": f"{bin}{os.pathsep}{os.environ['PATH']}"})

    assert result.returncode == 0, result.stderr
    assert (lane / "uv.log").read_text() == f"uv run {lane / '.lanes' / 'runner'} run tests/test_new.py\n"
