"""The release smoke test's own behaviour, offline: its canned tickets, and how it reports, fails and cleans up."""

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from lanes.cli import main

TICKETS = Path(__file__).resolve().parent.parent / "examples" / "fixture-repo" / "tickets"


@pytest.mark.parametrize("canned", sorted(TICKETS.glob("*.md")), ids=lambda path: path.stem)
def test_each_canned_ticket_passes_check(canned: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    issues = tmp_path / ".scratch" / "fixture" / "issues"
    issues.mkdir(parents=True)
    shutil.copy(canned, issues / f"01-{canned.name}")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["check", f".scratch/fixture/issues/01-{canned.name}"])

    assert result.exit_code == 0, result.output


def test_the_fixture_has_two_canned_tickets() -> None:
    assert len(list(TICKETS.glob("*.md"))) == 2


def test_a_run_whose_steps_all_pass_reports_each_and_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    # Here, not at the top, in each test: on the base, where smoke.py is missing, these tests fail rather than
    # the whole file failing to collect.
    import smoke

    done: list[str] = []
    steps = [(name, lambda name=name: done.append(name)) for name in ("seed", "check", "dispatch")]

    code = smoke.run(steps, cleanup=lambda: done.append("cleanup"))

    assert code == 0
    assert done == ["seed", "check", "dispatch", "cleanup"]
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line.endswith(": ok")] == [
        "seed: ok",
        "check: ok",
        "dispatch: ok",
        "cleanup: ok",
    ]


def test_a_failed_step_is_named_the_rest_are_skipped_and_cleanup_still_runs(capsys: pytest.CaptureFixture[str]) -> None:
    import smoke

    done: list[str] = []

    def dispatch() -> None:
        raise smoke.Failed("claude --bg printed no session")

    steps = [("seed", lambda: done.append("seed")), ("dispatch", dispatch), ("tick", lambda: done.append("tick"))]

    code = smoke.run(steps, cleanup=lambda: done.append("cleanup"))

    assert code == 1
    assert done == ["seed", "cleanup"]
    assert "smoke failed at dispatch: claude --bg printed no session" in capsys.readouterr().out


def test_a_cleanup_that_fails_fails_the_run(capsys: pytest.CaptureFixture[str]) -> None:
    import smoke

    def cleanup() -> None:
        raise smoke.Failed("branch T7 is still on the sandbox")

    code = smoke.run([("seed", lambda: None)], cleanup=cleanup)

    assert code == 1
    assert "smoke failed at cleanup: branch T7 is still on the sandbox" in capsys.readouterr().out


def test_an_interrupted_run_still_cleans_up() -> None:
    import smoke

    done: list[str] = []

    def tick() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        smoke.run([("tick", tick)], cleanup=lambda: done.append("cleanup"))

    assert done == ["cleanup"]
