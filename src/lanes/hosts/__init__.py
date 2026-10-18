"""The `HostAdapter` port: the only way Lanes calls the host (ADR-0001)."""

from typing import Protocol


class TicketNotFound(Exception):
    pass


class HostAdapter(Protocol):
    def read_ticket(self, ref: str) -> str:
        """Return the body of the ticket named by `ref`, or raise `TicketNotFound`."""
        ...
