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

    def for_tier(self, tier: Tier) -> list[Reviewer]:
        reviewers: list[Reviewer] = getattr(self, tier)
        return reviewers


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


class FixRoundV1(_Event):
    """A fix round, before the event carried the commit it was spent on."""

    event: Literal["fix_round"] = "fix_round"
    version: Literal[1] = 1


class FixRound(_Event):
    """One resume of a lane, spent on the head commit CI or a verdict found wanting; None only when upgraded from v1."""

    event: Literal["fix_round"] = "fix_round"
    version: Literal[2] = 2
    commit: str | None


class Finding(BaseModel):
    """One problem a reviewer found, at a line of the commit it reviewed."""

    model_config = ConfigDict(extra="forbid")

    file: str
    line: int
    message: str


class Verdict(BaseModel):
    """One reviewer's result for one commit: what blocks the merge, and nits that never do.

    The JSON schema reviewers fill is generated from this model, so every field is required and none is extra.
    """

    model_config = ConfigDict(extra="forbid")

    blocking: list[Finding]
    nits: list[str]


class VerdictGiven(_Event):
    """A reviewer's verdict on a lane's commit, logged so that the commit is never reviewed twice."""

    event: Literal["verdict"] = "verdict"
    version: Literal[1] = 1
    reviewer: Reviewer
    commit: str
    verdict: Verdict


class RebaseRequested(_Event):
    """A lane resumed to rebase on its base after the host couldn't update its branch; a lane gets one."""

    event: Literal["rebase_requested"] = "rebase_requested"
    version: Literal[1] = 1
    commit: str


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


Event = LaneDispatched | FixRound | VerdictGiven | RebaseRequested | FailureLogged


def _event_tag(record: Any) -> str:
    if isinstance(record, dict):
        return f"{record.get('event')}/{record.get('version')}"
    return f"{record.event}/{record.version}"


LogRecord = Annotated[
    Annotated[LaneDispatched, Tag("lane_dispatched/1")]
    | Annotated[FixRoundV1, Tag("fix_round/1")]
    | Annotated[FixRound, Tag("fix_round/2")]
    | Annotated[VerdictGiven, Tag("verdict/1")]
    | Annotated[RebaseRequested, Tag("rebase_requested/1")]
    | Annotated[FailureLoggedV1, Tag("failure_logged/1")]
    | Annotated[FailureLogged, Tag("failure_logged/2")],
    Discriminator(_event_tag),
]
"""Any record the log may hold, of any version."""


class LaneCache(BaseModel):
    """The rebuildable part of the state directory: which session each lane last ran in, by ticket (ADR-0001)."""

    model_config = ConfigDict(extra="forbid")

    sessions: dict[str, str] = {}


CI = Literal["pending", "passed", "failed"]


class PullRequest(BaseModel):
    """An open pull request that closes a ticket, as the host reports it."""

    ref: str
    ticket: str
    branch: str
    head: str
    ci: CI
    labels: list[str]
    draft: bool


class Session(BaseModel):
    """A Claude Code session as `claude agents --json --all` lists it; agent view is a preview, so new fields are ignored."""

    model_config = ConfigDict(populate_by_name=True)

    id: str | None = None
    session_id: str = Field(alias="sessionId")
    name: str = ""
    state: str | None = None
    cwd: str
    started_at: int = Field(alias="startedAt", default=0)
