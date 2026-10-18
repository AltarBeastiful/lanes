# Fixture repository

Seeded cases the gates must catch, plus one clean branch they must pass. The harness builds a real git repository from `main/`, then one branch per directory under `branches/`: each branch copies its files over `main/` and removes the paths listed in its `DELETED` file, if any. The `.lanes/` gate script and runner come from `src/lanes/templates/`, as `lanes init` would install them.

| Branch | Case | Gate |
| --- | --- | --- |
| `clean` | adds a function and its test, edits non-config `pyproject.toml` fields | passes |
| `deletes-test` | removes `test_divide` | fails |
| `deletes-test-file` | removes `tests/test_more.py` | fails |
| `adds-skip` | marks `test_divide` skipped | fails |
| `adds-xfail` | marks `test_add` xfail | fails |
| `skips-without-fixture` | adds a test that skips when its gitignored fixture is missing | fails |
| `edits-lint-config` | raises ruff's line length | fails without `allow-config`, passes with it |
| `breaks-add` | breaks `add`, a new failure | fails |

`test_mean_of_nothing_is_zero` already fails on `main`, so every branch, `clean` included, keeps one failure that must not count.
