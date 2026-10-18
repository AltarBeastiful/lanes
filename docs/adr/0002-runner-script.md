# Test frameworks plug in through one committed runner script

The gates need only two answers from a test framework: whether given test files pass, fail on an assertion, or fail to run at a given commit, and which tests are skipped. Rather than build an adapter per framework into Lanes, or let a model judge test validity at CI time, each repository commits one `.lanes/runner` script with two commands (`run [files…]`: runs the given files, or the whole suite when none are given, prints one `<outcome> <test-id>` line per test, and exits 0 pass, 1 assertion failure, anything else inconclusive; `skips`: one test ID per line, xfails included). Lanes ships the pytest runner; for any other framework `lanes init` has a model draft the script once, and the human reviews and commits it under `allow-config`. No model runs inside the gates, so CI stays the only certifier.

The gates need test IDs, not just an exit code, to catch a test deleted from inside a file and to tell a new failure from one already failing on the base. `run`'s outcome lines (passed, failed, skipped, xfailed, or error for a file that fails to collect) give them without a third command. Each test ID starts with its file's path followed by `::`, so the gates can run a new test's file on the base commit to prove the test fails there.

## Considered options

- **Built-in adapters per framework**: reimplements test-runner knowledge that each repository already has.
- **A model classifies test results in CI**: makes a model part of the certifier, against the rule that only CI output counts as evidence.
- **Replace the script later** with a third-party tool or an agent: still possible, since the gates only see the script's contract.
- **A third `tests` command listing test IDs**: one more command for every runner to implement, when `run` already sees every test.
