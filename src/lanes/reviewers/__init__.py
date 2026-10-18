"""The `Reviewers` port: the only way Lanes runs a reviewer on a lane's commit."""

from pathlib import Path
from typing import Protocol

from lanes.models import Reviewer, Verdict


class ReviewerError(Exception):
    """The reviewer refused or failed a review; the message says why, in its own words."""


class Reviewers(Protocol):
    def review(self, reviewer: Reviewer, prompt: str, cwd: Path) -> Verdict:
        """Have `reviewer` review the checkout in `cwd`, read-only and in a fresh session, given only `prompt`."""
        ...
