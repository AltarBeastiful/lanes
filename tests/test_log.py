"""Seam 1: `lanes fail` and `lanes report` through the CLI, with a real `LaneStore` on a temporary state directory."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from lanes.cli import main
from lanes.models import FixRound, LaneDispatched
from lanes.store.files import FileStore, state_directory

Lanes = Callable[..., Result]


def ticket(route: str = "direct", tier: str = "medium") -> str:
    return (
        "# 12: A ticket\n\n## Acceptance criteria\n\n- [ ] `lanes check` exits 0 here\n\n## Lanes\n\n```toml\n"
        f'route = "{route}"\ntier = "{tier}"\nproduces = []\nconsumes = []\nseams = ["cli"]\ndemo = "it runs"\n```\n'
    )


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return state


@pytest.fixture
def repo(tmp_path: Path, state: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    issues = repo / ".scratch" / "feature" / "issues"
    issues.mkdir(parents=True)
    (issues / "12-a-ticket.md").write_text(ticket())
    (issues / "15-another.md").write_text(ticket(route="grill", tier="small"))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def lanes(repo: Path) -> Lanes:
    return lambda *args: CliRunner().invoke(main, list(args))


def logged(state: Path) -> list[dict[str, object]]:
    return [json.loads(line) for log in state.glob("lanes/*/events.jsonl") for line in log.read_text().splitlines()]


def test_fail_appends_one_versioned_event(lanes: Lanes, state: Path) -> None:
    result = lanes("fail", "12", "missed null check")

    assert result.exit_code == 0, result.output
    [event] = logged(state)
    assert event["event"] == "failure_logged"
    assert event["version"] == 2
    assert (event["ticket"], event["route"], event["tier"], event["what"]) == ("12", "direct", "medium", "missed null check")


def test_fail_and_report_write_nothing_inside_the_repository(lanes: Lanes, repo: Path) -> None:
    before = sorted(repo.rglob("*"))

    failed, reported = lanes("fail", "12", "missed null check"), lanes("report")

    assert failed.exit_code == 0, failed.output
    assert reported.exit_code == 0, reported.output
    assert sorted(repo.rglob("*")) == before


def test_fail_on_an_unknown_ticket_logs_nothing(lanes: Lanes, state: Path) -> None:
    result = lanes("fail", "99", "missed null check")

    assert result.exit_code == 2
    assert "no ticket numbered 99" in result.output
    assert logged(state) == []


def test_a_ticket_named_with_leading_zeros_is_the_same_lane(lanes: Lanes, repo: Path) -> None:
    FileStore(state_directory(repo)).append(LaneDispatched(ticket="12", route="direct", tier="medium"))
    lanes("fail", "012", "missed null check")

    result = lanes("report")

    assert "T12: 0 fix rounds, 1 failure" in result.output
    assert "T012" not in result.output


def test_report_shows_a_logged_defect_under_its_route_and_tier(lanes: Lanes) -> None:
    lanes("fail", "12", "missed null check")
    lanes("fail", "15", "wrong default")
    lanes("fail", "15", "typo in message")

    result = lanes("report")

    assert result.exit_code == 0, result.output
    rows = {tuple(line.split()[:2]): line.split() for line in result.output.splitlines()}
    assert rows[("direct", "medium")][-1] == "1"
    assert rows[("grill", "small")][-1] == "2"


def test_report_shows_first_pass_acceptance_and_fix_rounds_per_route_and_tier(lanes: Lanes, repo: Path) -> None:
    store = FileStore(state_directory(repo))
    store.append(LaneDispatched(ticket="12", route="direct", tier="medium"))
    store.append(LaneDispatched(ticket="15", route="grill", tier="small"))
    store.append(LaneDispatched(ticket="16", route="grill", tier="small"))
    store.append(FixRound(ticket="12", commit="abc"))
    store.append(FixRound(ticket="15", commit="abc"))
    store.append(FixRound(ticket="15", commit="abc"))
    store.append(FixRound(ticket="15", commit="abc"))

    result = lanes("report")

    assert result.exit_code == 0, result.output
    rows = {tuple(line.split()[:2]): line.split()[2:] for line in result.output.splitlines()}
    # lanes, first pass, fix rounds per ticket, defects at merge
    assert rows[("direct", "medium")] == ["1", "0%", "1.0", "0"]
    assert rows[("grill", "small")] == ["2", "50%", "1.5", "0"]


def test_report_names_the_lanes_with_the_most_fix_rounds_or_failures(lanes: Lanes, repo: Path) -> None:
    store = FileStore(state_directory(repo))
    store.append(LaneDispatched(ticket="12", route="direct", tier="medium"))
    store.append(LaneDispatched(ticket="15", route="grill", tier="small"))
    store.append(LaneDispatched(ticket="16", route="grill", tier="small"))
    store.append(FixRound(ticket="12", commit="abc"))
    store.append(FixRound(ticket="15", commit="abc"))
    lanes("fail", "15", "wrong default")

    result = lanes("report")

    assert result.exit_code == 0, result.output
    worst = result.output.split("Most fix rounds or failures:")[1].strip().splitlines()
    assert [line.strip() for line in worst] == ["T15: 1 fix round, 1 failure", "T12: 1 fix round, 0 failures"]


def test_report_reads_a_failure_logged_before_events_carried_route_and_tier(lanes: Lanes, repo: Path) -> None:
    store = FileStore(state_directory(repo))
    store.append(LaneDispatched(ticket="15", route="grill", tier="small"))
    v1 = {"at": "2026-10-18T09:00:00Z", "ticket": "15", "event": "failure_logged", "version": 1, "what": "wrong default"}
    with (state_directory(repo) / "events.jsonl").open("a") as log:
        log.write(json.dumps(v1) + "\n")

    result = lanes("report")

    assert result.exit_code == 0, result.output
    rows = {tuple(line.split()[:2]): line.split() for line in result.output.splitlines()}
    assert rows[("grill", "small")][-1] == "1"
    assert "T15: 0 fix rounds, 1 failure" in result.output


def test_report_on_an_empty_log_says_so(lanes: Lanes) -> None:
    result = lanes("report")

    assert result.exit_code == 0, result.output
    assert "No events logged yet" in result.output


def test_report_puts_a_v1_failure_with_no_dispatched_lane_under_unknown(lanes: Lanes, repo: Path) -> None:
    v1 = {"at": "2026-10-18T09:00:00Z", "ticket": "12", "event": "failure_logged", "version": 1, "what": "missed null check"}
    state_directory(repo).mkdir(parents=True)
    (state_directory(repo) / "events.jsonl").write_text(json.dumps(v1) + "\n")

    result = lanes("report")

    assert result.exit_code == 0, result.output
    rows = {tuple(line.split()[:2]): line.split() for line in result.output.splitlines()}
    assert rows[("unknown", "unknown")][-1] == "1"


def test_report_counts_a_fix_round_logged_before_events_carried_the_commit(lanes: Lanes, repo: Path) -> None:
    FileStore(state_directory(repo)).append(LaneDispatched(ticket="12", route="direct", tier="medium"))
    v1 = {"at": "2026-10-18T09:00:00Z", "ticket": "12", "event": "fix_round", "version": 1}
    with (state_directory(repo) / "events.jsonl").open("a") as log:
        log.write(json.dumps(v1) + "\n")

    result = lanes("report")

    assert result.exit_code == 0, result.output
    assert "T12: 1 fix round, 0 failures" in result.output


def test_report_reads_a_lane_dispatched_before_events_carried_overrides(lanes: Lanes, repo: Path) -> None:
    v1 = {"at": "2026-10-18T09:00:00Z", "ticket": "12", "event": "lane_dispatched", "version": 1, "route": "spec",
          "tier": "high"}  # fmt: skip
    state_directory(repo).mkdir(parents=True)
    (state_directory(repo) / "events.jsonl").write_text(json.dumps(v1) + "\n")

    result = lanes("report")

    assert result.exit_code == 0, result.output
    assert ("spec", "high") in {tuple(line.split()[:2]) for line in result.output.splitlines()}
