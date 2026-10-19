"""The systemd user timer that runs `lanes tick --all` every 10 minutes."""

import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import jinja2

TEMPLATES = files("lanes") / "templates" / "systemd"
TIMER = "lanes-tick.timer"


class TimerError(Exception):
    """systemctl could not reload or enable the timer."""


def units_directory() -> Path:
    """Where systemd looks for user units: `systemd/user` under `$XDG_CONFIG_HOME`, or `~/.config` when unset."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "systemd" / "user"


def units() -> dict[str, str]:
    """The service that ticks every registered repository with this Python's Lanes, and the timer that starts it."""
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    environment.filters["systemd"] = _quoted
    environment.filters["systemd_command"] = _quoted_command
    service = (TEMPLATES / "lanes-tick.service.j2").read_text(encoding="utf-8")
    return {
        "lanes-tick.service": environment.from_string(service).render(
            path=f"PATH={os.environ.get('PATH', '')}",
            command=[sys.executable, "-m", "lanes", "tick", "--all"],
        ),
        TIMER: (TEMPLATES / TIMER).read_text(encoding="utf-8"),
    }


def install() -> None:
    """Write the units, overwriting what is there, and enable and start the timer."""
    directory = units_directory()
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in units().items():
        (directory / name).write_text(text, encoding="utf-8")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", TIMER)


def _systemctl(*args: str) -> None:
    try:
        done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    except OSError as error:
        raise TimerError(f"systemctl could not start: {error}") from None
    if done.returncode != 0:
        raise TimerError(f"systemctl --user {' '.join(args)} exited {done.returncode}: {done.stderr.strip()}")


def _quoted(word: str) -> str:
    """One word of a unit file's command line or assignment, quoted so systemd reads it back as it is."""
    escaped = word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _quoted_command(word: str) -> str:
    """One word of `ExecStart`, where systemd also expands `$NAME`, so a `$` is doubled to stay itself."""
    return _quoted(word.replace("$", "$$"))
