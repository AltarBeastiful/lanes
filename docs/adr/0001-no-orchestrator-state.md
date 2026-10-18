# Lanes keeps no state of its own: PRs, CI and agent view are the state

Lanes has no orchestrator process, run-state file or lease. Each `tick` re-reads facts from the host (tickets, PRs, CI) and from agent view (`claude agents --json --all`), acts once per lane, and exits. The pull request is a lane's durable anchor: its body carries `Closes #<n>`, so a lane can always be rebuilt from the host. The per-repo state directory holds only a rebuildable cache (lane → session ID, touch predictions) and an append-only event log; deleting it loses history, never a lane. We chose this over a Tempo-style orchestrator because agent view now hosts, restarts and reports sessions, and a second copy of that state would drift from it.

## Consequences

- All state access goes through two seams: `HostAdapter` (tickets, edges, contracts, PRs) and `LaneStore` (cache and log). Nothing else reads the state directory or calls the host directly.
- Moving to beads, plain files in the repo, or a database is a new `HostAdapter` or `LaneStore` implementation, not a rewrite of `tick`. Keep both interfaces small and free of GitHub- or filesystem-specific types so that stays true.
- The log is the one thing worth migrating, so its events are versioned records, not ad-hoc lines.

## Considered options

- **Orchestrator with run state and leases (Tempo)**: duplicates what agent view now owns.
- **beads as the store from day one**: plausible later, but adds a dependency before we know which state is worth keeping; the seams leave the door open.
