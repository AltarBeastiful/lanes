"""Seam 2: the real gate script and pytest runner against the fixture repository's seeded branches."""

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "examples" / "fixture-repo"
TEMPLATES = ROOT / "src" / "lanes" / "templates"

Gate = Callable[..., subprocess.CompletedProcess[str]]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "--all")
    git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.com", "commit", "-q", "-m", message)


def install_gates(repo: Path) -> None:
    """Put the gate script and pytest runner where `lanes init` installs them."""
    (repo / ".lanes").mkdir()
    shutil.copy2(TEMPLATES / "gates" / "gates.py", repo / ".lanes" / "gates.py")
    shutil.copy2(TEMPLATES / "runner" / "pytest", repo / ".lanes" / "runner")


def build_fixture(repo: Path) -> None:
    shutil.copytree(FIXTURE / "main", repo)
    git(repo, "init", "-q", "-b", "main")
    install_gates(repo)
    commit_all(repo, "main")
    for branch in sorted((FIXTURE / "branches").iterdir()):
        git(repo, "checkout", "-q", "-b", branch.name, "main")
        # Fresh mtimes: a fresh checkout gives every file one mtime, and git's stat
        # cache would miss an overlay of the same size as the file it replaces.
        shutil.copytree(
            branch, repo, dirs_exist_ok=True, copy_function=shutil.copy, ignore=shutil.ignore_patterns("DELETED")
        )
        deleted = branch / "DELETED"
        for path in deleted.read_text().split() if deleted.exists() else []:
            (repo / path).unlink()
        commit_all(repo, branch.name)


def run_gate(
    repo: Path, labels: Sequence[str] = (), body: str = "", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    path = os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]])
    return subprocess.run(
        [sys.executable, ".lanes/gates.py", "--base", "main", "--labels", ",".join(labels), f"--body={body}"],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": path, **(env or {})},
    )


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("fixture") / "repo"
    build_fixture(repo)
    return repo


@pytest.fixture
def gate(fixture_repo: Path) -> Gate:
    def run(branch: str, labels: Sequence[str] = (), body: str = "") -> subprocess.CompletedProcess[str]:
        git(fixture_repo, "checkout", "-q", branch)
        return run_gate(fixture_repo, labels, body)

    return run


def test_clean_branch_passes(gate: Gate) -> None:
    result = gate("clean")
    assert result.returncode == 0, result.stdout + result.stderr


def test_deleting_a_test_fails_the_gate(gate: Gate) -> None:
    result = gate("deletes-test")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "test deleted: tests/test_calc.py::test_divide" in result.stdout


def test_deleting_a_test_file_fails_the_gate(gate: Gate) -> None:
    result = gate("deletes-test-file")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "test deleted: tests/test_more.py::test_add_is_commutative" in result.stdout


def test_adding_a_skip_fails_the_gate(gate: Gate) -> None:
    result = gate("adds-skip")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "new skip: tests/test_calc.py::test_divide" in result.stdout


def test_adding_an_xfail_fails_the_gate(gate: Gate) -> None:
    result = gate("adds-xfail")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "new skip: tests/test_calc.py::test_add" in result.stdout


def test_test_that_skips_in_a_fresh_checkout_fails_the_gate(gate: Gate) -> None:
    result = gate("skips-without-fixture")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "new skip: tests/test_data.py::test_mean_of_data" in result.stdout


def test_lint_config_edit_fails_without_allow_config(gate: Gate) -> None:
    result = gate("edits-lint-config")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "config changed without allow-config: pyproject.toml [tool.ruff]" in result.stdout


def test_lint_config_edit_passes_with_allow_config(gate: Gate) -> None:
    result = gate("edits-lint-config", labels=["ready", "allow-config"])
    assert result.returncode == 0, result.stdout + result.stderr


def test_new_failure_fails_the_gate_but_one_already_failing_on_the_base_does_not(gate: Gate) -> None:
    result = gate("breaks-add")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "new failure: tests/test_calc.py::test_add" in result.stdout
    assert "test_mean_of_nothing_is_zero" not in result.stdout


def test_new_test_that_passes_on_the_base_fails_the_gate(gate: Gate) -> None:
    result = gate("adds-passing-test")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "vacuous test, passes on base: tests/test_more.py::test_add_zero" in result.stdout


def test_new_test_that_fails_on_the_base_by_assertion_passes(gate: Gate) -> None:
    result = gate("rounds-mean")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_mean_rounds_to_two_places" not in result.stdout


def test_new_test_that_cannot_import_on_the_base_is_inconclusive_not_failed(gate: Gate) -> None:
    result = gate("adds-power")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "inconclusive on base, for the reviewer: tests/test_power.py::test_power (runner run exited 2)" in result.stdout


def test_test_that_cannot_import_on_the_base_does_not_hide_a_vacuous_one(gate: Gate) -> None:
    result = gate("hides-passing-test")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "vacuous test, passes on base: tests/test_more.py::test_add_zero" in result.stdout
    assert "inconclusive on base, for the reviewer: tests/test_power.py::test_power (runner run exited 2)" in result.stdout


def test_preserved_test_the_base_already_had_must_pass_on_both(gate: Gate) -> None:
    result = gate("clean", body="behaviour: preserved tests/test_calc.py::test_mean_of_nothing_is_zero")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "preserved test does not pass on base: tests/test_calc.py::test_mean_of_nothing_is_zero" in result.stdout
    assert "preserved test does not pass on head: tests/test_calc.py::test_mean_of_nothing_is_zero" in result.stdout


def test_new_test_that_passes_on_the_base_passes_when_declared_preserved(gate: Gate) -> None:
    result = gate("adds-passing-test", body="behaviour: preserved tests/test_more.py::test_add_zero")
    assert result.returncode == 0, result.stdout + result.stderr


def test_preserved_test_that_fails_on_the_base_fails_the_gate(gate: Gate) -> None:
    result = gate("floors-divide", body="Refactor.\n\nbehaviour: preserved `tests/test_more.py::test_divide_is_whole`\n")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "preserved test does not pass on base: tests/test_more.py::test_divide_is_whole" in result.stdout


FAKE_RUNNER = """#!/bin/sh
echo "$1" >> "$RUNNER_LOG"
[ -e broken ] && exit 3
case "$1" in
  run) cat outcomes.txt ;;
  skips) cat skips.txt ;;
  *) exit 64 ;;
esac
"""


def fake_runner_repo(repo: Path, base: dict[str, str], head: dict[str, str]) -> None:
    """A repository whose runner answers from the files of the commit it runs in."""
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    install_gates(repo)
    (repo / ".lanes" / "runner").write_text(FAKE_RUNNER)
    for files, branch in ((base, "main"), (head, "head")):
        if branch != "main":
            git(repo, "checkout", "-q", "-b", branch)
        for name in ("outcomes.txt", "skips.txt", "broken"):
            if name in files:
                (repo / name).write_text(files[name])
            elif (repo / name).exists():
                (repo / name).unlink()
        commit_all(repo, branch)


def test_gates_reach_the_framework_only_through_the_runner(tmp_path: Path) -> None:
    fake_runner_repo(
        tmp_path / "repo",
        base={"outcomes.txt": "passed a\n", "skips.txt": ""},
        head={"outcomes.txt": "passed a\nskipped b\n", "skips.txt": "b\n"},
    )
    no_pytest = tmp_path / "no-pytest"
    no_pytest.mkdir()
    (no_pytest / "pytest.py").write_text("raise ImportError('the gates must not import pytest')\n")
    log = tmp_path / "runner.log"

    result = run_gate(tmp_path / "repo", env={"PYTHONPATH": str(no_pytest), "RUNNER_LOG": str(log)})

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["new skip: b"]
    assert sorted(log.read_text().split()) == ["run", "run", "skips", "skips"]


def test_runner_that_cannot_run_the_base_fails_the_gate(tmp_path: Path) -> None:
    fake_runner_repo(
        tmp_path / "repo",
        base={"outcomes.txt": "passed a\n", "skips.txt": "", "broken": ""},
        head={"outcomes.txt": "", "skips.txt": ""},
    )

    result = run_gate(tmp_path / "repo", env={"RUNNER_LOG": str(tmp_path / "runner.log")})

    assert result.returncode == 1, result.stdout + result.stderr
    assert "suite inconclusive at base: runner run exited 3" in result.stdout


@pytest.mark.parametrize(
    ("installed", "template"),
    [
        (".lanes/gates.py", "gates/gates.py"),
        (".lanes/runner", "runner/pytest"),
        (".github/workflows/gates.yml", "gates/gates.yml"),
    ],
)
def test_lanes_runs_the_gates_it_ships(installed: str, template: str) -> None:
    assert (ROOT / installed).read_bytes() == (TEMPLATES / template).read_bytes()
