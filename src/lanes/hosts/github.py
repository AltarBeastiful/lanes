"""GitHub host over the `gh` CLI: tickets are issues, blocking edges are GitHub's native issue dependencies."""

import json
import re
import subprocess
from typing import Any

from lanes.hosts import HostError, MergeConflict, TicketNotFound
from lanes.models import CI, PullRequest

_NOT_FOUND = "Could not resolve to an issue"
# What `gh pr update-branch` prints when there is nothing to update, and when it can't for conflicts.
_UP_TO_DATE = "PR branch already up-to-date"
_CONFLICTS = "Cannot update PR branch due to conflicts"
_LABEL_NOT_FOUND = re.compile(r"'.*' not found$")
_ISSUE_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)")
_CLOSES = re.compile(r"^Closes #(\d+)\b", re.MULTILINE | re.IGNORECASE)
_JOB_URL = re.compile(r"/actions/runs/\d+/job/(\d+)")
_CHECKS = "statusCheckRollup"
_RED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ERROR"}
_GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# `gh run view --log-failed` puts the job, the step and a timestamp before each line.
_LOG_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t\ufeff?(?:\d{4}-\d\d-\d\dT[\d:.]+Z ?)?")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


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
        self._label("issue", number, repo, label)

    def _label(self, kind: str, number: str, repo: str, label: str) -> None:
        try:
            self._gh(kind, "edit", number, "--repo", repo, "--add-label", label)
        except HostError as error:
            if not _LABEL_NOT_FOUND.search(str(error).splitlines()[0]):
                raise
            self._gh("label", "create", label, "--repo", repo)
            self._gh(kind, "edit", number, "--repo", repo, "--add-label", label)

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

    def pull_requests(self) -> list[PullRequest]:
        fields = f"number,body,headRefName,headRefOid,isDraft,labels,{_CHECKS}"
        listed = json.loads(self._gh("pr", "list", "--repo", self._here(), "--state", "open", "--limit", "200", "--json", fields))
        return [
            PullRequest(
                ref=str(pr["number"]),
                ticket=closes.group(1),
                branch=pr["headRefName"],
                head=pr["headRefOid"],
                ci=_ci(_latest(pr[_CHECKS])),
                labels=[label["name"] for label in pr["labels"]],
                draft=pr["isDraft"],
            )
            for pr in listed
            if (closes := _CLOSES.search(pr["body"] or ""))
        ]

    def ci_log(self, ref: str) -> str:
        repo, number = self._here(), ref.removeprefix("#")
        checks = json.loads(self._gh("pr", "view", number, "--repo", repo, "--json", _CHECKS))[_CHECKS]
        latest = _latest(checks)
        red = [check for check in latest if _state(check) in _RED]
        # A failed job's log is cut to its failed steps; a green one has none, so its whole log is read.
        flag = "--log-failed" if red else "--log"
        jobs = [job.group(1) for check in red or latest if (job := _JOB_URL.search(_url(check)))]
        return "".join(_plain(self._gh("run", "view", "--repo", repo, "--job", job, flag)) for job in jobs)

    def diff(self, ref: str) -> str:
        return self._gh("pr", "diff", ref.removeprefix("#"), "--repo", self._here())

    def label_pull_request(self, ref: str, label: str) -> None:
        self._label("pr", ref.removeprefix("#"), self._here(), label)

    def update_branch(self, ref: str) -> bool:
        """Merges the base into the branch, as GitHub's "Update branch" button does."""
        try:
            printed = self._gh("pr", "update-branch", ref.removeprefix("#"), "--repo", self._here())
        except HostError as error:
            if _CONFLICTS not in str(error):
                raise
            raise MergeConflict(str(error)) from None
        return _UP_TO_DATE not in printed

    def mark_ready(self, ref: str) -> None:
        self._gh("pr", "ready", ref.removeprefix("#"), "--repo", self._here())

    def request_review(self, ref: str) -> None:
        """GitHub refuses a review request from the pull request's author, so it is assigned to them instead."""
        repo, number = self._here(), ref.removeprefix("#")
        author = json.loads(self._gh("pr", "view", number, "--repo", repo, "--json", "author"))["author"]["login"]
        me = json.loads(self._gh("api", "user"))["login"]
        asked = ["--add-assignee", "@me"] if author == me else ["--add-reviewer", me]
        self._gh("pr", "edit", number, "--repo", repo, *asked)

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


def _latest(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The newest run of each check: an older failed run a re-run has since passed no longer counts."""
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for check in sorted(checks, key=lambda check: check.get("startedAt") or ""):
        newest[check.get("workflowName") or "", check.get("name") or check.get("context") or ""] = check
    return list(newest.values())


def _state(check: dict[str, Any]) -> str:
    """A check run's conclusion, or a commit status's state; empty while a check run is still going."""
    return str(check.get("conclusion") or check.get("state") or "")


def _url(check: dict[str, Any]) -> str:
    return str(check.get("detailsUrl") or check.get("targetUrl") or "")


def _ci(checks: list[dict[str, Any]]) -> CI:
    states = [_state(check) for check in checks]
    if any(state in _RED for state in states):
        return "failed"
    return "passed" if states and all(state in _GREEN for state in states) else "pending"


def _plain(log: str) -> str:
    """The lines a failed job printed, without GitHub's prefixes, colours, step headers or annotations."""
    lines: list[str] = []
    in_group = False
    for raw in log.splitlines():
        line = _ANSI.sub("", _LOG_PREFIX.sub("", raw, count=1))
        if line.startswith("##[group]"):
            in_group = True
        elif line.startswith("##[endgroup]"):
            in_group = False
        elif not in_group and not line.startswith("##["):
            lines.append(line + "\n")
    return "".join(lines)
