"""Seam 1: `lanes init` driven through the CLI against a temporary repository and home."""

import json
import os
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

from lanes.cli import main
from lanes.models import LanesConfig, TierReviewers

Init = Callable[..., Result]

# host, tracker, test command, lint command, N, Codex allowed, then reviewers for trivial, small, medium and high.
DEFAULTS = ["github", "github", "pytest", "", "2", "y", "", "", "", ""]


def answers(**changed: str) -> str:
    order = ["host", "tracker", "test_command", "lint_command", "max_lanes", "codex", "trivial", "small", "medium", "high"]
    given = dict(zip(order, DEFAULTS)) | changed
    return "".join(f"{given[name]}\n" for name in order)


# Logs each call, and exits with $FAKE_SYSTEMCTL_EXIT, 0 when unset.
FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$FAKE_SYSTEMCTL_LOG"
exit "${FAKE_SYSTEMCTL_EXIT:-0}"
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    bin = tmp_path / "bin"
    bin.mkdir()
    (bin / "systemctl").write_text(FAKE_SYSTEMCTL)
    (bin / "systemctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SYSTEMCTL_LOG", str(tmp_path / "systemctl.log"))
    return home


def systemctl_calls(home: Path) -> list[str]:
    log = home.parent / "systemctl.log"
    return log.read_text().splitlines() if log.exists() else []


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


def frontmatter(path: Path) -> dict[str, Any]:
    found: dict[str, Any] = yaml.safe_load(path.read_text().split("---\n")[1])
    return found


def test_init_writes_the_implementer_and_reviewer_agents_and_their_hooks(repo: Path, init: Init) -> None:
    result = init()

    assert result.exit_code == 0, result.output
    for hook in ("guard.py", "quick-check.py"):
        assert (repo / ".lanes" / "hooks" / hook).stat().st_mode & 0o111, hook
    assert frontmatter(repo / ".claude" / "agents" / "reviewer.md")["name"] == "reviewer"
    implementer = frontmatter(repo / ".claude" / "agents" / "implementer.md")
    assert implementer["name"] == "implementer"
    [guard] = implementer["hooks"]["PreToolUse"]
    assert {"Bash", "Edit", "Write"} <= set(guard["matcher"].split("|"))
    assert guard["hooks"] == [
        {"type": "command", "command": '"$CLAUDE_PROJECT_DIR"/.lanes/hooks/guard.py', "onFailure": "block"}
    ]
    [quick_check] = implementer["hooks"]["Stop"]
    assert quick_check["hooks"][0]["command"] == '"$CLAUDE_PROJECT_DIR"/.lanes/hooks/quick-check.py'


def test_the_guard_blocks_when_its_script_is_missing(repo: Path, init: Init) -> None:
    init()
    [guard] = frontmatter(repo / ".claude" / "agents" / "implementer.md")["hooks"]["PreToolUse"][0]["hooks"]
    (repo / ".lanes" / "hooks" / "guard.py").unlink()

    missing = subprocess.run(guard["command"], shell=True, env={"CLAUDE_PROJECT_DIR": str(repo)}, input="{}", text=True)

    # Exit 2 would be a deliberate deny; any other failure blocks only through onFailure.
    assert missing.returncode not in (0, 2)
    assert guard["onFailure"] == "block"


def test_written_config_round_trips_through_the_config_model(repo: Path, init: Init) -> None:
    result = init(
        answers(
            tracker="markdown",
            test_command='uv run pytest -k "not slow"',
            lint_command="uv run ruff check",
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
        lint_command="uv run ruff check",
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


def unit(home: Path, name: str) -> str:
    return (home / ".config" / "systemd" / "user" / name).read_text()


def test_init_writes_and_enables_a_timer_that_ticks_every_repository_every_10_minutes(home: Path, init: Init) -> None:
    result = init()

    assert result.exit_code == 0, result.output
    timer = unit(home, "lanes-tick.timer")
    assert "OnCalendar=*:0/10" in timer
    assert "WantedBy=timers.target" in timer
    service = unit(home, "lanes-tick.service")
    assert "Type=oneshot" in service
    [exec_start] = [line.removeprefix("ExecStart=") for line in service.splitlines() if line.startswith("ExecStart=")]
    assert shlex.split(exec_start) == [sys.executable, "-m", "lanes", "tick", "--all"]
    assert systemctl_calls(home) == ["--user daemon-reload", "--user enable --now lanes-tick.timer"]


def test_the_service_runs_lanes_tick_all_with_the_path_init_ran_with(home: Path, init: Init, tmp_path: Path) -> None:
    init()
    service = unit(home, "lanes-tick.service")
    [exec_start] = [line.removeprefix("ExecStart=") for line in service.splitlines() if line.startswith("ExecStart=")]
    [environment] = [line.removeprefix("Environment=") for line in service.splitlines() if line.startswith("Environment=")]
    empty = tmp_path / "empty-config"

    ran = subprocess.run(
        shlex.split(exec_start),
        env={"HOME": str(home), "XDG_CONFIG_HOME": str(empty), "PYTHONPATH": str(Path(__file__).parent.parent / "src")},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert ran.returncode == 0, ran.stderr
    assert "no repository is registered" in ran.stdout
    assert shlex.split(environment) == [f"PATH={os.environ['PATH']}"]


def test_init_warns_and_succeeds_when_systemctl_cannot_enable_the_timer(
    home: Path, init: Init, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SYSTEMCTL_EXIT", "1")

    result = init()

    assert result.exit_code == 0, result.output
    assert "timer that runs lanes tick --all is written but not enabled" in result.output
    assert "systemctl --user enable --now lanes-tick.timer" in result.output
    assert (home / ".config" / "systemd" / "user" / "lanes-tick.timer").is_file()
