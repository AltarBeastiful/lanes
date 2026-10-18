"""The `Agents` port: the only way Lanes starts, resumes and lists the sessions lanes run in."""

from pathlib import Path
from typing import Protocol

from lanes.models import Session


class AgentError(Exception):
    """The agent refused or failed a call; the message says why, in its own words."""


class Agents(Protocol):
    def dispatch(self, name: str, agent: str, prompt: str, model: str, cwd: Path) -> str:
        """Start a background session named `name` in `cwd`, running `agent` on `prompt`; return its session ID."""
        ...

    def resume(self, session: Session, prompt: str, name: str) -> str:
        """Send `prompt` to `session` in the background under `name`; return the session ID it now runs as.

        That ID is new when the agent continued in a copy rather than in the session itself.
        """
        ...

    def sessions(self) -> list[Session]:
        """Every session the agent knows, running or finished."""
        ...
