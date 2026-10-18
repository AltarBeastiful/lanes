"""The state directory as plain files under `$XDG_STATE_HOME/lanes/`, outside the repository."""

import hashlib
import os
from pathlib import Path

from pydantic import TypeAdapter

from lanes.models import Event, LogRecord
from lanes.store import upgrade

_RECORD: TypeAdapter[LogRecord] = TypeAdapter(LogRecord)


def state_directory(repo: Path) -> Path:
    """One directory per repository, named after it and told apart by a hash of its path."""
    state_home = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    repo = repo.resolve()
    digest = hashlib.sha256(str(repo).encode()).hexdigest()[:12]
    return Path(state_home) / "lanes" / f"{repo.name}-{digest}"


class FileStore:
    def __init__(self, directory: Path) -> None:
        self._log = directory / "events.jsonl"

    def append(self, event: Event) -> None:
        self._log.parent.mkdir(parents=True, exist_ok=True)
        with self._log.open("a", encoding="utf-8") as log:
            log.write(event.model_dump_json() + "\n")

    def events(self) -> list[Event]:
        if not self._log.exists():
            return []
        lines = self._log.read_text(encoding="utf-8").splitlines()
        records = [_RECORD.validate_json(line) for line in lines if line.strip()]
        return upgrade(records)
