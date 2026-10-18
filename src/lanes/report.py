"""`lanes report`: first-pass acceptance, fix rounds and defects caught at merge, per route and tier."""

from collections import Counter
from typing import get_args

from lanes.models import Event, FailureLogged, FixRound, LaneDispatched, Route, Tier

# How many lanes the report names as worth a `/retro`.
_WORST = 5
_ORDER = {name: i for i, name in enumerate([*get_args(Route), *get_args(Tier)])}


def render(events: list[Event]) -> str:
    if not events:
        return "No events logged yet."
    dispatched = {event.ticket: event for event in events if isinstance(event, LaneDispatched)}
    rounds = Counter(event.ticket for event in events if isinstance(event, FixRound))
    failures = Counter(event.ticket for event in events if isinstance(event, FailureLogged))
    defects = Counter(
        _class(dispatched[event.ticket] if event.ticket in dispatched else event)
        for event in events
        if isinstance(event, FailureLogged)
    )
    lanes: dict[tuple[str, str], list[str]] = {}
    for ticket, lane in dispatched.items():
        lanes.setdefault(_class(lane), []).append(ticket)

    rows: list[tuple[str, ...]] = [("route", "tier", "lanes", "first pass", "fix rounds/ticket", "defects at merge")]
    for key in sorted(lanes.keys() | defects.keys(), key=lambda key: [_ORDER.get(part, len(_ORDER)) for part in key]):
        tickets = lanes.get(key, [])
        first_pass = sum(1 for ticket in tickets if not rounds[ticket])
        per_ticket = sum(rounds[ticket] for ticket in tickets) / len(tickets) if tickets else None
        rows.append((
            *key,
            str(len(tickets)),
            f"{first_pass / len(tickets):.0%}" if tickets else "-",
            f"{per_ticket:.1f}" if per_ticket is not None else "-",
            str(defects[key]),
        ))
    lines = _table(rows)

    worst = sorted(rounds.keys() | failures.keys(), key=lambda ticket: (-(rounds[ticket] + failures[ticket]), ticket))
    if worst:
        lines += ["", "Most fix rounds or failures:"]
        for ticket in worst[:_WORST]:
            counts = f"{_count(rounds[ticket], 'fix round')}, {_count(failures[ticket], 'failure')}"
            lines.append(f"  {_lane(ticket)}: {counts}")
    return "\n".join(lines)


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    """Route and tier left-aligned, numbers right-aligned."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.ljust(w) if i < 2 else cell.rjust(w) for i, (cell, w) in enumerate(zip(row, widths))) for row in rows]


def _class(event: LaneDispatched | FailureLogged) -> tuple[str, str]:
    return event.route or "unknown", event.tier or "unknown"


def _lane(ticket: str) -> str:
    return f"T{ticket}" if ticket.isdigit() else ticket


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")
