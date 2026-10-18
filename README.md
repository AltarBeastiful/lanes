# Lanes

Takes a refined ticket to a reviewed, mergeable pull request, with code (never a model) deciding what runs next. See `GLOSSARY.md` and `docs/adr/`.

```bash
uv run lanes init      # in the target repository: config, hotspots, gates and runner
uv run lanes check .scratch/<feature>/issues/01-<slug>.md
```

`lanes init` asks for the host, tracker, test command, N, whether Codex may read the code and the reviewers per tier. It writes `.lanes/lanes.toml`, `.lanes/hotspots.txt`, the gates and the pytest runner, registers the repository in `~/.config/lanes/repos.toml`, and warns if Claude Code's workspace trust hasn't been accepted for it. It refuses a test command other than pytest. Commit what it wrote under the `allow-config` label, then turn on branch protection requiring `gates` and an up-to-date branch by hand.

## Gates

The `gates` check runs `.lanes/gates.py` on every pull request: no test deleted, skipped or xfailed against the base, no CI, coverage or lint config change without the `allow-config` label, no failure that is new against the base, and no vacuous test: each new test is run on the base and must fail there. A refactor declares the tests that prove it preserved behaviour as `behaviour: preserved <test-id>` lines in the pull request body; those must pass on both commits instead. A new test the runner can't run on the base is reported inconclusive for the reviewer, not failed. It reaches the tests only through `.lanes/runner` (ADR-0002). `.lanes/` holds the copies `lanes init` installs from `src/lanes/templates/`; `tests/test_gates.py` proves them against the seeded branches in `examples/fixture-repo/`.
