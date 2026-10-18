"""Pydantic models: the single source for every structured shape."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, PositiveInt, StringConstraints, model_validator

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
