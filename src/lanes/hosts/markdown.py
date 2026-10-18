"""Offline host: one markdown file per ticket under `.scratch/<feature>/issues/`."""

from pathlib import Path

from lanes.hosts import TicketNotFound


class MarkdownHost:
    def __init__(self, root: Path) -> None:
        self._root = root

    def read_ticket(self, ref: str) -> str:
        """Read a ticket by its path, or by its number (`1`, `01`) when that is unambiguous."""
        path = self._root / ref
        if not path.is_file():
            path = self._find_by_number(ref)
        return path.read_text(encoding="utf-8")

    def _find_by_number(self, ref: str) -> Path:
        if not ref.isdigit():
            raise TicketNotFound(f"no ticket file at {ref}")
        matches = [path for path in self._root.glob(".scratch/*/issues/*.md") if _number(path) == int(ref)]
        if len(matches) != 1:
            found = ", ".join(str(path.relative_to(self._root)) for path in sorted(matches))
            raise TicketNotFound(
                f"ticket {ref} matches {found}; pass its path" if matches else f"no ticket numbered {ref} in .scratch/"
            )
        return matches[0]


def _number(path: Path) -> int | None:
    """The `NN` in `<NN>-<slug>.md`."""
    prefix = path.name.split("-", 1)[0]
    return int(prefix) if prefix.isdigit() else None
