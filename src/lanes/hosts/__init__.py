"""The `HostAdapter` port: the only way Lanes calls the host (ADR-0001)."""

from typing import Protocol

from lanes.models import PullRequest


class HostError(Exception):
    """The host refused or failed a call; the message says why, in the host's words."""


class TicketNotFound(HostError):
    pass


class HostAdapter(Protocol):
    """A ticket is named by a ref: its number, or whatever else the host says names it (a path, a URL)."""

    def read_ticket(self, ref: str) -> str:
        """Return the body of the ticket named by `ref`, or raise `TicketNotFound`."""
        ...

    def blocked_by(self, ref: str) -> list[str]:
        """The refs of every ticket that blocks `ref`, open or closed, each one `read_ticket` accepts.

        A blocker that lives on the same host under another project, such as another GitHub repository, is named by
        its URL rather than its number.
        """
        ...

    def labels(self, ref: str) -> list[str]:
        """The labels on the ticket `ref`."""
        ...

    def add_label(self, ref: str, label: str) -> None:
        """Put `label` on the ticket `ref`, keeping its other labels; a label it already has stays once."""
        ...

    def remove_label(self, ref: str, label: str) -> None:
        """Take `label` off the ticket `ref`, keeping its other labels; a label it doesn't have is no error."""
        ...

    def comment(self, ref: str, text: str) -> None:
        """Add `text` as a comment on the ticket `ref`."""
        ...

    def comments(self, ref: str) -> list[str]:
        """The comments on the ticket `ref`, oldest first."""
        ...

    def pull_requests(self) -> list[PullRequest]:
        """Every open pull request whose body closes a ticket (`Closes #<n>`): the durable anchor of a lane."""
        ...

    def ci_log(self, ref: str) -> str:
        """The output of the failed CI checks on the pull request `ref`'s latest runs, as plain lines."""
        ...

    def label_pull_request(self, ref: str, label: str) -> None:
        """Put `label` on the pull request `ref`, keeping its other labels."""
        ...
