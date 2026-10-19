"""The release smoke test: the fixture's canned tickets taken from `check` to a ready pull request by real agents.

Run by hand before tagging a release, never in CI (see the README's release checklist):

    uv run python tests/smoke.py --sandbox owner/repo

The sandbox is a throwaway GitHub repository with Actions on: its `main` is replaced by the fixture's on every run.
Sessions, worktrees and the state directory live under `--workdir`, which Claude Code's workspace trust must cover.
"""

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import click

from fixture import FIXTURE, commit_all, git
from lanes import install
from lanes.agents.claude import ClaudeAgents, workspace_trusted
from lanes.hosts.github import GitHubHost
from lanes.models import LanesConfig, Session, TierReviewers
from lanes.store.files import FileStore, state_directory

Step = tuple[str, Callable[[], object]]

TICKETS = FIXTURE / "tickets"
LABELS = ("needs-triage", "ready-for-agent", "needs-human", "allow-config")
CONFIG = LanesConfig(
    host="github",
    tracker="github",
    # Under `uv run`, so that the lanes' quick checks run the runner in the project's environment, as the gates do.
    test_command="uv run pytest",
    max_lanes=2,
    codex_allowed=False,
    reviewers=TierReviewers(
        trivial=[],
        small=["claude-adversarial"],
        medium=["claude-adversarial", "claude-high"],
        high=["claude-adversarial", "claude-max"],
    ),
)
# Lanes run unattended, so nothing may wait on a permission prompt; the guard still denies config writes.
SETTINGS = '{\n  "permissions": {\n    "defaultMode": "auto"\n  }\n}\n'
MODEL = "sonnet"


def canned_tickets() -> list[tuple[str, str]]:
    """Each canned ticket's title and issue body, from `# <title>` and the text after it."""
    tickets = []
    for path in sorted(TICKETS.glob("*.md")):
        heading, body = path.read_text(encoding="utf-8").split("\n", 1)
        tickets.append((heading.removeprefix("# "), body.strip()))
    return tickets


class Failed(Exception):
    """A step saw something other than what a working Lanes produces; the message says what."""


def run(steps: Sequence[Step], cleanup: Callable[[], None]) -> int:
    """Run each step in turn, reporting it, and stop at the first that fails; clean up either way."""
    code = 1
    try:
        code = 0 if all(_step(name, step) for name, step in steps) else 1
    finally:
        # Even on Ctrl-C: a stopped run must not leave sessions working on the sandbox.
        if not _step("cleanup", cleanup):
            code = 1
    return code


def _step(name: str, step: Callable[[], object]) -> bool:
    try:
        step()
    except Exception as error:
        # Any error, not only a `Failed`, must still name the step it stopped.
        print(f"smoke failed at {name}: {error}", flush=True)
        return False
    print(f"{name}: ok", flush=True)
    return True


class Smoke:
    def __init__(self, sandbox: str, workdir: Path, timeout: float, interval: float) -> None:
        self.sandbox = sandbox
        self.workdir = workdir
        self.repo = workdir / "repo"
        self.timeout = timeout
        self.interval = interval
        self.agents = ClaudeAgents()
        # Ticket number to title, for the tickets this run opened.
        self.tickets: dict[str, str] = {}

    def steps(self) -> list[Step]:
        return [
            ("trust", self.trust),
            ("seed", self.seed),
            ("tickets", self.open_tickets),
            ("check", self.check),
            ("dispatch", self.dispatch),
            ("tick", self.tick),
        ]

    def trust(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        if not workspace_trusted(self.workdir):
            raise Failed(f"Claude Code's workspace trust has not been accepted for {self.workdir}: run `claude` there once")

    def seed(self) -> None:
        """Replace the sandbox's `main` with the fixture's, set up as `lanes init` would, clearing any earlier run."""
        self.clear()
        shutil.copytree(FIXTURE / "main", self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        # Not `lanes init`, which would register the sandbox for the timer and race this run's ticks.
        install.install(self.repo, CONFIG)
        (self.repo / ".claude" / "settings.json").write_text(SETTINGS, encoding="utf-8")
        (self.repo / ".gitignore").write_text(".venv/\n__pycache__/\n.lanes-blocked-on\n", encoding="utf-8")
        # The gates workflow runs `uv sync --locked`, so the fixture needs pytest in a lockfile.
        self._run(["uv", "add", "--quiet", "--dev", "--no-sync", "pytest"], self.repo)
        commit_all(self.repo, "The fixture, set up for Lanes")
        git(self.repo, "remote", "add", "origin", f"git@github.com:{self.sandbox}.git")
        git(self.repo, "push", "-q", "--force", "origin", "main")
        for label in LABELS:
            self._gh("label", "create", label, "--force", "--repo", self.sandbox)

    def open_tickets(self) -> None:
        for title, body in canned_tickets():
            url = self._gh(
                "issue", "create", "--title", title, "--body", body, "--label", "needs-triage", "--repo", self.sandbox
            )
            number = url.strip().rsplit("/", 1)[1]
            self.tickets[number] = title
            print(f"  #{number}: {title}", flush=True)

    def check(self) -> None:
        for number in self.tickets:
            print(self._lanes("check", number), end="", flush=True)
            labels = self._gh("issue", "view", number, "--json", "labels", "--jq", ".labels[].name", "--repo", self.sandbox)
            if "ready-for-agent" not in labels.split():
                raise Failed(f"#{number} passed lanes check but is not labelled ready-for-agent")

    def dispatch(self) -> None:
        # By hand until `lanes go` dispatches (#15): one worktree off origin/main and one T<n> session per ticket.
        git(self.repo, "fetch", "-q", "origin")
        for number, title in self.tickets.items():
            worktree = self.workdir / f"T{number}"
            git(self.repo, "worktree", "add", "-q", "-b", f"smoke-{number}", str(worktree), "origin/main")
            body = self._gh("issue", "view", number, "--json", "body", "--jq", ".body", "--repo", self.sandbox)
            prompt = f"Ticket #{number}: {title}\n\n{body}"
            session = self.agents.dispatch(f"T{number}", "implementer", prompt, MODEL, worktree)
            # The cache comes before the name when tick looks a lane up, so a T<n> session of another repository
            # never stands in for this one.
            FileStore(state_directory(self.repo)).remember_session(number, session)
            print(f"  T{number}: session {session}", flush=True)

    def tick(self) -> None:
        """Tick until every ticket's pull request is ready for review, as the timer would but more often."""
        deadline = time.monotonic() + self.timeout
        while True:
            print(self._lanes("tick"), end="", flush=True)
            lanes = self._lanes_by_ticket()
            for number in self.tickets:
                print(f"  T{number}: {lanes.get(number, 'no pull request')}", flush=True)
            if all(lanes.get(number, "").startswith("ready") for number in self.tickets):
                return
            if time.monotonic() > deadline:
                raise Failed(f"not every pull request was ready after {self.timeout / 60:.0f} minutes")
            time.sleep(self.interval)

    def _lanes_by_ticket(self) -> dict[str, str]:
        """Each ticket's open pull request, as `<draft|ready> #<n>`; raises when one was handed to a person."""
        found: dict[str, str] = {}
        # Through the host, so that a pull request belongs to a ticket exactly when tick thinks it does.
        for pr in GitHubHost(self.sandbox).pull_requests():
            if pr.ticket not in self.tickets:
                continue
            if "needs-human" in pr.labels:
                raise Failed(f"T{pr.ticket}'s pull request #{pr.ref} was labelled needs-human")
            found[pr.ticket] = f"{'draft' if pr.draft else 'ready'} #{pr.ref}"
        return found

    def keep(self) -> None:
        print(f"  kept the sessions, pull requests and branches in {self.sandbox} and {self.workdir}; the next run clears them")

    def cleanup(self) -> None:
        self.clear()
        left = [f"session {session.id or session.session_id}" for session in self._sessions()]
        left += [f"pull request #{pr}" for pr in self._open_prs()]
        left += [f"branch {branch}" for branch in self._git_heads() if branch != "main"]
        if left:
            raise Failed(f"left behind: {', '.join(left)}")

    def clear(self) -> None:
        """Remove every session, pull request, branch and ticket a run leaves, carrying on past what fails."""
        errors: list[str] = []
        for session in self._sessions():
            short = session.id or session.session_id
            # Stopping a session that has already exited fails; removing it is what counts.
            subprocess.run(["claude", "stop", short], capture_output=True)
            errors += self._try(["claude", "rm", short])
        for pr in self._open_prs():
            errors += self._try(["gh", "pr", "close", pr, "--delete-branch", "--repo", self.sandbox])
        for branch in self._git_heads():
            if branch != "main":
                errors += self._try(["gh", "api", "-X", "DELETE", f"repos/{self.sandbox}/git/refs/heads/{branch}"])
        titles = {title for title, _ in canned_tickets()}
        issues = self._gh(
            "issue", "list", "--state", "open", "--json", "number,title", "--jq", '.[] | "\\(.number) \\(.title)"',
            "--repo", self.sandbox,
        )  # fmt: skip
        for line in issues.splitlines():
            number, title = line.split(" ", 1)
            if title in titles:
                errors += self._try(["gh", "issue", "close", number, "--reason", "not planned", "--repo", self.sandbox])
        for child in self.workdir.iterdir() if self.workdir.is_dir() else []:
            shutil.rmtree(child)
        if errors:
            raise Failed("; ".join(errors))

    def _sessions(self) -> list[Session]:
        return [session for session in self.agents.sessions() if Path(session.cwd).is_relative_to(self.workdir)]

    def _open_prs(self) -> list[str]:
        return self._gh("pr", "list", "--state", "open", "--json", "number", "--jq", ".[].number", "--repo", self.sandbox).split()

    def _git_heads(self) -> list[str]:
        heads = self._run(["git", "ls-remote", "--heads", f"git@github.com:{self.sandbox}.git"], None)
        return [line.split("refs/heads/", 1)[1] for line in heads.splitlines()]

    def _lanes(self, *args: str) -> str:
        """Run this checkout's `lanes` in the sandbox clone."""
        done = subprocess.run([sys.executable, "-m", "lanes", *args], cwd=self.repo, capture_output=True, text=True)
        output = "".join(f"  {line}\n" for line in (done.stdout + done.stderr).splitlines())
        if done.returncode != 0:
            raise Failed(f"lanes {' '.join(args)} exited {done.returncode}:\n{output}")
        return output

    def _gh(self, *args: str) -> str:
        return self._run(["gh", *args], None)

    def _try(self, command: list[str]) -> list[str]:
        try:
            self._run(command, None)
        except Failed as error:
            return [str(error)]
        return []

    @staticmethod
    def _run(command: list[str], cwd: Path | None) -> str:
        done = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
        if done.returncode != 0:
            raise Failed(f"{' '.join(command[:3])} exited {done.returncode}: {done.stderr.strip()}")
        return done.stdout


@click.command()
@click.option("--sandbox", envvar="LANES_GITHUB_SANDBOX", required=True, help="Throwaway GitHub repository, owner/repo.")
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "lanes" / "smoke",
    show_default=True,
    help="Where the sandbox clone, worktrees and state directory go; Claude Code must trust it.",
)
@click.option("--timeout", type=float, default=60, show_default=True, help="Minutes to wait for both ready pull requests.")
@click.option("--interval", type=float, default=60, show_default=True, help="Seconds between ticks.")
@click.option("--keep", is_flag=True, help="Leave the sessions, pull requests and branches for a look; the next run clears them.")
def main(sandbox: str, workdir: Path, timeout: float, interval: float, keep: bool) -> None:
    """Take the fixture's two canned tickets from `check` to a ready pull request with real agents and reviewers."""
    workdir = workdir.resolve()
    # The state directory goes under the work directory, for the store here and the `lanes` runs alike, so that it
    # never mixes with a real repository's and cleanup removes it.
    os.environ["XDG_STATE_HOME"] = str(workdir / "state")
    smoke = Smoke(sandbox, workdir, timeout * 60, interval)
    sys.exit(run(smoke.steps(), cleanup=smoke.keep if keep else smoke.cleanup))


if __name__ == "__main__":
    main()
