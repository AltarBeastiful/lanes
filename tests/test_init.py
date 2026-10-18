"""Seam 1: `lanes init` driven through the CLI against a temporary repository and home."""

import json
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from lanes.cli import main
from lanes.models import LanesConfig, TierReviewers

Init = Callable[..., Result]

# host, tracker, test command, N, Codex allowed, then reviewers for trivial, small, medium and high.
DEFAULTS = ["github", "github", "pytest", "2", "y", "", "", "", ""]


def answers(**changed: str) -> str:
    order = ["host", "tracker", "test_command", "max_lanes", "codex", "trivial", "small", "medium", "high"]
    given = dict(zip(order, DEFAULTS)) | changed
    return "".join(f"{given[name]}\n" for name in order)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


@pytest.fixture
def repo(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def init(repo: Path) -> Init:
    def run(given: str | None = None, *args: str) -> Result:
        return CliRunner().invoke(main, ["init", *args], input=answers() if given is None else given)

    return run


def test_init_writes_the_config_hotspots_gates_and_pytest_runner(repo: Path, init: Init) -> None:
    result = init()

    assert result.exit_code == 0, result.output
    for path in (".lanes/lanes.toml", ".lanes/hotspots.txt", ".lanes/gates.py", ".github/workflows/gates.yml"):
        assert (repo / path).is_file(), path
    runner = repo / ".lanes" / "runner"
    assert runner.stat().st_mode & 0o111
    assert "pytest" in runner.read_text()
    assert "uv.lock" in (repo / ".lanes" / "hotspots.txt").read_text().split()
    assert "python .lanes/gates.py" in (repo / ".github" / "workflows" / "gates.yml").read_text()


def test_written_config_round_trips_through_the_config_model(repo: Path, init: Init) -> None:
    result = init(
        answers(
            tracker="markdown",
            test_command='uv run pytest -k "not slow"',
            max_lanes="3",
            small="none",
            high="codex, claude-max, claude-adversarial",
        )
    )

    assert result.exit_code == 0, result.output
    config = LanesConfig.model_validate(tomllib.loads((repo / ".lanes" / "lanes.toml").read_text()))
    assert config == LanesConfig(
        host="github",
        tracker="markdown",
        test_command='uv run pytest -k "not slow"',
        max_lanes=3,
        codex_allowed=True,
        reviewers=TierReviewers(
            trivial=[],
            small=[],
            medium=["codex", "claude-high"],
            high=["codex", "claude-max", "claude-adversarial"],
        ),
    )


def test_answering_no_to_codex_records_it_as_disallowed(repo: Path, init: Init) -> None:
    result = init(answers(codex="n"))

    assert result.exit_code == 0, result.output
    config = tomllib.loads((repo / ".lanes" / "lanes.toml").read_text())
    assert config["codex_allowed"] is False
    assert config["reviewers"]["high"] == ["claude-adversarial", "claude-max"]


def test_codex_is_refused_as_a_reviewer_once_disallowed(repo: Path, init: Init) -> None:
    result = init(answers(codex="n", small="codex\nclaude-adversarial"))

    assert result.exit_code == 0, result.output
    assert "Codex may not read this repository" in result.output
    config = tomllib.loads((repo / ".lanes" / "lanes.toml").read_text())
    assert config["reviewers"]["small"] == ["claude-adversarial"]


@pytest.mark.parametrize("command", ["pytest", "uv run pytest -x", "python -m pytest tests"])
def test_pytest_test_commands_get_the_pytest_runner(repo: Path, init: Init, command: str) -> None:
    result = init(answers(test_command=command))

    assert result.exit_code == 0, result.output
    assert (repo / ".lanes" / "runner").is_file()


@pytest.mark.parametrize("command", ["npm test", "cargo test", "uv run tox"])
def test_a_non_pytest_test_command_is_refused_with_a_pointer_to_the_runner_contract(
    repo: Path, init: Init, command: str
) -> None:
    result = init(answers(test_command=command))

    assert result.exit_code == 2, result.output
    assert "only the pytest runner" in result.output
    assert "docs/adr/0002-runner-script.md" in result.output
    assert "run [files...]" in result.output and "skips" in result.output
    assert [path.name for path in repo.iterdir()] == [".git"]


def registered(config_home: Path) -> list[str]:
    repos: list[str] = tomllib.loads((config_home / "lanes" / "repos.toml").read_text())["repos"]
    return repos


def test_init_registers_the_repository_in_the_user_config(repo: Path, home: Path, init: Init) -> None:
    result = init()

    assert result.exit_code == 0, result.output
    assert registered(home / ".config") == [str(repo.resolve())]


def test_init_registers_a_repository_once_alongside_the_others(
    repo: Path, tmp_path: Path, init: Init, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    (config_home / "lanes").mkdir(parents=True)
    (config_home / "lanes" / "repos.toml").write_text('repos = ["/elsewhere/other"]\n')

    init()
    result = init()

    assert result.exit_code == 0, result.output
    assert registered(config_home) == ["/elsewhere/other", str(repo.resolve())]


def trust(home: Path, path: Path, accepted: bool = True) -> None:
    projects = {str(path.resolve()): {"hasTrustDialogAccepted": accepted}}
    (home / ".claude.json").write_text(json.dumps({"projects": projects}))


def test_init_warns_when_workspace_trust_has_not_been_accepted(repo: Path, home: Path, init: Init) -> None:
    trust(home, repo, accepted=False)

    result = init()

    assert result.exit_code == 0, result.output
    assert f"workspace trust has not been accepted for {repo.resolve()}" in result.output


def test_init_warns_when_claude_code_has_never_run(init: Init) -> None:
    result = init()

    assert result.exit_code == 0, result.output
    assert "workspace trust has not been accepted" in result.output


@pytest.mark.parametrize("trusted", [".", ".."])
def test_init_is_quiet_about_trust_accepted_for_the_repository_or_a_parent(
    repo: Path, home: Path, init: Init, trusted: str
) -> None:
    trust(home, repo / trusted)

    result = init()

    assert result.exit_code == 0, result.output
    assert "workspace trust" not in result.output


def test_lanes_own_config_is_valid() -> None:
    lanes_toml = Path(__file__).resolve().parent.parent / ".lanes" / "lanes.toml"
    LanesConfig.model_validate(tomllib.loads(lanes_toml.read_text()))


def test_init_from_a_subdirectory_sets_up_the_repository_root(
    repo: Path, home: Path, init: Init, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "src").mkdir()
    monkeypatch.chdir(repo / "src")

    result = init()

    assert result.exit_code == 0, result.output
    assert (repo / ".lanes" / "lanes.toml").is_file()
    assert (repo / ".github" / "workflows" / "gates.yml").is_file()
    assert not (repo / "src" / ".lanes").exists()
    assert registered(home / ".config") == [str(repo.resolve())]


def test_init_outside_a_git_repository_is_refused(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init"], input=answers())

    assert result.exit_code == 2, result.output
    assert "not a git repository" in result.output
    assert not (tmp_path / ".lanes").exists()


def test_init_refuses_to_overwrite_a_file_that_differs_unless_forced(repo: Path, init: Init) -> None:
    (repo / ".lanes").mkdir()
    (repo / ".lanes" / "runner").write_text("#!/bin/sh\n# hand-written\n")

    refused = init()

    assert refused.exit_code == 2, refused.output
    assert ".lanes/runner differs" in refused.output
    assert (repo / ".lanes" / "runner").read_text() == "#!/bin/sh\n# hand-written\n"
    assert not (repo / ".lanes" / "lanes.toml").exists()

    forced = init(None, "--force")

    assert forced.exit_code == 0, forced.output
    assert "pytest" in (repo / ".lanes" / "runner").read_text()


def test_init_run_again_with_the_same_answers_succeeds(repo: Path, init: Init) -> None:
    init()

    result = init()

    assert result.exit_code == 0, result.output
