# Lanes — from refined ticket to merged PR, with no model deciding what runs next

Oct 18, 2026 · @Rémi

Lanes takes a refined ticket to a reviewed, mergeable pull request without a model ever deciding what runs next. It reuses what already exists: Matt Pocock's skills to refine work and to review it, one Claude Code background session per ticket, CI as the only certifier, and GitHub or Azure DevOps for tickets and PRs. The default needs one Claude Code subscription and nothing billed beyond it; a second model family, parallel lanes and heavier test checks are options. What it adds is about 700 lines of Python that set how much process each task gets, cap fix rounds, and log what happened. Lanes lives in its own repository, installs into any project with `lanes init`, builds part of itself from its second week, and dispatches its own tickets by its fourth.

## Design rules

1. Code decides control flow; models write plans, code and verdicts.
2. Nothing an implementer says about its own work is evidence. Only CI output and a reviewer that never saw its transcript count.
3. Process scales with the task. Small, reversible work skips the skill chain; risky work gets more review, not more planning.
4. Dependencies are declared before lanes start. Discovering them mid-run is the fallback.
5. You decide twice: when you approve tickets and when you merge.
6. Every threshold is tuned from the log, never set once and trusted.
7. The default runs on one Claude Code subscription with nothing billed beyond it. Anything heavier is an option, switched on by you or by the log.

## The cycle

&#91;embedded content: Lanes cycle · 6 stages, 2 human gates, 1 capped fix loop\]

Everything between your two decisions runs unattended. A red CI or a blocking review finding goes back to the same session at most twice, then comes to you. The next section decides how much of this cycle a given task goes through.

## How much process a task gets

Two decision tables, applied at intake, set each task's planning depth and its risk tier. They exist because extra process on small work costs money without adding quality. In an October 2026 benchmark (skillarena, Claude Code 2.1.296, Sonnet 5.5), vanilla Claude Code passed every small task; Matt's skills added 6% cost without ever firing, and superpowers 7 added 27%.

**Planning depth** comes from size and clarity:

| Fits one session? | Requirements clear? | Route |
| --- | --- | --- |
| yes | yes | Direct: the ticket text is the lane's prompt |
| yes | no | Grill first: `/grill-with-docs` with you, then direct |
| no | yes | Spec flow: `/grill-with-docs`, `/to-spec`, `/to-tickets`, then one lane per ticket |
| no | no | `/wayfinder` until the route is clear, then the spec flow |

**Risk tier** comes from the two Merge Danger questions in Matt's `/pr` skill: can the change be walked back (a two-way door) or not (a one-way door), and how much breaks if it is wrong. The first matching row from the top wins:

| Tier | When | Lane prompt | Tests | Review | At merge |
| --- | --- | --- | --- | --- | --- |
| high | One-way door: a migration, deleted data, sent messages, a published API, auth or security | `/implement`, then `/pr` | Locked acceptance tests, written before the lane starts; mutation testing on changed code | Reviewer at high effort | Read the diff |
| medium | Two-way door, but it produces or consumes a shared contract, or spans modules | `/implement`, then `/pr` | The implementer's, which must fail on the base | Reviewer at medium effort | Read Merge Danger and the risky files |
| small | Two-way door, one module, no shared contract | Ticket text, then `/pr` | The implementer's, which must fail on the base | Reviewer at low effort | Skim |
| trivial | Two-way door, one file, text or config | Ticket text | CI only | None | Skim, or auto-merge (open question) |

`/implement` adds test-first work through `/tdd`; a plain prompt loads none of Matt's skills. You can override with `--route` or `--tier`, and every override is logged.

Two floors stop a risky change from being classified small. Paths listed as `high_paths` in `lanes.toml` (migrations, schema, auth, CI, public API) force `high`. And when the PR's Merge Danger section disagrees with the ticket's tier, the higher tier wins.

**Options, all off by default.** Each can be switched on per repository in `lanes.toml`, per tier, or per ticket with a flag, and every use is logged.

- **Adversarial review** (`--adversarial`): a second reviewer that tries to break the change and hunts bugs, which the default review doesn't do. It is either Codex, if you have a ChatGPT plan, or a second Claude pass told to break the change. On `high` it also reviews the spec and tickets before you approve them. Codex runs on your machine, never in CI on public repositories, and never on a client repository until the client approves it in writing.
- **Locked tests and mutation testing on `medium`**, as on `high`.
- **Parallel lanes** (N ≥ 2, see Parallelism). On one subscription, two lanes and their reviews reach usage limits fast, so N = 1 is the default.

**Idle cost.** Install Matt's skills as the Claude Code plugin. They are user-invoked, so a direct lane pays only for their listing. Reviews don't call the plugin's `/code-review`: Lanes ships its own pinned copy, `lanes-review` (stage 4), so a plugin update can't change a review mid-project and the name clash with Claude Code's built-in `/code-review` doesn't matter. Lanes uses neither the built-in `/code-review` nor Ultrareview: both draw more usage than one pass, and Ultrareview bills usage credits. Don't add superpowers: its every-session bootstrap is the main idle cost, and its TDD skill overlaps `/tdd`. Run `/skill-doctor` once a month and switch off what never fires.

**Self-correction.** After 10 tickets in a class, if a lighter route or tier would have caught the same defects (`lanes fail`, below) at lower cost, the boundary moves down. Any defect that escapes a light tier moves its class up or switches on an option for it, and a promotion expires after 20 clean tickets in that class. Options appear where your own log says the default isn't enough.

## Stage by stage

**1. Refine (interactive, Opus 5.5).** Your own ideas start with `/grill-with-docs`; `/triage` is only for issues other people filed, such as reports on a public repo. The spec flow runs grill, `/to-spec` and `/to-tickets` in one session, so the spec is never re-fetched, and `/to-tickets` publishes sub-issues with blocking links. Its quiz is your first gate. `lanes check` refuses a ticket unless it has:

- a demo line answered with behaviour (no answer means a horizontal slice);
- acceptance criteria that each name an observation failing at the base commit;
- a route and a tier;
- the contracts it `produces` and `consumes`: type names, config keys, endpoints, schema fields, possibly none;
- seams named in the spec, since `/implement` agrees none on its own.

**2. Plan lanes (deterministic).** `lanes plan` computes the frontier from merged PRs, not from the tracker's blocked-by count, which lags until blockers close. A consumer whose producer has not merged gets a hard edge, unless you choose to put the new contract into its prompt instead. With N = 1, the default, the highest-priority ticket on the frontier starts next; with parallel lanes on, candidates are scored (see Parallelism).

Only tickets you marked ready are dispatched, so an outsider's issue text never becomes a prompt: outside reports first become your own brief through `/triage`. A ticket you closed without a PR counts as done. `lanes plan` refuses cyclic edges and lists any ticket blocked for more than two days.

**3. Implement (one background session per ticket, Sonnet 5.5).** `lanes go` fetches, then dispatches each picked ticket:

```bash
git fetch origin
claude --bg --agent implementer --name T42 "/mattpocock-skills:implement #42"
```

That is the medium and high form; small and trivial lanes get the ticket text. The fetch matters: new worktrees branch from the cached `origin/HEAD` unless the last fetch is over 24 hours old, so without it a lane can start without its blocker's merge.

Agent view moves the session into its own worktree before its first edit and keeps it running without a terminal. Its built-in rules commit, push and open a draft PR, and never push to `main`. The `implementer` agent adds:

- Skip `/implement`'s closing review. It runs before the commit, so it sees no diff (an open bug), and an author reviewing its own work is biased anyway.
- Commit, push, open a draft PR with `/mattpocock-skills:pr`, stop.
- Explore and research subagents are allowed under a concurrency cap; never start another lane.
- With parallel lanes on, before adding a new shared name, announce it to the other `T*` lanes by cross-session message.
- If you need something no merged code provides, write `$CLAUDE_JOB_DIR/tmp/blocked-on.json` (`{"ticket": 15, "need": "InvoiceTotal type"}`) and stop.

A `PreToolUse` guard with `onFailure: "block"` denies writes to CI, coverage and lint config, to locked tests and to `.lanes/`, and pushes to `main`. A `Stop` hook runs lint and affected tests before the session stops, capped at three re-entries. Both are conveniences; the session's own test run is never evidence.

The guard also denies `gh` commands that add labels or change branch protection, so a lane can never grant itself `allow-config`. The implementer agent sets its model and effort explicitly, and a per-tier file-count ceiling flags diffs that outgrow their ticket.

**Locked tests (`high`).** Before the implementer starts, `lanes go` runs a `test-writer` session (Opus 5.5) that turns the ticket's acceptance criteria into tests and pushes them as the lane's first commit. The guard denies the implementer any edit to those files, and the hygiene gate fails if they changed, so the implementer is graded by tests it didn't write. If it believes a locked test is wrong, it writes `$CLAUDE_JOB_DIR/tmp/test-dispute.json` with the test's name and its reason, and stops.

**4. Validate (CI, then a reviewer in a fresh session).** CI runs on every push, in a fresh checkout, with no model involved:

1. Hygiene: no test deleted, skipped or marked xfail against the base; no locked test changed; no CI, coverage or lint config change without the `allow-config` label.
2. Suite: full tests, lint and type check; only failures new against the base count.
3. Test validity: new or changed tests run on the base commit and must fail with assertions (pytest exit 1). Collection or import errors (exit 2) are inconclusive and go to the reviewer. Tests marked `behaviour: preserved` must pass on both.
4. New skips fail the gate, which catches gitignored fixtures that skip silently in a fresh checkout.
5. Mutation testing on `high`: mutants of the changed code, run through the same per-runner adapter. Survivors go to the reviewer and into the fix round; whether they block is tuned from the log.

The hygiene gate accepts `allow-config` only when a human account added it, and runs a secret scan. Failed tests rerun once; a test that passes on the rerun is flagged flaky instead of turning the build red. Test-validity rules come from a per-runner adapter, the pytest exit codes above being the first; a runner without an adapter reports inconclusive, never pass.

When CI is green, `lanes tick` runs one reviewer on your machine: a fresh `claude -p` session in a detached worktree of the PR head. `tick` starts it, never the lane, so the author never writes its prompt. It reads the repository, the ticket and the CI log, never the implementer's transcript or environment files. It runs `lanes-review`: Matt's `/code-review`, vendored under that name and pinned to a commit. Its one change is the line users apply on forks to both sub-agent briefs: review directly, never invoke the skill again or spawn more agents. That closes a known open bug where its sub-agents re-invoke it; one report reached more than 50 agents.

The skill reviews on two axes, each in its own sub-agent: Standards (`CODING_STANDARDS.md`, then twelve named code smells) and Spec (the ticket, which `tick` writes to a file and passes as the spec path). It doesn't hunt bugs: CI and the tests do that, and adversarial review adds it as an option. The reviewer agent turns the report into one JSON verdict shape. A breach of a documented rule or a Spec finding (a missing or wrong requirement, scope creep) is blocking; a smell is a nit; a finding without a citation is dropped.

```bash
claude -p --agent reviewer-medium --permission-mode dontAsk \
  --max-budget-usd "$REVIEW_BUDGET" --output-format json \
  --json-schema "$(cat verdict.schema.json)" \
  "/lanes-review origin/main $LANE_STATE/ticket.md"
```

`reviewer-low`, `reviewer-medium` and `reviewer-high` differ only in effort. All run Opus 5.5, so a Sonnet implementer is reviewed by a different model of the same family. `dontAsk` denies anything that would need approval, so the session reads but doesn't edit, and the verdict arrives in `structured_output`. The budget ceiling stops a runaway review; on a subscription it is a client-side estimate, so treat it as a guard, not accounting.

Reviews run as detached jobs that `tick` collects on its next pass. A verdict is keyed on the PR's own patch (merge base to head), so updating the branch from `main` does not trigger a new review. A missing or schema-invalid verdict counts as a blocking finding, `review-unavailable`. Options and their models are set per repository in `lanes.toml`.

Nits never block, and only cited blocking findings go back: the skill finds something new on every pass, so the two-round cap is what ends the loop. A red CI or a blocking finding goes back to the same session with `claude --resume <id> --bg "<failing tests or findings, ≤ 200 lines>"`. Since v2.1.257 a resume may continue under a new ID; `tick` reads the printed ID and re-maps the lane. After two fix rounds the PR is labelled `needs-human`.

**5. Merge (you).** Branch protection requires the `gates` check and an up-to-date branch. When a lane is green and clean but behind `main`, `tick` runs the host's update-branch (`gh pr update-branch` on GitHub), which needs no model. On a conflict it resumes the session once with "rebase on origin/main". Then it marks the PR ready and notifies you, and you merge. GitHub's merge queue is an option only on public repos owned by an organisation, or on Enterprise Cloud.

**6. Retro and failure log (you).** Every defect you catch by hand at merge goes into the log with `lanes fail <PR> "<what I caught>"`. `lanes report` names the lanes with the most fix rounds or logged failures; run `/retro` on a sample of those by hand. Matt warns that an automated retro chases false positives. Mechanical mistakes become CI checks or lint rules; judgement calls go to `CODING_STANDARDS.md`, which the reviewer's Standards axis reads; `CLAUDE.md` gets navigation pointers only. Recurring conflicts on the same files are an architecture signal: feed them to `/improve-codebase-architecture`.

The ready-for-merge notification ends by asking whether you caught anything, so logging a defect is one reply. Notifications come as at most one digest per tick, and only for blocked, `needs-human` and ready PRs.

## Parallelism

Lanes runs one lane at a time by default. Parallel lanes are an option for when your plan's usage limits and your log can carry them. With it on, two tickets run together only when the dependency graph allows it and their estimated collision risk is under a threshold tuned from your own log. The merge step stays the final authority.

Dependencies are declared up front because discovering them while lanes run mostly comes too late. Matt's field reports show it: two parallel tickets added the same field as `blockedSince` and `blockedOn`, GitHub's blocked-by counts stalled the frontier mid-run, and layer-sliced tickets cost about 20 agent runs per closed ticket. NP-Bench (October 2026) measured it: on a breaking contract change, clean integration was 0.00 with no coordination and with live warnings, and 1.00 with an up-front plan on Opus 4.8 (15 runs). Its limit is also ours: its scopes were hand-declared, and predicting them is unproven.

**Prediction.** `/to-tickets` keeps file paths out of ticket bodies by design, so `lanes plan` asks Haiku 5.5 once per ready ticket which files it will touch and which new shared names it will add:

```bash
claude -p --model haiku --output-format json --json-schema "$(cat touch.schema.json)" "Ticket #42: …"
```

Each file gets a confidence of 0.9 (named area), 0.5 (likely) or 0.2 (possible). The prediction is stored in the state directory, not the repository.

**Score.** Each file gets a weight h(f): 1.0 for hotspots (lockfiles, route tables, DI containers, i18n catalogues, migration indexes, registries, seeded in `.lanes/hotspots.txt`, plus any file that caused a past conflict), 0.5 otherwise. For tickets i and j:

```latex
P_{ij} = 1 - \prod_{f \in T_i \cap T_j} \left(1 - c_i(f)\, c_j(f)\, h(f)\right)
```

The same new shared name in both tickets sets P to 1. Declared `produces` and `consumes` contracts are hard edges, not scores.

Predicted new shared names also go into each lane's prompt as fixed names, so two lanes that run together spell them the same way. `/implement-spec` does the same in the notes it writes before its implementers start.

**Schedule, on every `tick`.**

1. Frontier = tickets whose blockers have merged into `main`.
2. For each running lane, replace its prediction with its real diff (`git diff --name-only origin/main...T42`) plus predicted files not yet touched.
3. Walk the frontier by priority; start a ticket when its risk against every running lane is under the threshold and fewer than N lanes run.
4. Between the threshold and 0.4, record a soft edge and show it in `lanes plan`. Above 0.4, serialise without asking.

Once parallel lanes are on: threshold 0.15, N = 2 at first, then 3. More than three or four lanes is past what one person can integrate.

Lanes share your subscription with everything else you run, so dispatch happens only inside the hours set in `lanes.toml`, and stops while any lane is waiting on a usage limit.

**Your own lanes.** `lanes go 12 15` prints the risk for each pair and starts them; `--force` skips the soft-edge prompt. A session you start yourself with `claude --bg --name T12 …`, or from agent view, is adopted on the next `tick` by its `T<n>` name, and from then on its real diff counts against new candidates.

**Unplanned dependencies.** When a lane writes `blocked-on.json`, `tick` adds the edge, logs a planning miss, and resumes the lane once the producer merges, with the producer's diff summary in the prompt. Cross-session announcements are only a supplement: the same study found vague live warnings worth about nothing.

**Calibration.** Every pair that ran together is logged with its predicted P and the outcome: clean, update-branch conflict, or a post-merge CI failure traced to the pair. After about 30 pairs, move the threshold so conflicts stay under one in ten. Add git co-change coupling, or replace the formula with a fitted model, only if the log shows it misranks pairs.

## Hosts, trackers and PRs

Lanes needs a tracker to hold the blocking graph and PRs to attach gates and reviews; it does not need GitHub. Matt has moved the same way: his skills target an integration branch rather than a PR, run offline on local markdown, and treat the tracker as configuration. His v1.3 notes also recommend a deterministic script over an agent loop for unattended work; Lanes is that script.

PRs stay because they are where the work is read. In Matt's words, PRs are still the main bottleneck for work reaching main: writing code got cheap, reading it did not. Even in a one-person repository, the PR is where CI attaches, where review findings land, where agent view shows a lane's status, and where update-branch and Merge Danger live.

Each host plugs in through one adapter: four tracker calls (list ready tickets, read blocking edges, read contracts, mark done) and four PR calls (status, update branch, label, comment).

| Host | Tracker | PRs and CI | Status |
| --- | --- | --- | --- |
| GitHub | Issues, native sub-issues and blocking links | PRs, Actions, `gh` | Verified against current docs |
| Azure DevOps | Boards predecessor links, via `az boards` and Matt's "Other" tracker | Repos PRs, Pipelines, `az repos` | To verify in rollout step 6 |
| Offline | `.scratch/` markdown, edges parsed from text | Local branches, local gate script | For spikes; supervise the edges |

Use one tracker per repository, as Matt advises. The frontier always comes from merged PRs, because tracker state lags on every host.

## Lanes as its own project

Lanes is installed once per machine and initialised once per target repository. Nothing about it assumes a particular project.

```text
lanes/                      the Lanes repository
  src/lanes/
    cli.py                  check, plan, go, tick, report, fail, init, upgrade
    tick.py                 the control table
    schedule.py             frontier; prediction and scoring for parallel lanes
    review.py               runs the reviewer and any options, merges verdicts
    hosts/github.py         tracker + PR adapter
    hosts/azure.py
    hosts/markdown.py
  templates/                rendered into target repos by lanes init
    gates/                  CI workflow + hygiene, test-validity and mutation scripts
    agents/                 implementer.md, test-writer.md, reviewer-{low,medium,high}.md
    skills/lanes-review/    Matt's /code-review, pinned, with the no-fan-out line
    hooks/                  guard.sh, quick-checks.sh
    schemas/                verdict, touch prediction
  examples/fixture-repo/    two canned tickets, run end to end as a smoke test
```

`lanes init` asks four questions: host, tracker, test command, and which options to switch on (none by default). It then writes committed configuration into the target repository: `.lanes/lanes.toml`, `.lanes/hotspots.txt`, the schemas, the lanes-review skill, `.claude/agents/`, `.claude/hooks/` with their settings entries, and the CI workflow. It also registers the repository in `~/.config/lanes/repos.toml`. Runtime state, meaning predictions, session mappings and the log, lives in `~/.local/state/lanes/<repo>/`, outside the repository. One systemd user timer runs `lanes tick --all` every 10 minutes across every registered repository. `lanes upgrade` re-renders the templates as a diff you commit under the `allow-config` label.

`lanes init` also accepts Claude Code's workspace trust for the repository (a timer-launched `claude --bg` exits without it), checks that `loginctl enable-linger` is on so the timer runs while you are logged out, and records pinned versions of Lanes, Claude Code, Matt's skills and the reviewer models. Upgrades of any of them happen only after the fixture repository runs end to end. A dead-man check notifies you when no tick has succeeded for an hour. Round counts and lane-to-ticket mappings are mirrored as PR labels, so a lost state directory is rebuilt, not restarted.

Lanes depends on these platform facts, checked on 17 October 2026:

| Dependency | Fact relied on |
| --- | --- |
| Claude Code ≥ v2.1.295 | Hook `onFailure: "block"`; `--resume <id> --bg` (since v2.1.257); `claude agents --json --all` states `working`, `blocked`, `done`, `failed`, `stopped` |
| Agent view (research preview) | Worktree per session, commit/push/draft-PR rules, survives sleep, restarts crashed sessions; a shutdown leaves sessions failed but resumable |
| Matt Pocock skills v1.3, as plugin; /code-review vendored as lanes-review | Skills used: `/grill-with-docs`, `/to-spec`, `/to-tickets`, `/implement`, `/tdd`, `/pr`, `/retro`, `/wayfinder` |
| `gh` ≥ 2.94 | Sub-issues and blocking links; `gh pr update-branch` |
| Codex CLI (only with the Codex option) | `codex exec` read-only by default, `--output-schema`, model `gpt-6.1-sol` |
| Python ≥ 3.11 | The Lanes tool itself |

## What to build

| Piece | Job | Size |
| --- | --- | --- |
| `cli.py` | Commands, `init` interview, `fail` log entries | \~150 lines |
| `tick.py` | The control table below | \~120 lines |
| `schedule.py` | Frontier, Haiku prediction, risk score, soft and hard edges | \~100 lines |
| `review.py` | Reviewer per tier, options, verdict parsing | \~60 lines |
| `hosts/*.py` | GitHub, Azure DevOps, markdown adapters | \~60 lines each |
| `templates/gates/` | CI workflow, hygiene, test-validity and mutation scripts | \~120 lines |
| `templates/agents/`, `templates/hooks/`, `templates/schemas/` | Implementer, test-writer and reviewer agents, the lanes-review skill, guard and quick-check hooks, two schemas | \~200 lines |
| `examples/fixture-repo/` | Smoke test for every release | small |

There is no orchestrator process, no run state file and no lease: agent view hosts the sessions, and PRs, CI and `claude agents --json --all` are the state. `lanes tick` is the whole control loop. Per lane it reads facts and acts once, holding a file lock so its git calls never overlap:

| Fact | Action |
| --- | --- |
| `state: blocked` | Notify you; nothing else |
| `blocked-on.json` written | Add the edge, log a planning miss, resume after the producer merges |
| test-dispute.json written | Label needs-human, notify; only you change a locked test |
| CI red, rounds < 2 | Resume with the failing tests and first error lines (≤ 200 lines) |
| CI green, no verdict for this patch | Run the tier's reviewer and any options |
| Verdict blocking, rounds < 2 | Resume with the findings |
| Resume printed a new session ID | Re-map the lane |
| Rounds = 2 and still failing | Label `needs-human`, notify |
| Green, clean, behind `main` | Update branch; on conflict, resume once with "rebase on origin/main" |
| Green, clean, up to date | Mark ready for review, notify |
| `state: failed` after a reboot | Resume with `claude --resume <id> --bg "continue"` |
| PR merged | Fetch, recompute the frontier, dispatch up to N lanes |

A crashed or skipped tick costs ten minutes. Never delete a `needs-human` lane in agent view: deleting removes its worktree with any uncommitted changes.

## Building Lanes with Lanes

Lanes builds part of itself from its second week, and dispatches its own tickets unattended from about its fourth. The durations assume steady attention; stretch them in proportion to the time you have.

| When | What builds what | Still by hand |
| --- | --- | --- |
| Days 1–3 | One interactive session writes the gates templates and `lanes init`, then initialises the Lanes repository itself. From here on, CI certifies every change to Lanes | Everything except certification |
| Weeks 1–2 | Lanes dispatched by hand (`claude --bg --agent implementer`) build `tick` | Dispatch and review |
| Weeks 2–3 | `tick` runs the fix and review loop while lanes build `plan` and `go` | Dispatch |
| Weeks 3–4 | `lanes plan` and `go` dispatch Lanes' own tickets | Approve tickets, merge |

Three rules keep self-hosting safe:

- **Run a pinned release, develop on `main`.** Lanes runs from the last tagged version (`uv tool install`), never from its own working tree. A new tag is cut only after the gates pass and the fixture repo runs end to end, so a bad change to `tick` can't break the loop that would fix it.
- **The safety net stays human.** Changes to the gates templates, `guard.sh` and the hooks are always tier `high` and need the `allow-config` label, so Lanes cannot loosen its own checks.
- **Split the code before parallelising it.** In a 700-line tool, one big file would be a hotspot that serialises every ticket, which is why the layout above has separate modules. Even so, Lanes is too small to calibrate the threshold; that needs about 30 parallel pairs from a larger target repository.

Every defect logged against Lanes itself becomes a new case in the fixture repository, so the smoke test grows with the failures it has to prevent.

## Rollout

Each step has a gate; stop at the first one that fails.

1. **Setup (half a day).** Create the Lanes repository; install Claude Code ≥ v2.1.295 and Matt's skills as the plugin, and vendor `lanes-review` at a pinned commit. Gate: `lanes-review` returns a schema-valid verdict on a sample diff, with no agent beyond its two sub-agents. Install Codex only if you switch that option on.
2. **Gates (2–3 days).** Write the gates templates; `lanes init` them into the Lanes repository and a first target repository you own and can read every diff of. Run them against that target's last 20 merged PRs. Gate: no false positive you can't fix in an hour, and at least one weakened or vacuous test caught.
3. **Manual lanes (1 week).** Build `tick` with lanes you start by hand, reviewed with `/lanes-review origin/main`. Gate: first-pass CI green on most tickets.
4. **`tick` (1 week).** `tick` drives fix rounds, review and update-branch while hand-dispatched lanes build `plan` and `go`. Gate: no PR reaches you red or with an unaddressed blocking finding.
5. **`plan` and `go` (1 week).** Lanes dispatches its own tickets, and the target repository runs at N = 1, then at N = 2 and 3 with parallel lanes switched on. The parallelism spike below runs here. Gate: under one conflict per ten parallel pairs, and under an hour a day of your integration time.
6. **A second target.** Preferably on Azure DevOps, to verify that adapter against a real board and pipeline.

The parallelism spike answers three questions in that week:

| Question | How | Stop rule |
| --- | --- | --- |
| Do predicted touch sets match reality? | Predict for 20 already-merged tickets and compare with their real diffs; no agents run | Recall under 70% on hotspot files: serialise by default, parallel by hand only |
| Is N = 3 worth it over N = 2? | The same 10 tickets at N = 2 and N = 3: conflicts per pair, your integration minutes | Under 20% wall-time saving: stay at 2 |
| Do declared contracts prevent semantic breaks? | Re-run NP-Bench's contract-change scenario shape on the target repository | Breaks despite declared contracts: treat contracts as hard edges only |

## Measuring

From step 3, `lanes report` tracks three numbers per route and tier: first-pass acceptance, fix rounds per ticket, and defects you caught at merge (`lanes fail`). They drive the self-correction of the decision tables and decide whether anything more is worth adding.

The only published comparison of skill sets does not answer Lanes' question: its tasks are small enough that every arm passes. After step 5, run a replay bench on your own work instead. Take 10 tickets you have already merged, with their tests, from after the models' training cutoff. Hide the tests, then replay each ticket three ways: a plain `claude --bg` session that you review by hand; `/implement-spec`, with the same gates on its PR and one final `lanes-review`; and Lanes. `/implement-spec` is the arm to beat, because it is what you would use without Lanes. Score hidden-test pass, defects you catch, fix rounds, cost per ticket and wall time. Run it on the lean default first, then with an option on before that option becomes the default for any class.

## Alternatives considered

| Option | What it gives | Why not the default | Use it when |
| --- | --- | --- | --- |
| A full orchestrator process (Tempo) | Run state file, lease, liveness, resume | Agent view now hosts, restarts and reports sessions; PRs and CI hold the state | Never, unless agent view goes away |
| Claude Code Projects | Zero code: a coordinator starts cloud threads that open PRs and auto-fix CI and review comments | Public beta on Pro and Max, github.com only. A model coordinates, thread limits are instructions rather than caps, auto-fix has no round cap and can't react to merge conflicts | Personal GitHub work while your machine is off, with the gates still certifying |
| Matt's `/implement-spec` | One session runs the ticket graph in parallel worktrees onto one integration branch | An agent orchestrates, and every ticket lands on one integration branch: CI never certifies a single ticket, a high ticket shares one diff with trivial ones, and the one end-of-run review has no stop rule | A supervised afternoon on a small spec, and as the arm to beat in the replay bench |
| `/autofix-pr` | A cloud session watches one PR and pushes fixes | Cold context, uncapped, needs the Claude GitHub App | One stuck PR |
| Dynamic workflows, `/batch` | Scripted fan-out of subagents | Workflow scripts can't run shell commands or take mid-run input, so they can't host external gates | `/batch` inside one ticket for the migrate step of an expand–contract refactor |
| Sandcastle | Docker, Podman or Vercel sandboxes, exec gates, resume and fork | Another language and dependency; agent view already isolates local lanes | Repositories you don't trust: swap `lanes go` internals for Sandcastle |
| A shared benchmark platform | Comparable results across users | skillarena already exists, and small tasks saturate | Contribute task packs there if you want to share |
| Claude Code's built-in /code-review and Ultrareview | Bug hunting by several agents; Ultrareview reproduces each finding in a cloud sandbox | More usage than one pass; Ultrareview bills usage credits after 3 free runs and uploads the code | As the adversarial option, if the cost suits you |

## Risks and open questions

The full register, 79 risks rated by likelihood and impact with a mitigation and status each, is in Risk analysis. The six that matter most are a reviewer that shares the implementer's blind spots (the lean default's one likely and severe risk), client code reaching a model vendor without approval, an outsider's issue becoming a lane prompt, a lane granting itself `allow-config`, a one-way-door change classified as small, and secrets reaching review inputs; the design above mitigates each. The questions still open:

- [ ] Does Sonnet 5.5 hold first-pass acceptance, or should `medium` tickets start on Opus 5.5?
- [ ] Is a 0.15 threshold too strict for small repositories where most tickets touch the same few files?
- [ ] Should `trivial` PRs auto-merge after CI, or does every merge stay a click?
- [ ] What replaces test validity for UI and infrastructure tickets: snapshot diffs, a deploy dry-run, or a second reviewer only?
- [ ] Which Azure DevOps calls give update-branch and predecessor links reliably?
- [ ] Which mutation tool limits mutants to changed code for each test runner, starting with pytest?
- [ ] Does the lean default let defects through that adversarial review would catch? The replay bench answers it per tier.

## Sources

Pages read on 17 October 2026, plus the Tempo architecture and assessment documents this design replaces.

- Claude Code: [Agent view](https://code.claude.com/docs/en/agent-view.md), [Run agents in parallel](https://code.claude.com/docs/en/agents.md), [Worktrees](https://code.claude.com/docs/en/worktrees.md), [Hooks](https://code.claude.com/docs/en/hooks.md), [Headless mode](https://code.claude.com/docs/en/headless), [Dynamic workflows](https://code.claude.com/docs/en/workflows), [Code Review](https://code.claude.com/docs/en/code-review), [Commands](https://code.claude.com/docs/en/commands), [Projects](https://code.claude.com/docs/en/claude-projects), [Cloud sessions and auto-fix](https://code.claude.com/docs/en/claude-code-on-the-web), [Cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging)
- Matt Pocock, skills v1.3: [changelog](https://www.aihero.dev/skills/skills-changelog-v13-implement-spec-pr-retro-and-glossary-md), [setup](https://www.aihero.dev/skills-setup-matt-pocock-skills), [/to-tickets](https://www.aihero.dev/skills-to-tickets), [/implement](https://www.aihero.dev/skills-implement), [/implement-spec](https://www.aihero.dev/skills-implement-spec), [/code-review](https://www.aihero.dev/skills-code-review), [/retro](https://www.aihero.dev/skills-retro), [/triage](https://www.aihero.dev/skills-triage), [/wayfinder](https://www.aihero.dev/skills-wayfinder), [Sandcastle](https://github.com/mattpocock/sandcastle)
- [superpowers v7.0.0](https://github.com/obra/superpowers/releases/tag/v7.0.0)
- OpenAI: [Codex plugin for Claude Code](https://github.com/openai/codex-plugin-cc), [Codex models](https://developers.openai.com/codex/models), [Codex non-interactive mode](https://learn.chatgpt.com/codex/non-interactive-mode)
- GitHub: [Managing a merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
- Evidence: [skillarena October 2026 results](https://github.com/caioagiani/skillarena/blob/main/docs/results/2026-10-claude-code-sonnet-5-5.md), [NP-Bench](https://arxiv.org/abs/2610.07261), [Augment multi-agent workspace guide](https://www.augmentcode.com/guides/how-to-run-a-multi-agent-coding-workspace), [Compound Engineering PR #1007](https://github.com/EveryInc/compound-engineering-plugin/pull/1007), [Adam Tornhill on merge conflicts](https://adamtornhill.substack.com/p/why-merge-conflicts-became-the-new)
