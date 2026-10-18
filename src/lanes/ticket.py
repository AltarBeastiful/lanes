"""Parse a ticket body the same way on every host and list what stops it becoming a lane."""

import re
import tomllib
from typing import get_args

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from lanes.models import TicketLanesSection

_HEADING = re.compile(r"^#{2,6} +(.+?)\s*:?\s*$", re.MULTILINE)
_TOML_BLOCK = re.compile(r"^```toml\s*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_CHECKBOX = re.compile(r"^[-*] \[[ xX]\] *(.*?)\s*$", re.MULTILINE)
# A criterion this short, or a template placeholder, can't say what to observe at the base commit.
_MIN_OBSERVATION_WORDS = 3
_PLACEHOLDER = re.compile(r"^(acceptance criterion \d+|tbd|todo|\.\.\.)$", re.IGNORECASE)


def problems(body: str) -> list[str]:
    """Every reason the ticket can't be dispatched; empty when it passes `lanes check`."""
    found: list[str] = []
    start, end = _section(body, "Lanes") or (len(body), len(body))
    block = _TOML_BLOCK.search(body, start, end)
    if block is None:
        found.append("no ## Lanes section with a ```toml block")
    found += _section_problems(block.group(1) if block else "")
    found += _criteria_problems(body[:start] + body[end:])
    return found


def _criteria_problems(body: str) -> list[str]:
    """Criteria sit under an `Acceptance criteria` heading; the local template has none, so any checkbox counts."""
    start, end = _section(body, "Acceptance criteria") or (0, len(body))
    criteria = _CHECKBOX.findall(body, start, end)
    if not criteria:
        return ["no acceptance criteria"]
    return [
        f"acceptance criterion {number} names no observation: {criterion!r}"
        for number, criterion in enumerate(criteria, start=1)
        if len(criterion.split()) < _MIN_OBSERVATION_WORDS or _PLACEHOLDER.match(criterion)
    ]


def _section_problems(toml: str) -> list[str]:
    try:
        TicketLanesSection.model_validate(tomllib.loads(toml))
    except tomllib.TOMLDecodeError as error:
        return [f"## Lanes block is not valid TOML: {error}"]
    except ValidationError as error:
        return [_describe(e) for e in error.errors()]
    return []


def _describe(error: ErrorDetails) -> str:
    field = ".".join(str(part) for part in error["loc"])
    if error["type"] in ("missing", "string_too_short"):
        return f"missing {field}"
    if error["type"] == "literal_error":
        allowed = ", ".join(get_args(TicketLanesSection.model_fields[field].annotation))
        return f"invalid {field} {error['input']!r}: allowed values are {allowed}"
    return f"invalid {field}: {error['msg']}"


def _section(body: str, title: str) -> tuple[int, int] | None:
    """Where the `## <title>` heading starts and the next heading begins."""
    headings = list(_HEADING.finditer(body))
    for i, heading in enumerate(headings):
        if heading.group(1).casefold() == title.casefold():
            return heading.start(), headings[i + 1].start() if i + 1 < len(headings) else len(body)
    return None
