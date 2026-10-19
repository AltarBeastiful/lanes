"""The fixture repository built as a real git repository, for the gates tests and the release smoke test.

See `examples/fixture-repo/README.md`.
"""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "examples" / "fixture-repo"
TEMPLATES = ROOT / "src" / "lanes" / "templates"

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
