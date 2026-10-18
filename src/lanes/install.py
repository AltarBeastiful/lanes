"""Render the templates `lanes init` commits into a target repository."""

import shlex
from importlib.resources import files
from pathlib import Path
from typing import Any

import jinja2
import tomli_w

from lanes.models import LanesConfig

TEMPLATES = files("lanes") / "templates"
# Template, where it goes in the repository, and whether it is executable. A
# `.j2` template is rendered with jinja2; any other file is copied as it is.
INSTALLED = [
    ("config/lanes.toml.j2", ".lanes/lanes.toml", False),
    ("config/hotspots.txt", ".lanes/hotspots.txt", False),
    ("gates/gates.py", ".lanes/gates.py", False),
    ("runner/pytest", ".lanes/runner", True),
    ("gates/gates.yml", ".github/workflows/gates.yml", False),
    ("agents/implementer.md", ".claude/agents/implementer.md", False),
    ("agents/reviewer.md", ".claude/agents/reviewer.md", False),
    ("hooks/guard.py", ".lanes/hooks/guard.py", True),
    ("hooks/quick-check.py", ".lanes/hooks/quick-check.py", True),
]

RUNNER_CONTRACT = "https://github.com/AltarBeastiful/lanes/blob/main/docs/adr/0002-runner-script.md"
# Wrappers that still end in pytest. The gates workflow runs under uv, so other launchers are not offered.
_LAUNCHERS = [["uv", "run"], ["python", "-m"], ["python3", "-m"]]


def runs_pytest(test_command: str) -> bool:
    """Whether the pytest runner Lanes ships can stand in for `test_command`."""
    try:
        words = shlex.split(test_command)
    except ValueError:
        return False
    for launcher in _LAUNCHERS:
        if words[: len(launcher)] == launcher:
            words = words[len(launcher) :]
    return words[:1] == ["pytest"]


def rendered(config: LanesConfig) -> dict[str, str]:
    """Each installed file's path in the repository and its content."""
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    environment.filters["toml"] = _toml
    found = {}
    for template, path, _ in INSTALLED:
        text = (TEMPLATES / template).read_text(encoding="utf-8")
        found[path] = environment.from_string(text).render(config=config) if template.endswith(".j2") else text
    return found


def differing(repo: Path, config: LanesConfig) -> list[str]:
    """Installed files already in `repo` whose content `install` would change."""
    return [
        path
        for path, text in rendered(config).items()
        if (repo / path).exists() and (repo / path).read_text(encoding="utf-8") != text
    ]


def install(repo: Path, config: LanesConfig) -> None:
    """Write every installed file into `repo`, overwriting what is there."""
    executable = {path for _, path, is_executable in INSTALLED if is_executable}
    for path, text in rendered(config).items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if path in executable:
            target.chmod(0o755)


def _toml(value: Any) -> str:
    """One value as TOML, through tomli-w so that any string comes out valid; a list stays on one line."""
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    return tomli_w.dumps({"_": value}).removeprefix("_ = ").rstrip("\n")
