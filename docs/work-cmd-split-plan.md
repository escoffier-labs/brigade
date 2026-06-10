# work_cmd.py Package Split Plan

Status: planned, not started. Prerequisites landed first: `brigade.localio` helper extraction and the `brigade.actionqueue` extraction. This note captures the seam analysis so the split can execute in a dedicated session without re-deriving it.

## Why this is the last refactor stage, not the first

The test suite monkeypatches the flat `work_cmd` namespace heavily:

- `work_cmd._now`: 100+ patch sites
- `work_cmd.shutil` / `work_cmd.subprocess`: ~70 patch sites
- public functions (`work_cmd.run`, `work_cmd.status`, `work_cmd.brief`, ...): patched in orchestration tests

Python module semantics mean a naive split breaks these silently: a submodule that does `from .helpers import _now` binds the function at import time, so patching the facade attribute `work_cmd._now` no longer affects it. Tests would pass or fail misleadingly rather than loudly.

## The discipline that makes the split safe

1. **Module-attribute access for everything shared.** Submodules never do `from .helpers import _now`; they do `from . import helpers` and call `helpers._now()`. Same for cross-family calls (`ledger._find_task`, not an imported name).
2. **Test sites move to the source module.** Mechanical rewrite of patch targets: `monkeypatch.setattr(work_cmd, "_now", ...)` becomes `monkeypatch.setattr(work_cmd.helpers, "_now", ...)`. About 170 sites; sed-able per family, verified by the suite.
3. **Facade re-exports for external callers.** `work_cmd/__init__.py` re-exports the full public surface (77 public functions, ~60 externally-referenced private helpers, ~50 constants) so `cli.py`, `daily_cmd.py`, `memory_cmd.py`, and friends keep `work_cmd.X` access unchanged. Public-function patches keep working because external dispatch goes through the facade attribute.

## Target layout (acyclic import order)

```
src/brigade/work_cmd/
    __init__.py    # facade: explicit re-export list
    constants.py   # all module-level constants (current lines ~1-425)
    helpers.py     # git/text/time/slug/path/snapshot utilities (~428-603)
    ledger.py      # task + import ledger CRUD, queries, github issue glue, handoff metadata
    config.py      # backup/scanner/review toml load + validation, schedule math
    services.py    # backup, review, verify/closeout, scanners, sweep, inbox operations
    session.py     # session lifecycle, run/status/doctor/brief, task ops
```

Import direction only flows downward in that list. `services` and `session` are still large; they can split further later, but this gets every file under ~3k lines without circular imports.

## Execution recipe

1. Branch. Move constants out first; suite green.
2. Move helpers with module-attribute discipline; rewrite the `_now`/`shutil`/`subprocess` test patch targets; suite green.
3. Move families one at a time (ledger, config, services, session), running the suite after each move. Never move two families in one step.
4. Facade `__init__.py` grows its re-export list with each move; a new test asserts the facade exposes every name `cli.py` and other modules reference.
5. Finish with `brigade roadmap commands --check` (parser untouched, should be clean) and a full release-gate pass.

The full external-symbol inventory and per-family line map from the 2026-06-09 analysis is reproducible with:
`grep -n "work_cmd\." src/brigade/*.py tests/*.py` and the section headers inside the module.
