"""The `LaneStore` port: the only way Lanes touches its state directory (ADR-0001)."""

from typing import Protocol

from lanes.models import (
    Event,
    FailureLogged,
    FailureLoggedV1,
    FixRound,
    FixRoundV1,
    LaneDispatched,
    LaneDispatchedV1,
    LogRecord,
)


class LaneStore(Protocol):
    def append(self, event: Event) -> None:
        """Add `event` to the end of the log."""
        ...

    def events(self) -> list[Event]:
        """Every logged event in the order it was appended, older versions upgraded to the current ones."""
        ...

    def session(self, ticket: str) -> str | None:
        """The session ID the lane for `ticket` last ran in, if the cache knows it."""
        ...

    def remember_session(self, ticket: str, session_id: str) -> None:
        """Map the lane for `ticket` to `session_id`, replacing any earlier one."""
        ...


def upgrade(records: list[LogRecord]) -> list[Event]:
    """Records of any version as current events, in the same order."""
    return [_upgrade(record) for record in records]


def _upgrade(record: LogRecord) -> Event:
    if isinstance(record, FailureLoggedV1):
        # v1 never knew the route and tier; the report takes them from the lane's dispatch instead.
        return FailureLogged(at=record.at, ticket=record.ticket, route=None, tier=None, what=record.what)
    if isinstance(record, FixRoundV1):
        return FixRound(at=record.at, ticket=record.ticket, commit=None)
    if isinstance(record, LaneDispatchedV1):
        # No v1 lane was started with an override: `lanes go`, which takes them, logs v2.
        return LaneDispatched(at=record.at, ticket=record.ticket, route=record.route, tier=record.tier)
    return record
