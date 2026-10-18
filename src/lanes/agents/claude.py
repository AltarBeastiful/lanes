"""The `Agents` port over the `claude` CLI, and what else Lanes needs to know about Claude Code."""

import json
import os
import re
import subprocess
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from lanes.agents import AgentError
from lanes.models import Session

_SESSIONS: TypeAdapter[list[Session]] = TypeAdapter(list[Session])
# `claude --bg` ends with `backgrounded · <short id> · <name>`, after a note when it started a copy.
_BACKGROUNDED = re.compile(r"^backgrounded · (\S+)", re.MULTILINE)


class ClaudeAgents:
    def dispatch(self, name: str, agent: str, prompt: str, model: str, cwd: Path) -> str:
        # The prompt goes last, and no option here is variadic: one that was would swallow it.
        return self._background(["--bg", "--agent", agent, "--model", model, "--name", name, prompt], cwd)

    def resume(self, session: Session, prompt: str, name: str) -> str:
        # Only the full session ID continues a session: a short one opens the resume picker, and the session sits blocked.
        return self._background(["--resume", session.session_id, "--bg", "--name", name, prompt], Path(session.cwd))

    def sessions(self) -> list[Session]:
        try:
            return _SESSIONS.validate_json(_claude("agents", "--json", "--all"))
        except ValidationError as error:
            raise AgentError(f"claude agents --json printed something unexpected: {error}") from None

    def _background(self, args: list[str], cwd: Path) -> str:
        printed = _claude(*args, cwd=cwd)
        short = _BACKGROUNDED.search(printed)
        if not short:
            raise AgentError(f"claude --bg printed no session: {printed.strip()}")
        for session in self.sessions():
            if session.id == short.group(1):
                return session.session_id
        raise AgentError(f"claude agents does not list session {short.group(1)}")


def _claude(*args: str, cwd: Path | None = None) -> str:
    try:
        done = subprocess.run(["claude", *args], cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        raise AgentError("Lanes needs the claude CLI on PATH") from None
    except OSError as error:
        raise AgentError(f"claude could not start in {cwd}: {error}") from None
    if done.returncode != 0:
        raise AgentError(done.stderr.strip() or f"claude {args[0]} exited {done.returncode}")
    return done.stdout


def workspace_trusted(repo: Path) -> bool:
    """Whether Claude Code's trust dialog was accepted for `repo` or a directory above it.

    A script-launched `claude --bg` exits with "Workspace not trusted" otherwise.
    """
    state = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home()) / ".claude.json"
    try:
        projects = json.loads(state.read_text(encoding="utf-8")).get("projects", {})
    except (OSError, ValueError):
        return False
    resolved = repo.resolve()
    return any(projects.get(str(path), {}).get("hasTrustDialogAccepted") is True for path in [resolved, *resolved.parents])
