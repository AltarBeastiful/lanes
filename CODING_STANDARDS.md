# Coding standards

Both reviewers read this file. It holds judgement calls; mechanical rules belong in lint or the gates.

## Dependencies

Add a runtime dependency only when it replaces code we would otherwise write and it meets all of these:

- a release in the last 12 months;
- a stable major version for at least 2 years;
- broad adoption: widely downloaded on PyPI or depended on by many projects;
- a permissive licence (MIT, BSD, Apache-2.0 or similar);
- few transitive dependencies.

Pin each dependency to its current major version. Current set: click, jinja2, pydantic, tomli-w.

## Shapes

Pydantic models are the single source for every structured shape: verdicts, touch predictions, `lanes.toml`, the ticket's `## Lanes` section and log events. JSON Schema files are generated from them, never edited by hand.

## State

Only `HostAdapter` calls the host, and only `LaneStore` touches the state directory (see `docs/adr/0001-no-orchestrator-state.md`).
