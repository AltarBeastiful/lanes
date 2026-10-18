"""Offline host: one markdown file per ticket under `.scratch/<feature>/issues/`."""

import re
from pathlib import Path

from lanes.hosts import HostError, TicketNotFound
from lanes.models import PullRequest

# `**Blocked by:** 01, 02` near the top, or a `## Blocked by` section listing them.
_BLOCKED_LINE = re.compile(r"^\**Blocked by:\**[ \t]*(.*)$", re.MULTILINE | re.IGNORECASE)
_BLOCKED_SECTION = re.compile(r"^#{2,6} +Blocked by\s*:?[ \t]*$(.*?)(?=^#{1,6} |\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE)
# The triage state, as in the local tracker convention: `**Status:** ready-for-agent`, comma-separated here.
_STATUS_LINE = re.compile(r"^(\**Status:\**)[ \t]*(.*)$", re.MULTILINE | re.IGNORECASE)
_TITLE_LINE = re.compile(r"^# .*\n", re.MULTILINE)
# Comments follow the ticket, each after this marker, and are not part of what `read_ticket` returns.
_COMMENT = "\n\n<!-- lanes:comment -->\n"
# Each blocker leads with its number; "None (can start immediately)" names none.
_BLOCKER = re.compile(r"(?:^|,)[ \t\-*]*#?(\d+)", re.MULTILINE)


class MarkdownHost:
    def __init__(self, root: Path) -> None:
        self._root = root

    def read_ticket(self, ref: str) -> str:
        """Read a ticket by its path, or by its number (`1`, `01`) when that is unambiguous."""
        return self._parts(ref)[0]

    def blocked_by(self, ref: str) -> list[str]:
        body = self.read_ticket(ref)
        listed = "\n".join(match.group(1) for pattern in (_BLOCKED_LINE, _BLOCKED_SECTION) for match in pattern.finditer(body))
        return [str(int(number)) for number in _BLOCKER.findall(listed)]

    def labels(self, ref: str) -> list[str]:
        status = _STATUS_LINE.search(self.read_ticket(ref))
        return [label.strip() for label in status.group(2).split(",") if label.strip()] if status else []

    def add_label(self, ref: str, label: str) -> None:
        labels = self.labels(ref)
        if label not in labels:
            self._write_labels(ref, [*labels, label])

    def remove_label(self, ref: str, label: str) -> None:
        labels = self.labels(ref)
        if label in labels:
            self._write_labels(ref, [kept for kept in labels if kept != label])

    def _write_labels(self, ref: str, labels: list[str]) -> None:
        path = self._path(ref)
        body = path.read_text(encoding="utf-8")
        value = ", ".join(labels)
        status = _STATUS_LINE.search(body, 0, len(self._parts(ref)[0]))
        if status:
            body = body[: status.start()] + f"{status.group(1)} {value}" + body[status.end() :]
        else:
            # A ticket without a Status line gets one under its title, as the local template places it.
            title = _TITLE_LINE.search(body)
            at = title.end() if title else 0
            body = body[:at] + ("\n" if title else "") + f"**Status:** {value}\n\n" + body[at:].lstrip("\n")
        path.write_text(body, encoding="utf-8")

    def comment(self, ref: str, text: str) -> None:
        with self._path(ref).open("a", encoding="utf-8") as ticket:
            ticket.write(f"{_COMMENT}{text}\n")

    def comments(self, ref: str) -> list[str]:
        return [text.removesuffix("\n") for text in self._parts(ref)[1:]]

    def pull_requests(self) -> list[PullRequest]:
        """Markdown tickets live offline, with no pull requests."""
        return []

    def ci_log(self, ref: str) -> str:
        raise HostError("markdown tickets have no pull requests")

    def diff(self, ref: str) -> str:
        raise HostError("markdown tickets have no pull requests")

    def label_pull_request(self, ref: str, label: str) -> None:
        raise HostError("markdown tickets have no pull requests")

    def update_branch(self, ref: str) -> bool:
        raise HostError("markdown tickets have no pull requests")

    def mark_ready(self, ref: str) -> None:
        raise HostError("markdown tickets have no pull requests")

    def request_review(self, ref: str) -> None:
        raise HostError("markdown tickets have no pull requests")

    def _parts(self, ref: str) -> list[str]:
        """The ticket file split into the ticket itself and each comment after it."""
        return self._path(ref).read_text(encoding="utf-8").split(_COMMENT)

    def _path(self, ref: str) -> Path:
        path = self._root / ref
        return path if path.is_file() else self._find_by_number(ref)

    def _find_by_number(self, ref: str) -> Path:
        if not ref.isdigit():
            raise TicketNotFound(f"no ticket file at {ref}")
        matches = [path for path in self._root.glob(".scratch/*/issues/*.md") if _number(path) == int(ref)]
        if len(matches) != 1:
            found = ", ".join(str(path.relative_to(self._root)) for path in sorted(matches))
            raise TicketNotFound(
                f"ticket {ref} matches {found}; pass its path" if matches else f"no ticket numbered {ref} in .scratch/"
            )
        return matches[0]


def _number(path: Path) -> int | None:
    """The `NN` in `<NN>-<slug>.md`."""
    prefix = path.name.split("-", 1)[0]
    return int(prefix) if prefix.isdigit() else None
