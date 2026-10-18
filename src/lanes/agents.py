"""What Lanes needs to know about Claude Code, the agent it dispatches lanes to."""

import json
import os
from pathlib import Path


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
