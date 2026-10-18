"""GitHub host over the `gh` CLI: tickets are issues, blocking edges are GitHub's native issue dependencies."""

import json
import re
import subprocess
from typing import Any

from lanes.hosts import HostError, TicketNotFound

_NOT_FOUND = "Could not resolve to an issue"
_LABEL_NOT_FOUND = re.compile(r"'.*' not found$")
_ISSUE_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)")


class GitHubHost:
    def __init__(self, repo: str | None = None) -> None:
        """`repo` is `owner/repo`; without it, the repository `gh` finds from the working directory."""
        self._repo = repo

    def read_ticket(self, ref: str) -> str:
        body: str = self._issue(ref, "body")["body"]
        return body

    def blocked_by(self, ref: str) -> list[str]:
        """GitHub's native issue dependencies; a blocker in another repository is named by its URL."""
        repo, number = self._locate(ref)
        jq = ".[] | [.number, .repository_url, .html_url]"
        rows = self._gh("api", "--paginate", f"repos/{repo}/issues/{number}/dependencies/blocked_by", "--jq", jq)
        here = self._here()
        return [
            str(blocker) if repository.endswith(f"/repos/{here}") else url
            for blocker, repository, url in map(json.loads, rows.splitlines())
        ]

    def labels(self, ref: str) -> list[str]:
        return [label["name"] for label in self._issue(ref, "labels")["labels"]]

    def add_label(self, ref: str, label: str) -> None:
        """Creates the label first when the repository doesn't have it yet."""
        repo, number = self._locate(ref)
        try:
            self._gh("issue", "edit", number, "--repo", repo, "--add-label", label)
        except HostError as error:
            if not _LABEL_NOT_FOUND.search(str(error).splitlines()[0]):
                raise
            self._gh("label", "create", label, "--repo", repo)
            self._gh("issue", "edit", number, "--repo", repo, "--add-label", label)

    def remove_label(self, ref: str, label: str) -> None:
        # gh refuses to remove a label the repository doesn't have, so only remove one the issue carries.
        if label in self.labels(ref):
            repo, number = self._locate(ref)
            self._gh("issue", "edit", number, "--repo", repo, "--remove-label", label)

    def comment(self, ref: str, text: str) -> None:
        repo, number = self._locate(ref)
        self._gh("issue", "comment", number, "--repo", repo, "--body-file", "-", stdin=text)

    def comments(self, ref: str) -> list[str]:
        return [comment["body"] for comment in self._issue(ref, "comments")["comments"]]

    def _issue(self, ref: str, fields: str) -> dict[str, Any]:
        repo, number = self._locate(ref)
        found: dict[str, Any] = json.loads(self._gh("issue", "view", number, "--repo", repo, "--json", fields))
        return found

    def _locate(self, ref: str) -> tuple[str, str]:
        """The `owner/repo` and number of an issue named by number (`12`, `#12`) or by URL."""
        url = _ISSUE_URL.fullmatch(ref)
        if url:
            return url.group(1), url.group(2)
        number = ref.removeprefix("#")
        if not number.isdigit():
            raise TicketNotFound(f"{ref} is neither an issue number nor an issue URL")
        return self._here(), number

    def _here(self) -> str:
        if self._repo is None:
            self._repo = self._gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()
        return self._repo

    def _gh(self, *args: str, stdin: str | None = None) -> str:
        try:
            done = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True)
        except FileNotFoundError:
            raise HostError("the GitHub host needs the gh CLI on PATH") from None
        if done.returncode != 0:
            message = done.stderr.strip() or f"gh {' '.join(args[:2])} exited {done.returncode}"
            raise (TicketNotFound if _NOT_FOUND in message else HostError)(message)
        return done.stdout
