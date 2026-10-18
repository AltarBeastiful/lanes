"""Pydantic models: the single source for every structured shape."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

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
