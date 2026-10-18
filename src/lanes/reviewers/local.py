"""The `Reviewers` port over the `codex` and `claude` CLIs, run on this machine, never in CI."""

import json
import re
import subprocess
import tempfile
from pathlib import Path

from pydantic import ValidationError

from lanes.models import Reviewer, Verdict
from lanes.reviewers import ReviewerError

_FRONTMATTER = re.compile(r"\A---\n.*?^---\n", re.MULTILINE | re.DOTALL)
_EFFORT = {"claude-high": "high", "claude-max": "max", "claude-adversarial": "high"}
# Where Codex may not read the code, this takes its place as the second, independent view.
_ADVERSARIAL = (
    "You are the second reviewer, and the first one will look for the obvious problems. Assume the change is wrong "
    "until the code shows otherwise: hunt for the input, state or ordering that breaks it, the acceptance criterion "
    "it only appears to meet, and the test that would still pass if the behaviour it names were broken. Hold the "
    "same bar for what blocks: a finding you can't back with a concrete failing case is a nit."
)


class LocalReviewers:
    def review(self, reviewer: Reviewer, prompt: str, cwd: Path) -> Verdict:
        if reviewer == "codex":
            return _codex(prompt, cwd)
        return _claude(reviewer, prompt, cwd)


def _schema() -> str:
    """The JSON schema every reviewer fills, generated from `Verdict`."""
    return json.dumps(Verdict.model_json_schema())


def _codex(prompt: str, cwd: Path) -> Verdict:
    # Codex has no agents of its own, so it is handed the reviewer agent's instructions, frontmatter cut off.
    agent = cwd / ".claude" / "agents" / "reviewer.md"
    if agent.is_file():
        prompt = _FRONTMATTER.sub("", agent.read_text(encoding="utf-8"), count=1).strip() + "\n\n" + prompt
    with tempfile.TemporaryDirectory(prefix="lanes-review-") as scratch:
        schema_file, answer = Path(scratch) / "verdict.schema.json", Path(scratch) / "verdict.json"
        schema_file.write_text(_schema(), encoding="utf-8")
        # `-` reads the prompt from stdin: a diff can be longer than one argument may be.
        _run(
            ["codex", "exec", "--sandbox", "read-only", "--output-schema", str(schema_file),
             "--output-last-message", str(answer), "-"],
            prompt,
            cwd,
        )
        try:
            return Verdict.model_validate_json(answer.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise ReviewerError(f"codex returned no verdict: {error}") from None


def _claude(reviewer: Reviewer, prompt: str, cwd: Path) -> Verdict:
    args = ["claude", "-p", "--agent", "reviewer", "--effort", _EFFORT[reviewer], "--output-format", "json",
            "--json-schema", _schema(), "--no-session-persistence"]
    if reviewer == "claude-adversarial":
        args += ["--append-system-prompt", _ADVERSARIAL]
    printed = _run(args, prompt, cwd)
    try:
        result = json.loads(printed)
        if result.get("is_error") or "structured_output" not in result:
            raise ReviewerError(f"{reviewer} returned no verdict: {result.get('result') or printed.strip()}")
        return Verdict.model_validate(result["structured_output"])
    except (ValueError, AttributeError) as error:
        raise ReviewerError(f"{reviewer} returned no verdict: {error}") from None


def _run(args: list[str], prompt: str, cwd: Path) -> str:
    try:
        done = subprocess.run(args, input=prompt, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        raise ReviewerError(f"this reviewer needs the {args[0]} CLI on PATH") from None
    except OSError as error:
        raise ReviewerError(f"{args[0]} could not start in {cwd}: {error}") from None
    if done.returncode != 0:
        raise ReviewerError(done.stderr.strip() or f"{args[0]} exited {done.returncode}")
    return done.stdout
