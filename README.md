# Lanes

Takes a refined ticket to a reviewed, mergeable pull request, with code (never a model) deciding what runs next. See `GLOSSARY.md` and `docs/adr/`.

```bash
uv run lanes init      # in the target repository: config, hotspots, gates, runner, agents and hooks
uv run lanes check 42                      # a GitHub issue, or a markdown ticket's number or path
uv run lanes fail 12 "missed null check"   # log a defect you caught at merge in ticket 12's lane
uv run lanes report                        # first-pass acceptance, fix rounds and defects, per route and tier
uv run lanes tick                          # act once on each lane: review a green one, resume a red or blocked one
```

`lanes init` asks for the host, tracker, test command, lint command, N, whether Codex may read the code and the reviewers per tier. It writes `.lanes/lanes.toml`, `.lanes/hotspots.txt`, the gates and the pytest runner, the implementer and reviewer agents in `.claude/agents/` and their hooks in `.lanes/hooks/`, registers the repository in `~/.config/lanes/repos.toml`, and warns if Claude Code's workspace trust hasn't been accepted for it. It refuses a test command other than pytest. Commit what it wrote under the `allow-config` label, then turn on branch protection requiring `gates` and an up-to-date branch by hand.

## Tickets

`lanes check` reads the ticket from the tracker `.lanes/lanes.toml` names: a GitHub issue through the `gh` CLI, or, with `tracker = "markdown"` or no config, a file under `.scratch/<feature>/issues/`. A ticket that passes goes from triage to ready: it gets the `ready-for-agent` label and loses `needs-triage` and `needs-info`, while `ready-for-human` and `wontfix` stay. On a markdown ticket, labels are its comma-separated `**Status:**` line. Blocking edges come from GitHub's native issue dependencies, or a markdown ticket's `**Blocked by:**` line or `## Blocked by` section.

Both trackers sit behind the `HostAdapter` port, and `tests/test_host_contract.py` holds them to one contract. It runs against markdown always. To run it against GitHub too, point it at a throwaway repository with the `needs-triage` and `ready-for-agent` labels, where it creates and closes issues: `LANES_GITHUB_SANDBOX=owner/repo uv run pytest tests/test_host_contract.py`.

## Event log

`LaneStore` is the only code that touches the per-repository state directory, `$XDG_STATE_HOME/lanes/<repo>-<hash>/` (`~/.local/state` when that is unset), so nothing Lanes logs lands in the repository. The log, `events.jsonl`, is append-only: one JSON record per line, each named by its `event` and `version`, and older versions are upgraded when read. `lanes fail` appends a `failure_logged` record carrying the ticket's route and tier. `lanes report` groups lanes and defects by the route and tier each lane was dispatched with, falling back to the ticket's own for a lane Lanes never dispatched, and names the five lanes with the most fix rounds and failures, worth a `/retro`.

## Tick

`lanes tick` reads each lane's facts from the pull request host and agent view and acts on it at most once. A lane is an open pull request whose body carries `Closes #<n>`; its session is the one the state directory's cache maps it to, else the newest session named `T<n>`. So far the control table has these rows:

- CI red on a commit no fix round was spent on, fewer than two rounds spent: resume the session in the background with `--name T<n>` and a prompt of at most 200 lines, the gates' problem lines (failing tests among them) first, then the first error lines from the failed job's log. Log a `fix_round` event carrying the commit. When the resume starts a copy under a new session ID, the cache maps the lane to the copy.
- CI red after two fix rounds: label the pull request `needs-human` and resume nothing more.
- CI red on a commit a round was already spent on: wait for the session to push.
- CI green on a commit some of the tier's reviewers have no verdict on: run those reviewers, one after another, in the session's working directory, and log a `verdict` event per reviewer and commit, so no commit is reviewed twice by the same reviewer. Each reviewer gets the ticket, the pull request's diff, the CI log and `CODING_STANDARDS.md` on stdin, in a fresh read-only session that never sees the lane's transcript: `codex exec --sandbox read-only --output-schema`, or `claude -p --agent reviewer --json-schema` with `--effort high` or `max`, its verdict read from `structured_output`. `claude-adversarial`, the second reviewer where Codex may not read the code, is `claude-high` with an adversarial system prompt. The schema both fill is generated from the `Verdict` model.
- CI green with a blocking finding among the tier's verdicts on the head commit: a fix round as for red CI, with every blocking finding as the prompt and the same two-round limit. Nits never resume the lane.
- Anything else, or a pull request already labelled `needs-human`: leave the lane alone.

CI is the newest run of each check on the head commit, so a failed run that a re-run has passed no longer counts. Sessions are resumed by their full session ID, from their own working directory.

## Gates

The `gates` check runs `.lanes/gates.py` on every pull request: no test deleted, skipped or xfailed against the base, no CI, coverage, lint or Lanes config change (`.github/`, `.lanes/`, `.claude/`, lint and test tool settings) without the `allow-config` label, no failure that is new against the base, and no vacuous test: each new test is run on the base and must fail there. A refactor declares the tests that prove it preserved behaviour as `behaviour: preserved <test-id>` lines in the pull request body; those must pass on both commits instead. A new test the runner can't run on the base is reported inconclusive for the reviewer, not failed. It reaches the tests only through `.lanes/runner` (ADR-0002). `.lanes/` holds the copies `lanes init` installs from `src/lanes/templates/`; `tests/test_gates.py` proves them against the seeded branches in `examples/fixture-repo/`.

## Implementer lanes

`.claude/agents/implementer.md` is the agent each lane runs as. It skips `/implement`'s closing review, announces new shared names to the other `T*` sessions, writes `.lanes-blocked-on` and stops when it needs a contract nothing merged provides, and otherwise ends by pushing its branch and opening a draft pull request whose body carries `Closes #<n>`. `.claude/agents/reviewer.md` is the read-only reviewer `tick` runs once CI is green.

Two hooks live in the implementer's frontmatter, so they run only in lanes and never in your own interactive sessions:

- `.lanes/hooks/guard.py` (PreToolUse, `onFailure: "block"`) denies writes to CI, coverage, lint and Lanes config (the same paths the gates protect) and pushes to the default branch. A guard that crashes or is missing blocks the call too.
- `.lanes/hooks/quick-check.py` (Stop) runs `lint_command` and the test files the lane changed through `.lanes/runner`, and sends failures back at most three times in a row before leaving them to CI.

The guard stops an honest lane going the wrong way; it is not a sandbox. It reads shell commands word by word (including `bash -c`, `eval` and `env` wrappers) and denies any that names config unless it only reads it, but a script that writes config without naming it gets through, and the gates still catch the change. The quick checks run only test files the lane changed, not tests affected by a source change; the full suite runs in CI. Whether the implementer's hooks also cover subagents it starts has not been checked.

### Manual check: the guard fires in a background lane

Frontmatter hooks only run once workspace trust has been accepted for the folder (a `-p` session doesn't count), so this is checked by hand rather than in CI. From a throwaway worktree of a trusted repository that has the agent and hooks installed:

```bash
git worktree add -b guard-check ../lanes-guard-check && cd ../lanes-guard-check
claude --bg --agent implementer --name T0-guard-check --permission-mode acceptEdits \
  "Test of your guard, not a ticket. (1) Edit .github/workflows/gates.yml, replacing 'runs-on: ubuntu-latest' with 'runs-on: ubuntu-24.04'. (2) Run: git push --dry-run origin HEAD:main. Report what each tool returned; don't retry."
claude logs <id>     # both calls end in "PreToolUse:... hook error: ... Lanes guard: ..."
git status --short   # .github/ unchanged
```

Put the prompt before any variadic option such as `--allowedTools`, or the option swallows it. Then run the same edit in an interactive `claude` session in that worktree: it goes through, because no project setting carries the hooks.

Checked on 18 Oct 2026 with Claude Code 2.1.302: both calls were denied with the guard's message, `.github/` was unchanged, and the Stop hook ran when the session stopped. The frontmatter hooks fire under `--bg --agent`, so the project-settings fallback is not needed. To send a follow-up to such a session, pass the full session ID to `--resume`: a short ID opens the resume picker in the background and the session sits blocked.
