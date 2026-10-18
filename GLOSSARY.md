# Lanes

Lanes takes a refined ticket to a reviewed, mergeable pull request, with code (never a model) deciding what runs next.

## Language

**Ticket**:
A tracker item that carries everything Lanes needs to dispatch it and has passed `lanes check`.
_Avoid_: Task, story, work item

**Lane**:
One ticket's run from dispatch to merge: exactly one session, one branch and one pull request, named `T<n>` after the ticket. A lane survives its session changing ID.
_Avoid_: Job, run, worker

**Fix round**:
One resume of a lane caused by a red CI run or a blocking verdict.
_Avoid_: Retry, iteration

**Route**:
How much planning a ticket gets before it runs: direct, grill, spec or wayfinder.
_Avoid_: Flow, mode

**Tier**:
How much review and merge scrutiny a ticket gets: trivial, small, medium or high.
_Avoid_: Risk level, priority

**Contract**:
A name one ticket produces and another consumes, such as a type, config key, endpoint or schema field.
_Avoid_: Interface, dependency

**Frontier**:
The tickets whose blockers have all merged into `main`.
_Avoid_: Ready queue, backlog

**Hard edge**:
A dependency that forces two tickets to run one after the other.
_Avoid_: Dependency, blocker (when meaning the edge itself)

**Soft edge**:
A recorded collision risk between two tickets that does not stop them running together.
_Avoid_: Warning

**Verdict**:
One reviewer's structured result for one commit: blocking findings and nits.
_Avoid_: Review, report

**Gates**:
The CI checks that certify a pull request; the only evidence Lanes accepts.
_Avoid_: Checks, validation

**Hotspot**:
A file whose edits are likely to collide between lanes.
_Avoid_: Shared file, conflict file

**Planning miss**:
A hard edge discovered only while a lane is running.
_Avoid_: Surprise dependency

**Runner**:
The committed script through which the gates run a repository's tests and list its skipped tests; the only part of Lanes that knows a test framework.
_Avoid_: Test adapter, test plugin

**Blocked lane**:
A lane that stopped because it needs a contract no merged code provides, saying which in an uncommitted `.lanes-blocked-on` file at the root of its worktree; it may have no pull request yet.
_Avoid_: Stuck lane
