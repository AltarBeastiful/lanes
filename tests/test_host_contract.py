"""Seam 3: the `HostAdapter` contract, run against every host.

The markdown host always runs. The GitHub host runs only when `LANES_GITHUB_SANDBOX` names a throwaway
repository (`owner/repo`) that `gh` may create and close issues and create labels in; it is left out of collection otherwise, so
CI never sees it as a skip.
"""

import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest

from lanes.hosts import HostAdapter, TicketNotFound
from lanes.hosts.github import GitHubHost
from lanes.hosts.markdown import MarkdownHost

SANDBOX = os.environ.get("LANES_GITHUB_SANDBOX", "")


class TicketWriter(Protocol):
    """Writes tickets the way a person would, outside the port under test."""

    def new_ticket(self, body: str, blocked_by: list[str] = ..., labels: list[str] = ...) -> str: ...


class MarkdownWriter:
    def __init__(self, root: Path) -> None:
        self._issues = root / ".scratch" / "feature" / "issues"
        self._issues.mkdir(parents=True)
        self._count = 0

    def new_ticket(self, body: str, blocked_by: list[str] = [], labels: list[str] = []) -> str:
        self._count += 1
        blockers = ", ".join(f"{int(ref):02}" for ref in blocked_by) or "None (can start immediately)"
        status = f"**Status:** {', '.join(labels)}\n\n" if labels else ""
        text = f"# {self._count:02}: A ticket\n\n**Blocked by:** {blockers}\n\n{status}{body}"
        (self._issues / f"{self._count:02}-a-ticket.md").write_text(text, encoding="utf-8")
        return str(self._count)


class GitHubWriter:
    def __init__(self, repo: str) -> None:
        self._repo = repo
        self.created: list[str] = []

    def new_ticket(self, body: str, blocked_by: list[str] = [], labels: list[str] = []) -> str:
        blockers = ["--blocked-by", ",".join(blocked_by)] if blocked_by else []
        labelled = [option for label in labels for option in ("--label", label)]
        url = self._gh("issue", "create", "--title", "lanes contract suite", "--body", body, *blockers, *labelled)
        ref = url.rstrip().rsplit("/", 1)[1]
        self.created.append(ref)
        return ref

    def close_all(self) -> None:
        for ref in self.created:
            self._gh("issue", "close", ref, "--reason", "not planned")

    def _gh(self, *args: str) -> str:
        return subprocess.run(["gh", *args, "--repo", self._repo], capture_output=True, text=True, check=True).stdout


@pytest.fixture(params=["markdown"] + (["github"] if SANDBOX else []))
def hosted(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[HostAdapter, TicketWriter]]:
    if request.param == "markdown":
        yield MarkdownHost(tmp_path), MarkdownWriter(tmp_path)
        return
    writer = GitHubWriter(SANDBOX)
    try:
        yield GitHubHost(SANDBOX), writer
    finally:
        writer.close_all()


@pytest.fixture
def host(hosted: tuple[HostAdapter, TicketWriter]) -> HostAdapter:
    return hosted[0]


@pytest.fixture
def writer(hosted: tuple[HostAdapter, TicketWriter]) -> TicketWriter:
    return hosted[1]


def test_a_ticket_body_is_read_back(host: HostAdapter, writer: TicketWriter) -> None:
    ref = writer.new_ticket("## What to build\n\nSomething — for Rémi.\n")

    assert "## What to build\n\nSomething — for Rémi." in host.read_ticket(ref)


def test_an_unknown_ticket_is_not_found(host: HostAdapter, writer: TicketWriter) -> None:
    writer.new_ticket("Something.\n")

    with pytest.raises(TicketNotFound):
        host.read_ticket("999999")


def test_blocking_edges_name_tickets_the_host_can_read(host: HostAdapter, writer: TicketWriter) -> None:
    first = writer.new_ticket("The first blocker.\n")
    second = writer.new_ticket("The second blocker.\n")
    blocked = writer.new_ticket("Waits for both.\n", blocked_by=[first, second])

    bodies = [host.read_ticket(ref) for ref in host.blocked_by(blocked)]

    assert len(bodies) == 2
    assert any("The first blocker." in body for body in bodies)
    assert any("The second blocker." in body for body in bodies)


def test_a_ticket_without_blockers_has_no_blocking_edges(host: HostAdapter, writer: TicketWriter) -> None:
    ref = writer.new_ticket("Starts now.\n")

    assert host.blocked_by(ref) == []


def test_an_added_label_joins_the_ticket_labels(host: HostAdapter, writer: TicketWriter) -> None:
    ref = writer.new_ticket("Something.\n", labels=["needs-triage"])

    host.add_label(ref, "ready-for-agent")

    assert sorted(host.labels(ref)) == ["needs-triage", "ready-for-agent"]


def test_adding_a_label_twice_keeps_one(host: HostAdapter, writer: TicketWriter) -> None:
    ref = writer.new_ticket("Something.\n")

    host.add_label(ref, "ready-for-agent")
    host.add_label(ref, "ready-for-agent")

    assert host.labels(ref) == ["ready-for-agent"]
    assert "Something." in host.read_ticket(ref)


def test_comments_are_read_back_in_order(host: HostAdapter, writer: TicketWriter) -> None:
    ref = writer.new_ticket("Something.\n")

    host.comment(ref, "First: `lanes check` passed.")
    host.comment(ref, "Second, over\ntwo lines.")

    assert host.comments(ref) == ["First: `lanes check` passed.", "Second, over\ntwo lines."]
    assert "Something." in host.read_ticket(ref)


def test_a_ticket_without_comments_has_none(host: HostAdapter, writer: TicketWriter) -> None:
    ref = writer.new_ticket("Something.\n")

    assert host.comments(ref) == []


def test_a_label_the_host_has_never_seen_is_added(host: HostAdapter, writer: TicketWriter) -> None:
    ref = writer.new_ticket("Something.\n")
    label = f"lanes-contract-{uuid.uuid4().hex[:8]}"

    host.add_label(ref, label)

    assert host.labels(ref) == [label]
