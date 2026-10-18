# Lanes

Takes a refined ticket to a reviewed, mergeable pull request, with code (never a model) deciding what runs next. See `GLOSSARY.md` and `docs/adr/`.

```bash
uv run lanes check .scratch/<feature>/issues/01-<slug>.md
```

## Gates

The `gates` check runs `.lanes/gates.py` on every pull request: no test deleted, skipped or xfailed against the base, no CI, coverage or lint config change without the `allow-config` label, and no failure that is new against the base. It reaches the tests only through `.lanes/runner` (ADR-0002). `.lanes/` holds the copies `lanes init` installs from `src/lanes/templates/`; `tests/test_gates.py` proves them against the seeded branches in `examples/fixture-repo/`.

Release checklist and smoke test: see #14.
