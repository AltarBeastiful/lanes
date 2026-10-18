import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from lanes.cli import main

Check = Callable[[str], Result]

GOOD_LANES = '''\
route = "direct"
tier = "medium"
produces = ["HostAdapter"]
consumes = []
seams = ["cli"]
demo = "`lanes check` on a good ticket exits 0"
'''

GOOD_CRITERIA = '''\
- [ ] `lanes check <ticket>` exits 0 for a valid ticket
- [ ] A ticket without a demo line is rejected
'''


def ticket(lanes: str | None = GOOD_LANES, criteria: str = GOOD_CRITERIA) -> str:
    text = f"# 01: A ticket\n\n**Status:** ready-for-agent\n\n## Acceptance criteria\n\n{criteria}\n"
    if lanes is not None:
        text += f"\n## Lanes\n\n```toml\n{lanes}```\n"
    return text


@pytest.fixture
def check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Check:
    monkeypatch.chdir(tmp_path)
    issues = tmp_path / ".scratch" / "feature" / "issues"
    issues.mkdir(parents=True)

    def run(text: str) -> Result:
        (issues / "01-a-ticket.md").write_text(text)
        return CliRunner().invoke(main, ["check", ".scratch/feature/issues/01-a-ticket.md"])

    return run


def test_valid_ticket_passes(check: Check) -> None:
    result = check(ticket())
    assert result.exit_code == 0, result.output


def test_missing_fields_are_each_named(check: Check) -> None:
    result = check(ticket(lanes='seams = ["cli"]\n'))
    assert result.exit_code == 1
    for field in ("route", "tier", "produces", "consumes", "demo"):
        assert f"missing {field}" in result.output
    assert "seams" not in result.output


def test_ticket_without_a_lanes_section_names_every_field(check: Check) -> None:
    result = check(ticket(lanes=None))
    assert result.exit_code == 1
    assert "no ## Lanes section" in result.output
    for field in ("route", "tier", "produces", "consumes", "seams", "demo"):
        assert f"missing {field}" in result.output


def test_blank_demo_line_is_rejected(check: Check) -> None:
    result = check(ticket(lanes=GOOD_LANES.replace("`lanes check` on a good ticket exits 0", "  ")))
    assert result.exit_code == 1
    assert "missing demo" in result.output


def test_invalid_route_and_tier_list_the_allowed_values(check: Check) -> None:
    lanes = GOOD_LANES.replace('"direct"', '"yolo"').replace('"medium"', '"huge"')
    result = check(ticket(lanes=lanes))
    assert result.exit_code == 1
    assert "invalid route 'yolo': allowed values are direct, grill, spec, wayfinder" in result.output
    assert "invalid tier 'huge': allowed values are trivial, small, medium, high" in result.output


def test_empty_acceptance_criteria_are_rejected(check: Check) -> None:
    result = check(ticket(criteria=""))
    assert result.exit_code == 1
    assert "no acceptance criteria" in result.output


def test_criterion_that_names_no_observation_is_rejected(check: Check) -> None:
    criteria = GOOD_CRITERIA + "- [ ] Acceptance criterion 3\n- [ ] It works\n- [ ] TBD\n"
    result = check(ticket(criteria=criteria))
    assert result.exit_code == 1
    for number in (3, 4, 5):
        assert f"acceptance criterion {number} names no observation" in result.output
    assert "acceptance criterion 1 " not in result.output


def test_local_template_without_a_criteria_heading_passes(check: Check) -> None:
    text = (
        "# 01: A ticket\n\n**What to build:** something.\n\n**Status:** ready-for-agent\n\n"
        f"{GOOD_CRITERIA}\n## Lanes\n\n```toml\n{GOOD_LANES}```\n"
    )
    result = check(text)
    assert result.exit_code == 0, result.output


def test_ticket_can_be_named_by_its_number(check: Check) -> None:
    check(ticket())
    result = CliRunner().invoke(main, ["check", "1"])
    assert result.exit_code == 0, result.output


def test_unknown_ticket_is_reported(check: Check) -> None:
    check(ticket())
    result = CliRunner().invoke(main, ["check", "7"])
    assert result.exit_code == 2
    assert "no ticket numbered 7" in result.output


def test_lanes_block_that_is_not_toml_is_rejected(check: Check) -> None:
    result = check(ticket(lanes='route = "direct\n'))
    assert result.exit_code == 1
    assert "not valid TOML" in result.output


def test_local_template_criteria_after_a_what_to_build_heading_pass(check: Check) -> None:
    text = f"# 01: A ticket\n\n## What to build\n\nSomething.\n\n{GOOD_CRITERIA}\n## Lanes\n\n```toml\n{GOOD_LANES}```\n"
    result = check(text)
    assert result.exit_code == 0, result.output


def test_criteria_heading_may_vary_in_level_case_and_colon(check: Check) -> None:
    text = f"# 01: A ticket\n\n### Acceptance Criteria:\n\n{GOOD_CRITERIA}\n## Lanes\n\n```toml\n{GOOD_LANES}```\n"
    result = check(text)
    assert result.exit_code == 0, result.output


def test_non_ascii_ticket_is_read(check: Check) -> None:
    result = check(ticket(lanes=GOOD_LANES.replace("exits 0", "exits 0 — Rémi")))
    assert result.exit_code == 0, result.output


def test_passing_ticket_is_marked_ready_for_agent(check: Check, tmp_path: Path) -> None:
    result = check(ticket().replace("ready-for-agent", "needs-triage"))

    assert result.exit_code == 0, result.output
    assert "**Status:** needs-triage, ready-for-agent" in (tmp_path / ".scratch/feature/issues/01-a-ticket.md").read_text()


def test_failing_ticket_is_not_marked_ready_for_agent(check: Check, tmp_path: Path) -> None:
    result = check(ticket(lanes=None).replace("ready-for-agent", "needs-triage"))

    assert result.exit_code == 1
    assert "ready-for-agent" not in (tmp_path / ".scratch/feature/issues/01-a-ticket.md").read_text()


FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["repo", "view"]:
    print("owner/repo")
elif sys.argv[1:3] == ["issue", "view"]:
    print(json.dumps({"body": os.environ["FAKE_GH_BODY"], "labels": []}))
"""


LANES_TOML = """\
host = "github"
tracker = "github"
test_command = "pytest"
max_lanes = 2
codex_allowed = false

[reviewers]
trivial = []
small = []
medium = []
high = []
"""


def test_github_issue_that_passes_gets_ready_for_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    (repo / ".lanes").mkdir(parents=True)
    (repo / ".lanes" / "lanes.toml").write_text(LANES_TOML)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    bin = tmp_path / "bin"
    bin.mkdir()
    (bin / "gh").write_text(FAKE_GH)
    (bin / "gh").chmod(0o755)
    log = tmp_path / "gh.log"
    monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_BODY", ticket())
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(main, ["check", "42"])

    assert result.exit_code == 0, result.output
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["issue", "edit", "42", "--repo", "owner/repo", "--add-label", "ready-for-agent"] in calls
