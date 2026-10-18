"""Pydantic models: the single source for every structured shape."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    PositiveInt,
    StringConstraints,
    Tag,
    field_validator,
    model_validator,
)

Route = Literal["direct", "grill", "spec", "wayfinder"]
Tier = Literal["trivial", "small", "medium", "high"]


class TicketLanesSection(BaseModel):
    """The TOML block under a ticket's `## Lanes` heading."""

    model_config = ConfigDict(extra="forbid")

    route: Route
    tier: Tier
    produces: list[str]
    consumes: list[str]
    seams: list[str]
    demo: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


Host = Literal["github"]
Tracker = Literal["github", "markdown"]
Reviewer = Literal["codex", "claude-high", "claude-max", "claude-adversarial"]


class TierReviewers(BaseModel):
    """Who reviews a lane of each tier after its CI is green; an empty list means CI only."""

    model_config = ConfigDict(extra="forbid")

    trivial: list[Reviewer]
    small: list[Reviewer]
    medium: list[Reviewer]
    high: list[Reviewer]


class LanesConfig(BaseModel):
    """`.lanes/lanes.toml`, the committed configuration `lanes init` writes."""

    model_config = ConfigDict(extra="forbid")

    host: Host
    tracker: Tracker
    test_command: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    lint_command: Annotated[str, StringConstraints(strip_whitespace=True)] = ""
    max_lanes: PositiveInt
    codex_allowed: bool
    reviewers: TierReviewers

    @model_validator(mode="after")
    def _codex_reviews_only_when_allowed(self) -> Self:
        if not self.codex_allowed and any("codex" in names for _, names in self.reviewers):
            raise ValueError("codex is a reviewer but codex_allowed is false")
        return self


class Registry(BaseModel):
    """`~/.config/lanes/repos.toml`: every repository `lanes init` set up on this machine."""

    model_config = ConfigDict(extra="forbid")

    repos: list[str] = []


class _Event(BaseModel):
    """One record of the append-only log; `(event, version)` names its shape (ADR-0001)."""

    model_config = ConfigDict(extra="forbid")

    at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    ticket: str

    @field_validator("ticket")
    @classmethod
    def _without_leading_zeros(cls, ticket: str) -> str:
        """`012` and `12` are the same lane."""
        return str(int(ticket)) if ticket.isdigit() else ticket


class LaneDispatched(_Event):
    event: Literal["lane_dispatched"] = "lane_dispatched"
    version: Literal[1] = 1
    route: Route
    tier: Tier


class FixRound(_Event):
    event: Literal["fix_round"] = "fix_round"
    version: Literal[1] = 1


class FailureLoggedV1(_Event):
    """A defect caught at merge, before the event carried the ticket's route and tier."""

    event: Literal["failure_logged"] = "failure_logged"
    version: Literal[1] = 1
    what: str


class FailureLogged(_Event):
    """A defect caught at merge, with the route and tier the ticket had then; None only when upgraded from v1."""

    event: Literal["failure_logged"] = "failure_logged"
    version: Literal[2] = 2
    route: Route | None
    tier: Tier | None
    what: str


Event = LaneDispatched | FixRound | FailureLogged


def _event_tag(record: Any) -> str:
    if isinstance(record, dict):
        return f"{record.get('event')}/{record.get('version')}"
    return f"{record.event}/{record.version}"


LogRecord = Annotated[
    Annotated[LaneDispatched, Tag("lane_dispatched/1")]
    | Annotated[FixRound, Tag("fix_round/1")]
    | Annotated[FailureLoggedV1, Tag("failure_logged/1")]
    | Annotated[FailureLogged, Tag("failure_logged/2")],
    Discriminator(_event_tag),
]
"""Any record the log may hold, of any version."""
