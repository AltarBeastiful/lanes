"""The per-repository lock that keeps two ticks, and so their git calls, from overlapping."""

import fcntl
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def exclusive(repo: Path) -> Iterator[bool]:
    """Hold `repo`'s tick lock while the block runs; False, holding nothing, when another tick has it.

    The lock lives in the repository's common git directory, so a tick from any of its worktrees takes the same one,
    and the kernel drops it when the process holding it exits, however it exits.
    """
    with _lock_file(repo).open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except BlockingIOError:
            held = False
        yield held


def _lock_file(repo: Path) -> Path:
    found = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(found.stdout.strip()) / "lanes-tick.lock"
